import { describe, expect, test } from "bun:test"
import fs from "fs/promises"
import path from "path"
import { Effect, Layer } from "effect"
import { AppNodeBuilder } from "@copilotcli/core/effect/app-node-builder"
import { makeLocationNode } from "@copilotcli/util/effect/app-node"
import { LayerNode } from "@copilotcli/util/effect/layer-node"
import { AppProcess } from "@copilotcli/util/process"
import { FSUtil } from "@copilotcli/util/fs-util"
import { Agent } from "@copilotcli/core/agent"
import { FileAccess } from "@copilotcli/core/file-access"
import { Location } from "@copilotcli/core/location"
import { Permission } from "@copilotcli/core/permission"
import { ArchCreatorPlugin } from "@copilotcli/core/plugin/arch-creator"
import { AbsolutePath } from "@copilotcli/core/schema"
import { Session } from "@copilotcli/core/session"
import { Tool } from "@copilotcli/core/tool"
import { location } from "../fixture/location"
import { tmpdirScoped } from "../fixture/tmpdir"
import { it } from "../lib/effect"
import { permissionLayer } from "../lib/permission"
import { executeTool, registerToolPlugin, toolIdentity } from "../lib/tool"

const project = path.join(path.sep, "work", "ecu")
const ruleset = [...Agent.Info.default(ArchCreatorPlugin.agentID).permissions, ...ArchCreatorPlugin.rules(project)]
const effect = (action: string, resource: string) => Permission.evaluate(action, resource, ruleset).effect

describe("ArchCreator agent permissions", () => {
  test("runs archcreator and reads freely", () => {
    expect(effect(ArchCreatorPlugin.analyzeTool, "DCDC_FSM")).toBe("allow")
    expect(effect(ArchCreatorPlugin.modulesTool, "*")).toBe("allow")
    expect(effect("read", path.join(project, ".flexcli", "archcreator", "DCDC_FSM", "model.json"))).toBe("allow")
    expect(effect("grep", "*")).toBe("allow")
  })

  test("reads the central profile folders without approval, anything else outside the project asks", () => {
    expect(effect("external_directory", "/share/profiles/100554.yaml")).toBe("ask")
    const share = path.join(path.sep, "share", "profiles")
    const withShare = [
      ...Agent.Info.default(ArchCreatorPlugin.agentID).permissions,
      ...ArchCreatorPlugin.rules(project, { ARCHCREATOR_PROFILES: share }),
    ]
    expect(Permission.evaluate("external_directory", path.join(share, "100554.yaml"), withShare).effect).toBe("allow")
    expect(Permission.evaluate("external_directory", path.join(path.sep, "elsewhere", "x.yaml"), withShare).effect).toBe("ask")
  })

  test("a profile override named by the model asks the user, whatever the file is called", () => {
    expect(effect(ArchCreatorPlugin.analyzeTool, "DCDC_FSM")).toBe("allow")
    expect(effect(ArchCreatorPlugin.analyzeTool, `${ArchCreatorPlugin.PROFILE_RESOURCE}/share/profiles/other.yaml`)).toBe("ask")
    expect(effect(ArchCreatorPlugin.analyzeTool, `${ArchCreatorPlugin.PROFILE_RESOURCE}${path.join(project, "override")}`)).toBe("ask")
    expect(effect(ArchCreatorPlugin.modulesTool, `${ArchCreatorPlugin.PROFILE_RESOURCE}${path.join(project, ".flexcli", "p.txt")}`)).toBe("ask")
  })

  test("writes only its notes under .flexcli: no source, profile or rule changes", () => {
    expect(effect("edit", ".flexcli/review.md")).toBe("allow")
    expect(effect("edit", path.join(project, ".flexcli", "review.md"))).toBe("allow")
    expect(effect("edit", "src/DCDC_FSM.c")).toBe("deny")
    expect(effect("edit", "profiles/100554.yaml")).toBe("deny")
    expect(effect("edit", "../.flexcli/x.md")).toBe("deny")
  })

  test("cannot change archcreator's outputs: the findings are archcreator's, not the model's", () => {
    expect(effect("edit", ".flexcli/archcreator/DCDC_FSM/findings.json")).toBe("deny")
    expect(effect("edit", path.join(project, ".flexcli", "archcreator", "DCDC_FSM", "summary.json"))).toBe("deny")
    expect(effect("edit", path.join(project, ".flexcli", "archcreator", "notes.md"))).toBe("deny")
  })

  test("asks for the shell and anything else not named; no subagent or web", () => {
    expect(effect("shell", "archcreator analyze --repo . --module X")).toBe("ask")
    expect(effect("polarion_apply_plan", "*")).toBe("ask")
    expect(effect("subagent", "explore")).toBe("deny")
    expect(effect("webfetch", "https://example.com")).toBe("deny")
    expect(effect("websearch", "*")).toBe("deny")
    expect(effect("read", path.join(project, ".env"))).toBe("ask")
  })
})

describe("archcreator invocation", () => {
  test("argument list is fixed: no shell, profile and fail-on only when given", () => {
    expect(ArchCreatorPlugin.analyzeArgs({ project: "/r", module: "M", out: "/r/.flexcli/archcreator/M" })).toEqual([
      "analyze", "--repo", "/r", "--module", "M", "--out", "/r/.flexcli/archcreator/M",
    ])
    expect(ArchCreatorPlugin.analyzeArgs({ project: "/r", module: "M", out: "/o", context: ["A", "B"] })).toEqual([
      "analyze", "--repo", "/r", "--module", "M", "--out", "/o", "--context", "A,B",
    ])
    expect(ArchCreatorPlugin.analyzeArgs({ project: "/r", module: "M", out: "/o", context: [] })).not.toContain("--context")
  })

  test("context names are validated before anything runs", () => {
    expect(ArchCreatorPlugin.contextProblem(undefined)).toBeUndefined()
    expect(ArchCreatorPlugin.contextProblem(["all"])).toBeUndefined()
    expect(ArchCreatorPlugin.contextProblem(["SEV", "Error_Check"])).toBeUndefined()
    expect(ArchCreatorPlugin.contextProblem([])).toContain("at least one")
    expect(ArchCreatorPlugin.contextProblem(["all", "SEV"])).toContain("stands alone")
    expect(ArchCreatorPlugin.contextProblem(["a;b"])).toContain("Invalid module name")
    expect(
      ArchCreatorPlugin.analyzeArgs({ project: "/r", module: "M", out: "/o", profile: "/p.yaml", failOn: "major" }),
    ).toEqual(["analyze", "--repo", "/r", "--module", "M", "--out", "/o", "--profile", "/p.yaml", "--fail-on", "major"])
  })

  test("executable from an absolute ARCHCREATOR_BIN, else archcreator on PATH, never from the repository", () => {
    const tools = path.resolve(path.sep, "tools")  // absolute, with the drive on Windows
    const exe = path.join(tools, process.platform === "win32" ? "archcreator.exe" : "archcreator")
    const isFile = (p: string) => p === exe || p === path.join(project, "archcreator.cmd") || p === path.join(project, "archcreator")
    const resolve = (env: Record<string, string>) => ArchCreatorPlugin.resolveExecutable(project, env, isFile)
    expect(resolve({ ARCHCREATOR_BIN: exe })).toEqual({ path: exe })
    expect(resolve({ ARCHCREATOR_BIN: "archcreator.exe" })).toMatchObject({ problem: expect.stringContaining("absolute") })
    expect(resolve({ ARCHCREATOR_BIN: path.join(project, "tools", "archcreator.exe") })).toMatchObject({
      problem: expect.stringContaining("inside the project"),
    })
    expect(resolve({ ARCHCREATOR_BIN: "  ", PATH: [project, tools].join(path.delimiter), PATHEXT: ".exe;.cmd" })).toEqual({ path: exe })
    expect(resolve({ PATH: project, PATHEXT: ".exe;.cmd" })).toMatchObject({ problem: expect.stringContaining("not found") })
    // A folder inside the project whose name starts with two dots is still inside the project.
    const dotted = path.join(project, "..tools")
    const dottedExe = path.join(dotted, "archcreator.exe")
    expect(ArchCreatorPlugin.resolveExecutable(project, { PATH: dotted, PATHEXT: ".exe" }, (p) => p === dottedExe))
      .toMatchObject({ problem: expect.stringContaining("not found") })
    expect(ArchCreatorPlugin.resolveExecutable(project, { ARCHCREATOR_BIN: dottedExe }, (p) => p === dottedExe))
      .toMatchObject({ problem: expect.stringContaining("inside the project") })
    // The parent folder, and a sibling, are outside.
    const sibling = path.join(path.dirname(project), "ecu-tools", "archcreator.exe")
    expect(ArchCreatorPlugin.resolveExecutable(project, { ARCHCREATOR_BIN: sibling }, () => true)).toEqual({ path: sibling })
    expect(resolve({})).toMatchObject({ problem: expect.stringContaining("not found") })
    // A relative PATH entry is resolved once, against the CLI's working directory, and that path is what runs.
    const relExe = path.resolve("tools-rel", process.platform === "win32" ? "archcreator.exe" : "archcreator")
    expect(ArchCreatorPlugin.resolveExecutable(project, { PATH: "tools-rel", PATHEXT: ".exe" }, (p) => p === relExe)).toEqual({ path: relExe })
    // A quoted entry, as Windows allows it, is the folder without the quotes; quoting a project folder changes nothing.
    expect(resolve({ PATH: `"${tools}"`, PATHEXT: ".exe" })).toEqual({ path: exe })
    expect(resolve({ PATH: `"${project}"`, PATHEXT: ".exe;.cmd" })).toMatchObject({ problem: expect.stringContaining("not found") })
  })

  test("one output folder per session id, even where the ids differ only in characters a folder cannot hold", () => {
    expect(ArchCreatorPlugin.sessionFolder("ses_abc-12")).toBe("ses_abc-12")
    const folders = ["ses_demo:a", "ses_demo_a", "ses_demo/a", "ses_demo?a", "ses_DEMO_a", "ses_Demo_A", "ses_demo_a.", "ses_demo_a ", "nul", "NUL", "com1", "", ".", "..",
      String.fromCharCode(0x73, 0xd800), String.fromCharCode(0x73, 0xd801)]
      .map(ArchCreatorPlugin.sessionFolder)
    // Distinct even where the file system ignores case.
    expect(new Set(folders.map((f) => f.toLowerCase())).size).toBe(folders.length)
    for (const folder of folders) {
      expect(folder).toMatch(/^[a-z0-9_~-]+$/)
      expect([".", ".."]).not.toContain(folder)
    }
  })

  test("accepts only an archcreator summary", () => {
    expect(ArchCreatorPlugin.parseSummary('{"schema":"archcreator.summary/1","module":"M"}')).toEqual({
      schema: "archcreator.summary/1",
      module: "M",
    })
    expect(ArchCreatorPlugin.parseSummary('{"schema":"other/1"}')).toBeUndefined()
    expect(ArchCreatorPlugin.parseSummary("not json")).toBeUndefined()
  })
})

// A stand-in for archcreator: records its arguments and writes the summary FAKE_MODE asks for.
const FAKE = `
const fs = require("fs"), path = require("path")
const args = process.argv.slice(2)
// Recorded next to the repository (--repo): the plugin no longer runs archcreator inside it.
fs.writeFileSync(path.join(args[args.indexOf("--repo") + 1], "argv.json"), JSON.stringify(args))
fs.writeFileSync(path.join(args[args.indexOf("--repo") + 1], "cwd.txt"), process.cwd())
const mode = process.env.FAKE_ARCHCREATOR_MODE || "ok"
if (args[0] === "list-modules") { process.stdout.write('{"schema":"archcreator.modules/1","scopes":[]}\\n'); process.exit(0) }
const out = args[args.indexOf("--out") + 1], mod = args[args.indexOf("--module") + 1]
if (mode === "crash") { process.stderr.write("Traceback: boom\\n"); process.exit(1) }
fs.mkdirSync(out, { recursive: true })
const code = mode === "usage" ? 2 : 1
const summary = mode === "usage"
  ? { schema: "archcreator.summary/1", module: mod, exit_code: 2, error: "no " + mod + ".c in App folder" }
  : { schema: "archcreator.summary/1", module: mod, exit_code: code, error: null, finding_counts: { total: 3 },
      analyzed_modules: [mod].concat(args.includes("--context") ? args[args.indexOf("--context") + 1].split(",") : []).sort() }
fs.writeFileSync(path.join(out, "summary.json"), JSON.stringify(summary))
process.exit(code)
`

const sessionID = Session.ID.make("ses_arch_creator_test")
const call = (name: string, input: unknown) => ({
  sessionID,
  ...toolIdentity,
  call: { type: "tool-call" as const, id: `call-${name}`, name, input },
})

const noRegistration = () => Effect.succeed({ dispose: Effect.void })

const withPlugin = <A, E, R>(
  directory: string,
  command: readonly string[],
  body: (registry: Tool.Interface) => Effect.Effect<A, E, R>,
  assertions: Permission.AssertInput[] = [],
  sessionDirectory = directory,
) => {
  const node = makeLocationNode({
    name: "test/arch-creator-plugin",
    layer: Layer.effectDiscard(
      registerToolPlugin(ArchCreatorPlugin.makePlugin({ command }), {
        agent: { get: () => Effect.die("unused"), list: () => Effect.die("unused"), transform: noRegistration, reload: () => Effect.void },
        command: { list: () => Effect.die("unused"), transform: noRegistration, reload: () => Effect.void },
      } as never),
    ),
    deps: [Tool.node, Location.node, FileAccess.node, Permission.node, AppProcess.node, FSUtil.node],
  })
  return Effect.gen(function* () {
    return yield* body(yield* Tool.Service)
  }).pipe(
    Effect.provide(
      AppNodeBuilder.build(LayerNode.group([Tool.node, node]), [
        Location.node.replace(
          Layer.succeed(
            Location.Service,
            Location.Service.of(
              location(
                { directory: AbsolutePath.make(sessionDirectory) },
                { projectDirectory: AbsolutePath.make(directory) },
              ),
            ),
          ),
        ),
        Permission.node.replace(permissionLayer({ assert: (input) => Effect.sync(() => void assertions.push(input)) })),
      ]),
    ),
  )
}

const text = (result: { content?: ReadonlyArray<{ type: string; text?: string }> }) =>
  result.content?.map((part) => (part.type === "text" ? part.text : "")).join("") ?? ""

const setup = (mode: string) =>
  Effect.gen(function* () {
    const tmp = yield* tmpdirScoped()
    const fake = path.join(tmp.path, "fake-archcreator.cjs")
    yield* Effect.promise(() => Bun.write(fake, FAKE))
    process.env.FAKE_ARCHCREATOR_MODE = mode
    return { dir: tmp.path, command: [process.execPath, fake] as const }
  })

const argv = (dir: string) => Effect.promise(async () => JSON.parse(await fs.readFile(path.join(dir, "argv.json"), "utf8")))

describe("archcreator_analyze", () => {
  it.live("runs archcreator in the project and returns its summary with the exit code", () =>
    Effect.gen(function* () {
      const { dir, command } = yield* setup("findings")
      // A profile inside the project is not used: profiles are central and archcreator picks the matching one.
      yield* Effect.promise(() => Bun.write(path.join(dir, ".flexcli", "archcreator.yaml"), "profile_version: 1\n"))
      const assertions: Permission.AssertInput[] = []
      const result = yield* withPlugin(dir, command, (registry) =>
        executeTool(registry, call(ArchCreatorPlugin.analyzeTool, { module: "DCDC_FSM" })), assertions)
      expect(result.status).toBe("completed")
      const outDir = path.join(dir, ".flexcli", "archcreator", "ses_arch_creator_test", "DCDC_FSM").split(path.sep).join("/")
      expect(result.output).toEqual({ exitCode: 1, outDir })
      expect(text(result)).toContain(`Full outputs: ${outDir}/`)
      expect(text(result)).toContain("archcreator exit 1 (findings at or above --fail-on) for module DCDC_FSM.")
      expect(text(result)).toContain('"finding_counts"')
      expect(yield* argv(dir)).toEqual([
        "analyze", "--repo", dir, "--module", "DCDC_FSM",
        "--out", path.join(dir, ".flexcli", "archcreator", "ses_arch_creator_test", "DCDC_FSM"),
      ])
      expect(assertions.map((a) => [a.action, a.resources, a.save])).toEqual([[ArchCreatorPlugin.analyzeTool, ["DCDC_FSM"], ["DCDC_FSM"]]])
      // archcreator does not run inside the repository: a wrapper there could not be picked up.
      const cwd = yield* Effect.promise(() => fs.readFile(path.join(dir, "cwd.txt"), "utf8"))
      expect(cwd.toLowerCase()).toBe(path.dirname(command[0]).toLowerCase())
    }),
  )

  it.live("writes to the project root and returns that folder when the session starts in a subdirectory", () =>
    Effect.gen(function* () {
      const { dir, command } = yield* setup("findings")
      const sub = path.join(dir, "software", "source")
      yield* Effect.promise(() => fs.mkdir(sub, { recursive: true }))
      const result = yield* withPlugin(dir, command, (registry) =>
        executeTool(registry, call(ArchCreatorPlugin.analyzeTool, { module: "DCDC_FSM" })), [], sub)
      expect(result.status).toBe("completed")
      const out = path.join(dir, ".flexcli", "archcreator", "ses_arch_creator_test", "DCDC_FSM")
      expect(result.output).toEqual({ exitCode: 1, outDir: out.split(path.sep).join("/") })
      expect(yield* Effect.promise(() => fs.stat(path.join(out, "summary.json")).then((s) => s.isFile()))).toBe(true)
      expect(yield* argv(dir)).toEqual(["analyze", "--repo", dir, "--module", "DCDC_FSM", "--out", out])
    }),
  )

  it.live("reports archcreator's own usage error from its summary", () =>
    Effect.gen(function* () {
      const { dir, command } = yield* setup("usage")
      const result = yield* withPlugin(dir, command, (registry) =>
        executeTool(registry, call(ArchCreatorPlugin.analyzeTool, { module: "Nope" })))
      expect(result.status).toBe("completed")
      expect(result.output?.exitCode).toBe(2)
      expect(text(result)).toContain("no Nope.c in App folder")
      expect(yield* argv(dir)).not.toContain("--profile")
    }),
  )

  it.live("passes the context modules through and tells the model what was parsed", () =>
    Effect.gen(function* () {
      const { dir, command } = yield* setup("findings")
      const result = yield* withPlugin(dir, command, (registry) =>
        executeTool(registry, call(ArchCreatorPlugin.analyzeTool, { module: "SEV", context: ["DCDC_FSM", "Error_Check"] })))
      expect(result.status).toBe("completed")
      expect(yield* argv(dir)).toEqual([
        "analyze", "--repo", dir, "--module", "SEV", "--out", path.join(dir, ".flexcli", "archcreator", "ses_arch_creator_test", "SEV"),
        "--context", "DCDC_FSM,Error_Check",
      ])
      expect(text(result)).toContain("Parsed modules: DCDC_FSM, Error_Check, SEV.")
      const bad = yield* withPlugin(dir, command, (registry) =>
        executeTool(registry, call(ArchCreatorPlugin.analyzeTool, { module: "SEV", context: ["all", "SEV"] })))
      expect(bad.status).toBe("error")
    }),
  )

  it.live("passes an explicitly named profile through", () =>
    Effect.gen(function* () {
      const { dir, command } = yield* setup("findings")
      yield* Effect.promise(() => Bun.write(path.join(dir, "override.yaml"), "profile_version: 1\n"))
      const assertions: Permission.AssertInput[] = []
      yield* withPlugin(dir, command, (registry) =>
        executeTool(registry, call(ArchCreatorPlugin.analyzeTool, { module: "SEV", profile: "override.yaml" })), assertions)
      expect((yield* argv(dir)).slice(-2)).toEqual(["--profile", path.join(dir, "override.yaml")])
      expect(assertions[0]?.resources).toEqual(["SEV", ArchCreatorPlugin.PROFILE_RESOURCE + path.join(dir, "override.yaml")])
      expect(assertions[0]?.save).toEqual([ArchCreatorPlugin.PROFILE_RESOURCE + path.join(dir, "override.yaml")])
      const wild = yield* withPlugin(dir, command, (registry) =>
        executeTool(registry, call(ArchCreatorPlugin.analyzeTool, { module: "SEV", profile: "over*.yaml" })))
      expect(wild.status).toBe("error")
      expect(JSON.stringify(wild.error)).toContain("must not contain")
    }),
  )

  it.live("never reuses a stale summary when archcreator crashes", () =>
    Effect.gen(function* () {
      const { dir, command } = yield* setup("crash")
      const stale = path.join(dir, ".flexcli", "archcreator", "ses_arch_creator_test", "SEV", "summary.json")
      yield* Effect.promise(() => Bun.write(stale, '{"schema":"archcreator.summary/1","module":"SEV","exit_code":0}'))
      const result = yield* withPlugin(dir, command, (registry) =>
        executeTool(registry, call(ArchCreatorPlugin.analyzeTool, { module: "SEV" })))
      expect(result.status).toBe("error")
      expect(JSON.stringify(result.error)).toContain("without a summary")
      expect(JSON.stringify(result.error)).toContain("Traceback: boom")
      expect(yield* Effect.promise(() => Bun.file(stale).exists())).toBe(false)
    }),
  )

  it.live("rejects a module name that is not a C identifier without starting archcreator", () =>
    Effect.gen(function* () {
      const { dir, command } = yield* setup("findings")
      for (const module of ["a;b", "../x", "a b", ""]) {
        const result = yield* withPlugin(dir, command, (registry) =>
          executeTool(registry, call(ArchCreatorPlugin.analyzeTool, { module })))
        expect(result.status).toBe("error")
      }
      expect(yield* Effect.promise(() => Bun.file(path.join(dir, "argv.json")).exists())).toBe(false)
    }),
  )

  it.live("names the install step when archcreator cannot be started", () =>
    Effect.gen(function* () {
      const tmp = yield* tmpdirScoped()
      // Outside the project (its parent is the system temp folder); a path inside it would be refused instead.
      const missing = path.join(path.dirname(tmp.path), `missing-archcreator-${path.basename(tmp.path)}.exe`)
      const result = yield* withPlugin(tmp.path, [missing], (registry) =>
        executeTool(registry, call(ArchCreatorPlugin.analyzeTool, { module: "SEV" })))
      expect(result.status).toBe("error")
      expect(JSON.stringify(result.error)).toContain("ARCHCREATOR_BIN")
      const planted = yield* withPlugin(tmp.path, [path.join(tmp.path, "archcreator.cmd")], (registry) =>
        executeTool(registry, call(ArchCreatorPlugin.analyzeTool, { module: "SEV" })))
      expect(planted.status).toBe("error")
      expect(JSON.stringify(planted.error)).toContain("inside the project directory")
      // A link outside the project that points into it is inside too.
      const inside = path.join(tmp.path, "tools")
      yield* Effect.promise(() => fs.mkdir(inside, { recursive: true }))
      yield* Effect.promise(() => Bun.write(path.join(inside, "archcreator.cmd"), "@echo planted\r\n"))
      const link = path.join(path.dirname(tmp.path), `link-${path.basename(tmp.path)}`)
      const linked = yield* Effect.promise(() =>
        fs.symlink(inside, link, "junction").then(() => true, () => false))
      if (linked) {
        const viaLink = yield* withPlugin(tmp.path, [path.join(link, "archcreator.cmd")], (registry) =>
          executeTool(registry, call(ArchCreatorPlugin.analyzeTool, { module: "SEV" })))
        expect(viaLink.status).toBe("error")
        expect(JSON.stringify(viaLink.error)).toContain("inside the project directory")
        yield* Effect.promise(() => fs.rm(link, { recursive: true, force: true }))
      }
    }),
  )

  it.live("archcreator_modules returns archcreator's module list", () =>
    Effect.gen(function* () {
      const { dir, command } = yield* setup("ok")
      const result = yield* withPlugin(dir, command, (registry) =>
        executeTool(registry, call(ArchCreatorPlugin.modulesTool, {})))
      expect(result.status).toBe("completed")
      expect(text(result)).toContain('"schema":"archcreator.modules/1"')
      expect(yield* argv(dir)).toEqual(["list-modules", "--repo", dir, "--json"])
    }),
  )
})
