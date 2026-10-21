export * as ArchCreatorPlugin from "./arch-creator.js"

import { ToolFailure } from "@copilotcli/ai"
import { define } from "@copilotcli/plugin/effect/plugin"
import { Agent } from "@copilotcli/schema/agent"
import { FSUtil } from "@copilotcli/util/fs-util"
import { AppProcess } from "@copilotcli/util/process"
import { Effect, Schema } from "effect"
import { ChildProcess } from "effect/process"
import { createHash } from "crypto"
import { accessSync, constants, realpathSync, statSync } from "fs"
import fs from "fs/promises"
import path from "path"
import { FileAccess } from "../file-access.js"
import { Location } from "../location.js"
import { Permission } from "../permission.js"
import METHOD from "./arch-creator/method.txt"

// archcreator is a separate, deterministic Python tool: it parses the C sources, builds the
// as-built module model and evaluates the rules without any model. This plugin only runs it and hands its summary to
// the ArchCreator agent, whose method forbids changing or inventing findings. The archcreator exit code stays the gate.

export const agentID = Agent.ID.make("arch-creator")
export const analyzeTool = "archcreator_analyze"
export const modulesTool = "archcreator_modules"
/** Output folder below the project directory, one subfolder per session so that concurrent sessions on the same
 *  project never read each other's summary; archcreator writes it, the agent may add notes next to it. */
export const outputRoot = path.join(".flexcli", "archcreator")

/** A session id as a folder name, one folder per id. Only an id of lowercase letters, digits, "_" and "-" that is no
 *  Windows device name is used as it is; any other id is lowercased and cleaned, then gets a hash of the original after
 *  a "~", which no id used as it is contains. So `ses:a` and `ses_a` never share a folder, and neither do ids that
 *  Windows would map to one folder: `ses_A` and `ses_a` (case), `ses_a.` and `ses_a` (trailing dot), or `nul`. */
export function sessionFolder(sessionID: string): string {
  const device = /^(con|prn|aux|nul|com[0-9]|lpt[0-9])$/
  if (/^[a-z0-9_-]+$/.test(sessionID) && !device.test(sessionID)) return sessionID
  const safe = sessionID.toLowerCase().replace(/[^a-z0-9_-]/g, "_")
  return `${safe}~${createHash("sha256").update(Buffer.from(sessionID, "utf16le")).digest("hex").slice(0, 16)}`
}
export const TIMEOUT = "5 minutes"
const MAX_OUTPUT_BYTES = 256 * 1024

const MODULE_NAME = /^[A-Za-z0-9_]+$/
const SUMMARY_SCHEMA = "archcreator.summary/1"
/** Permission resource prefix for a profile override: `profile:<absolute path>`. */
export const PROFILE_RESOURCE = "profile:"
/** Permission resource of a plain module listing. */
export const MODULES_RESOURCE = "list"

export const EXIT_MEANING: Record<number, string> = {
  0: "ok",
  1: "findings at or above --fail-on",
  2: "usage, profile or module-resolution error",
  3: "parse coverage below the profile gate",
  4: "accounting check failed (internal consistency error)",
}

type Env = Record<string, string | undefined>

/** The canonical absolute path: links and junctions followed where the path exists. Case-insensitive on Windows. */
function canonical(p: string): string {
  let abs = path.resolve(p)
  try {
    abs = realpathSync.native(abs)
  } catch {
    // Not there (yet): the lexical path is all there is.
  }
  return process.platform === "win32" ? abs.toLowerCase() : abs
}

/** Is `target` the project directory or below it, once links are followed on both sides? */
function insideProject(target: string, projectDirectory: string): boolean {
  const rel = path.relative(canonical(projectDirectory), canonical(target))
  if (rel === "") return true
  if (path.isAbsolute(rel)) return false // another drive
  // Outside means the relative path climbs out: a ".." segment, not a name that merely starts with two dots.
  const first = rel.split(path.sep)[0]
  return first !== ".."
}

/**
 * The archcreator executable: ARCHCREATOR_BIN as an absolute path, else `archcreator` found on PATH. Resolved here rather than
 * by the process launcher, because the launcher would also look in the working directory, which is the analyzed
 * repository: a checkout must not be able to plant an `archcreator.cmd` that then runs. For the same reason a
 * directory inside the project is never used, whether it comes from ARCHCREATOR_BIN or from PATH.
 */
export function resolveExecutable(
  projectDirectory: string,
  env: Env = process.env,
  isFile: (p: string) => boolean = (p) => {
    // A regular file that may be executed: on POSIX the execute bit decides, as it does for the shell's lookup.
    try {
      if (!statSync(p).isFile()) return false
      if (process.platform !== "win32") accessSync(p, constants.X_OK)
      return true
    } catch {
      return false
    }
  },
): { path: string } | { problem: string } {
  const bin = env.ARCHCREATOR_BIN?.trim()
  if (bin) {
    if (!path.isAbsolute(bin)) return { problem: `ARCHCREATOR_BIN must be an absolute path, got "${bin}"` }
    if (insideProject(bin, projectDirectory))
      return { problem: `ARCHCREATOR_BIN points inside the project directory (${bin}); archcreator must be installed outside the repository` }
    return { path: bin }
  }
  const exts = process.platform === "win32" ? (env.PATHEXT ?? ".COM;.EXE;.BAT;.CMD").split(";").filter(Boolean) : [""]
  for (const entry of (env.PATH ?? "").split(path.delimiter).filter(Boolean)) {
    // Absolute before anything else: a relative entry would mean one folder for the check (the CLI's working
    // directory) and another for the launch (the project).
    // Windows allows a quoted entry ("C:\Program Files\ArchCreator"); the quotes are not part of the folder.
    const dir = path.resolve(entry.trim().replace(/^"(.*)"$/, "$1"))
    if (insideProject(dir, projectDirectory)) continue
    for (const ext of exts) {
      const candidate = path.join(dir, `archcreator${ext}`)
      // The file itself may be a link into the repository even when its folder is not.
      if (isFile(candidate) && !insideProject(candidate, projectDirectory)) return { path: candidate }
    }
  }
  return { problem: "archcreator was not found on PATH. Install it (see its README) or set ARCHCREATOR_BIN to its full path." }
}

export function analyzeArgs(input: {
  project: string
  module: string
  out: string
  profile?: string
  failOn?: string
  context?: readonly string[]
}) {
  return [
    "analyze",
    "--repo",
    input.project,
    "--module",
    input.module,
    "--out",
    input.out,
    ...(input.context && input.context.length > 0 ? ["--context", input.context.join(",")] : []),
    ...(input.profile ? ["--profile", input.profile] : []),
    ...(input.failOn ? ["--fail-on", input.failOn] : []),
  ]
}

/** Module names for --context: identifiers, or "all" on its own. Returns the problem, if any. */
export function contextProblem(context: readonly string[] | undefined): string | undefined {
  if (context === undefined) return undefined
  if (context.length === 0) return "context must name at least one module, or 'all'"
  if (context.includes("all")) return context.length === 1 ? undefined : "'all' stands alone in context"
  const bad = context.filter((m) => !MODULE_NAME.test(m))
  return bad.length > 0 ? `Invalid module name(s) in context: ${bad.join(", ")} (letters, digits and _ only)` : undefined
}

/** The central profile folders of ARCHCREATOR_PROFILES (archcreator searches them, the agent may read them). */
export function profileDirectories(env: Env = process.env): string[] {
  return (env.ARCHCREATOR_PROFILES ?? "").split(path.delimiter).map((d) => d.trim()).filter(Boolean)
}

/** Added after the agent defaults; the last matching rule wins. Mirrors C Debug: read freely, write notes only. */
export function rules(projectDirectory: string, env: Env = process.env) {
  return [
    { action: "*", resource: "*", effect: "ask" as const },
    ...["read", "grep", "glob", "question", "skill", analyzeTool, modulesTool].map((action) => ({
      action,
      resource: "*",
      effect: "allow" as const,
    })),
    // A profile the model names changes include paths, defines, thresholds and the baseline: the user decides.
    // The tools assert it as the resource `profile:<path>`, whatever the file is called.
    ...[analyzeTool, modulesTool].map((action) => ({ action, resource: `${PROFILE_RESOURCE}*`, effect: "ask" as const })),
    { action: "read", resource: "*.env", effect: "ask" as const },
    { action: "read", resource: "*.env.*", effect: "ask" as const },
    { action: "read", resource: "*.env.example", effect: "allow" as const },
    // The central profile folders may be read without approval; anything else outside the project asks.
    ...profileDirectories(env).map((dir) => ({
      action: "external_directory",
      resource: path.join(dir, "*"),
      effect: "allow" as const,
    })),
    { action: "edit", resource: "*", effect: "deny" as const },
    { action: "edit", resource: ".flexcli/*", effect: "allow" as const },
    { action: "edit", resource: path.join(projectDirectory, ".flexcli", "*"), effect: "allow" as const },
    // archcreator's outputs are the findings; the agent explains them and must never be able to change them.
    { action: "edit", resource: `${outputRoot.split(path.sep).join("/")}/*`, effect: "deny" as const },
    { action: "edit", resource: path.join(projectDirectory, outputRoot, "*"), effect: "deny" as const },
    { action: "subagent", resource: "*", effect: "deny" as const },
    { action: "webfetch", resource: "*", effect: "deny" as const },
    { action: "websearch", resource: "*", effect: "deny" as const },
  ]
}

const FailOn = Schema.Literals(["info", "minor", "major", "critical"])

export const AnalyzeInput = Schema.Struct({
  module: Schema.String.annotate({ description: "Module name: the C file name without .c, e.g. DCDC_FSM" }),
  context: Schema.optionalKey(Schema.Array(Schema.String)).annotate({
    description:
      "Other in-build modules to parse alongside, or ['all']. archcreator parses only what it is told: callers of the module, who writes or reads its signals, and cycles are known among the module and its context only. Omit for the module's own view (fast); name the modules in question, or 'all' (slow), when the user asks who calls or produces something.",
  }),
  profile: Schema.optionalKey(Schema.String).annotate({
    description:
      "archcreator profile YAML, only to override. Normally omitted: archcreator picks the central profile that matches this repository (ARCHCREATOR_PROFILES).",
  }),
  fail_on: Schema.optionalKey(FailOn).annotate({
    description: "Exit 1 when a finding has at least this severity (for gating; normally omitted).",
  }),
})

export const ModulesInput = Schema.Struct({
  profile: Schema.optionalKey(Schema.String).annotate({ description: "archcreator profile YAML, as for archcreator_analyze" }),
})

const AnalyzeOutput = Schema.Struct({ exitCode: Schema.Finite, outDir: Schema.String })

const notFound = (cmd: readonly string[]) =>
  `archcreator could not be started (${cmd[0]}). Install archcreator (see its README) or set ARCHCREATOR_BIN to its full path.`

/** The command line to start archcreator with: the test override, else the resolved executable. */
function executable(project: string, override: readonly string[] | undefined): { cmd: readonly string[] } | { problem: string } {
  if (override) {
    return insideProject(override[0]!, project)
      ? { problem: `archcreator inside the project directory is not used (${override[0]})` }
      : { cmd: override }
  }
  const resolved = resolveExecutable(project)
  return "problem" in resolved ? resolved : { cmd: [resolved.path] }
}

export function makePlugin(options: { command?: readonly string[] } = {}) {
  return define({
    id: "copcli.arch-creator",
    effect: Effect.fn(function* (ctx) {
      const location = yield* Location.Service
      const processes = yield* AppProcess.Service
      const permission = yield* Permission.Service
      const access = yield* FileAccess.Service
      const files = yield* FSUtil.Service
      const project = location.project.directory

      const profileFor = (profile: string | undefined, context: FileAccess.Invocation) =>
        Effect.gen(function* () {
          if (profile) {
            // The path becomes a permission resource; a wildcard character in it would become a pattern.
            if (/[*?]/.test(profile))
              return yield* Effect.fail(new ToolFailure({ message: `Profile path must not contain * or ?: ${profile}` }))
            const target = yield* access.resolve({ path: profile, kind: "file" })
            yield* access.authorizeExternal([target], context)
            if (!(yield* files.isFile(target.absolute)))
              return yield* Effect.fail(new ToolFailure({ message: `Profile not found: ${profile}` }))
            return target.absolute as string
          }
          // Profiles are kept centrally; archcreator itself picks the one whose match names this repository.
          return undefined
        })

      const run = (args: readonly string[]) =>
        Effect.gen(function* () {
          const exe = executable(project, options.command)
          if ("problem" in exe) return yield* Effect.fail(new ToolFailure({ message: exe.problem }))
          const cmd = exe.cmd
          // archcreator gets every path absolute, so the child need not run inside the repository: a .cmd wrapper or
          // the launcher could otherwise pick up files from there. Its own folder is neutral by construction.
          return yield* processes
            .run(
              ChildProcess.make(cmd[0]!, [...cmd.slice(1), ...args], {
                cwd: path.dirname(cmd[0]!),
                extendEnv: true,
                env: { NoDefaultCurrentDirectoryInExePath: "1" },
                stdin: "ignore",
              }),
              { timeout: TIMEOUT, maxOutputBytes: MAX_OUTPUT_BYTES, maxErrorBytes: MAX_OUTPUT_BYTES },
            )
            .pipe(Effect.mapError((error) => new ToolFailure({ message: notFound(cmd), error })))
        })

      yield* ctx.tool
        .transform((editor) => {
          editor.add({
            name: analyzeTool,
            options: { codemode: false },
            description:
              "Run archcreator, the deterministic architecture review of one C module (no model involved), and return its summary: commit, parse coverage, finding counts, the first findings and the output folder with report.md, findings.json and model.json.",
            input: AnalyzeInput,
            output: AnalyzeOutput,
            execute: (input, context) =>
              Effect.gen(function* () {
                if (!MODULE_NAME.test(input.module))
                  return yield* Effect.fail(
                    new ToolFailure({ message: `Invalid module name: ${input.module} (letters, digits and _ only)` }),
                  )
                const problem = contextProblem(input.context)
                if (problem) return yield* Effect.fail(new ToolFailure({ message: problem }))
                const source = { type: "tool" as const, messageID: context.messageID, id: context.id }
                const profile = yield* profileFor(input.profile, context)
                yield* permission.assert({
                  action: analyzeTool,
                  // The profile is a resource of its own, so the profile:* rule makes any override ask.
                  resources: [input.module, ...(profile ? [PROFILE_RESOURCE + profile] : [])],
                  // "Always" saves exactly what was asked for: this profile, or this module. Never a pattern that
                  // would also match a profile resource and so silence the prompt for every later override.
                  save: profile ? [PROFILE_RESOURCE + profile] : [input.module],
                  metadata: { module: input.module, context: input.context, profile, fail_on: input.fail_on },
                  sessionID: context.sessionID,
                  agent: context.agent,
                  source,
                })
                const out = path.join(project, outputRoot, sessionFolder(String(context.sessionID)), input.module)
                // Absolute: the read tools resolve relative paths against the session directory, which can be
                // below the project root where archcreator writes.
                const outDir = out.split(path.sep).join("/")
                const summaryFile = path.join(out, "summary.json")
                // A summary left by an earlier run must never stand in for this one.
                yield* Effect.promise(() => fs.rm(summaryFile, { force: true }))
                const result = yield* run(
                  analyzeArgs({ project, module: input.module, out, profile, failOn: input.fail_on, context: input.context }),
                )
                const text = yield* files.readFileStringSafe(summaryFile).pipe(Effect.orElseSucceed(() => undefined))
                const summary = text === undefined ? undefined : parseSummary(text)
                if (!summary)
                  return yield* Effect.fail(
                    new ToolFailure({
                      // On Windows a missing executable surfaces as cmd's "not recognized" with exit 1, not as a
                      // spawn error, so the install hint goes with every run that produced no summary.
                      message: [
                        `archcreator exited ${result.exitCode} without a summary.`,
                        tail(result.stderr),
                        `Check that archcreator is installed (see its README) or set ARCHCREATOR_BIN to its full path.`,
                      ]
                        .filter(Boolean)
                        .join(" "),
                    }),
                  )
                const meaning = EXIT_MEANING[result.exitCode] ?? "unexpected exit code"
                const analyzed = Array.isArray(summary.analyzed_modules) ? (summary.analyzed_modules as string[]) : undefined
                return {
                  output: { exitCode: result.exitCode, outDir },
                  content: [
                    `archcreator exit ${result.exitCode} (${meaning}) for module ${input.module}.`,
                    `Full outputs: ${outDir}/ (report.md, findings.json, model.json).`,
                    ...(analyzed
                      ? [
                          `Parsed modules: ${analyzed.join(", ")}. Callers, signal producers and consumers outside this set are unknown to this run; pass them as context to see them.`,
                        ]
                      : []),
                    "",
                    JSON.stringify(summary, null, 2),
                  ].join("\n"),
                  metadata: { exitCode: result.exitCode },
                }
              }).pipe(
                Effect.mapError((error) =>
                  error instanceof ToolFailure
                    ? error
                    : new ToolFailure({ message: `archcreator failed for module ${input.module}`, error }),
                ),
              ),
          })
          editor.add({
            name: modulesTool,
            options: { codemode: false },
            description: "List the C modules archcreator can review in this project (App folders, in-build flag).",
            input: ModulesInput,
            output: Schema.Struct({ exitCode: Schema.Finite }),
            execute: (input, context) =>
              Effect.gen(function* () {
                const source = { type: "tool" as const, messageID: context.messageID, id: context.id }
                const profile = yield* profileFor(input.profile, context)
                yield* permission.assert({
                  action: modulesTool,
                  resources: [MODULES_RESOURCE, ...(profile ? [PROFILE_RESOURCE + profile] : [])],
                  save: profile ? [PROFILE_RESOURCE + profile] : [MODULES_RESOURCE],
                  metadata: { profile },
                  sessionID: context.sessionID,
                  agent: context.agent,
                  source,
                })
                const result = yield* run([
                  "list-modules",
                  "--repo",
                  project,
                  "--json",
                  ...(profile ? ["--profile", profile] : []),
                ])
                if (result.exitCode !== 0)
                  return yield* Effect.fail(
                    new ToolFailure({ message: `archcreator list-modules exited ${result.exitCode}. ${tail(result.stderr)}` }),
                  )
                return {
                  output: { exitCode: 0 },
                  content: result.stdout.toString("utf8"),
                  metadata: {},
                }
              }).pipe(
                Effect.mapError((error) =>
                  error instanceof ToolFailure ? error : new ToolFailure({ message: "archcreator list-modules failed", error }),
                ),
              ),
          })
        })
        .pipe(Effect.orDie)

      yield* ctx.agent.transform((editor) => {
        editor.update(agentID, (item) => {
          item.name = Agent.Name.make("ArchCreator")
          item.description =
            "Explains the deterministic architecture review (archcreator) of a C module: findings, dependencies, evidence. Writes only its notes."
          item.mode = "primary"
          item.system = METHOD
          item.permissions.push(...rules(project, process.env))
        })
      })
      yield* ctx.command.transform((editor) => {
        editor.add({
          name: "archcreator",
          description: "architecture review of a C module: <Module> [question]",
          execute: (input) =>
            Effect.gen(function* () {
              const session = yield* ctx.session.get({ sessionID: input.sessionID })
              if (session.agent !== agentID) yield* ctx.session.switchAgent({ sessionID: input.sessionID, agent: agentID })
              yield* ctx.session.prompt({
                ...input.prompt,
                sessionID: input.sessionID,
                text: input.prompt.text.trim() || "Ask me which module to review.",
                delivery: input.delivery,
              })
            }),
        })
      })
    }),
  })
}

export function parseSummary(text: string): Record<string, unknown> | undefined {
  try {
    const value: unknown = JSON.parse(text)
    if (typeof value !== "object" || value === null) return undefined
    return (value as { schema?: unknown }).schema === SUMMARY_SCHEMA ? (value as Record<string, unknown>) : undefined
  } catch {
    return undefined
  }
}

const tail = (buffer: Buffer) => {
  const text = buffer.toString("utf8").trim()
  return text.length > 2000 ? `…${text.slice(-2000)}` : text
}

export const Plugin = makePlugin()
