# Concept: archcreator in copcli

Status: steps 1 and 2 implemented, not committed (2026-10-17). Target:
next-gen copcli (`next-gen/copilotcli/`).

- archcreator side: in this repo (`summary.json`, `list-modules --json`,
  `version --json`, `tests/test_contract.py`).
- copcli side: worktree `F:\Programme\CopilotCliGit\MsCopilotCli-archcreator`, branch
  `feature/archcreator` off `copcli_next_gen` (dd6a8d2). The main checkout and its
  uncommitted changes are untouched. Codex review per that repo's rules is
  still to do.

Deviations from the draft below are marked **(as built)**.

## 1. Goal

A copcli user can type `/archcreator DCDC_FSM` in a C repository and get the
architecture findings of that module, explained, with file:line evidence, and
can ask follow-up questions ("who calls this?", "why is this a bypass?").

archcreator stays a standalone, deterministic tool. copcli is a front end for it.

## 2. Trust boundary (the one rule that shapes everything)

| | archcreator | copcli agent (M365 Copilot) |
|---|---|---|
| Decides findings, severities, exit codes | yes | never |
| Builds the as-built model | yes | never |
| Explains findings, reads code around evidence, answers questions | no | yes |
| Suggests fixes | no | yes, as text only; never edits source |
| Suppresses, approves or changes rules | no (human, in the profile) | never |
| Network | none | Graph (Copilot) |

Consequences:

- The agent may only quote findings from archcreator output. It must not invent,
  re-rate or drop a finding, and must say so when a question is outside what
  archcreator measured (the blind-spot list in `report.md`).
- CI and release gates call `archcreator` directly, never copcli. The exit code of
  `archcreator analyze` is the gate; the LLM conversation is advice.
- Re-running `/archcreator` on the same commit gives the same findings; only the
  explanation text may vary.

## 3. Architecture

```
 user ──/archcreator DCDC_FSM──▶ copcli (Bun/TS)
                               │
                               │  ArchCreator agent (system prompt = method)
                               │    ├─ archcreator_analyze  ─┐  plugin tools,
                               │    ├─ archcreator_modules  ─┤  spawn archcreator with an
                               │    │                    │  argument array (no shell)
                               │    ├─ read/grep/glob  (built-in, source + outputs)
                               │    └─ edit: only .copcli/*
                               ▼                         ▼
                        M365 Copilot (Graph)      archcreator (Python, offline)
                                                    │ reads repo, profile
                                                    ▼
                                         .copcli/archcreator/<Module>/
                                           summary.json  (new, small)
                                           findings.json model.json
                                           report.md     manifest.json
```

## 4. Changes on the archcreator side

Small and backward compatible.

1. **`summary.json`** in every output folder, schema `archcreator.summary/1`.
   It is what the copcli tool returns to the model, so it must be short
   (a few KB even for `Error_Check`):
   ```json
   {
     "schema": "archcreator.summary/1",
     "module": "DCDC_FSM",
     "exit_code": 0,
     "target_commit": "39db4e93a…", "target_dirty": true,
     "parse_coverage": 1.0, "accounting_ok": true,
     "finding_counts": {"COMPLEXITY-001": 9, "BUILD-001": 5},
     "top_findings": [ {"rule_id": "…", "severity": "…", "symbol": "…",
                        "file": "…", "line": 0, "message": "…",
                        "fingerprint": "…"} ],
     "dependencies": {"outgoing": 187, "incoming": 53, "unresolved_calls": 1},
     "outputs": {"report": "report.md", "findings": "findings.json",
                 "model": "model.json"}
   }
   ```
   `top_findings` is capped (default 20), sorted by severity then rule then
   file. The full list stays in `findings.json`.
2. **`--json`** on `list-modules` (module name, file, in_build).
3. **`archcreator version --json`** so copcli can check compatibility
   (`summary` schema version, archcreator version).
4. Exit codes unchanged: 0 ok, 1 findings ≥ `--fail-on`, 2 usage, 3 parse
   gate, 4 accounting. On 2/3/4 the tool still writes `summary.json` when it
   can, with an `error` field.

## 5. Changes on the copcli side

One built-in plugin, modelled on C Debug (`packages/core/src/plugin/c-debug.ts`).

### 5.1 Files

| File | Content |
|---|---|
| `packages/core/src/plugin/arch-creator.ts` | plugin: agent, tools, command |
| `packages/core/src/plugin/arch-creator/method.txt` | system prompt (section 6) |
| `packages/core/src/plugin/internal.ts` | import + list entry, next to `CDebugPlugin` |
| `packages/core/test/plugin/arch-creator.test.ts` | tests (section 8) |
| `WINDOWS-CMD.md` | `/archcreator` section next to `/cdebug` **(as built:** next-gen documents agents there; `docs/install.html` has no C Debug section either, so it was left unchanged**)** |

### 5.2 Tools (via `ctx.tool.transform` → `editor.add`)

- **`archcreator_analyze`** — args: `module` (required), `profile` (optional),
  `fail_on` (optional, enum). Runs
  `archcreator analyze --repo <project dir> --module <m> [--profile <p>] --out .copcli/archcreator/<m>`
  as an argument array, cwd = project directory, timeout 5 min. Returns
  `summary.json` plus the exit code and the relative paths of the full
  outputs. Module names are validated against `^[A-Za-z0-9_]+$` before
  spawning.
- **`archcreator_modules`** — runs `archcreator list-modules --json`.

Dedicated tools instead of the shell: the shell in C Debug always asks, and a
fixed argument list cannot be turned into another command.

### 5.3 Agent `arch-creator` ("ArchCreator")

`mode: "primary"`, `system: METHOD`, permissions (last match wins), derived
from C Debug's `rules()`:

```ts
{ action: "*",               resource: "*", effect: "ask" },
...["read","grep","glob","question","skill"].map(a => ({ action: a, resource: "*", effect: "allow" })),
{ action: "archcreator_analyze", resource: "*", effect: "allow" },
{ action: "archcreator_modules", resource: "*", effect: "allow" },
{ action: "read", resource: "*.env",   effect: "ask" },
{ action: "read", resource: "*.env.*", effect: "ask" },
{ action: "edit", resource: "*",        effect: "deny" },
{ action: "edit", resource: ".copcli/*", effect: "allow" },   // notes only
{ action: "edit", resource: ".copcli/archcreator/*", effect: "deny" }, // never archcreator's outputs (added 2026-10-18)
{ action: "subagent",  resource: "*", effect: "deny" },
{ action: "webfetch",  resource: "*", effect: "deny" },
{ action: "websearch", resource: "*", effect: "deny" },
```

The agent cannot edit source, the profile or archcreator outputs other than its
own notes under `.copcli/`.

### 5.4 Command `/archcreator`

```
/archcreator <Module> [question]      analyze, then explain (default question:
                                  "summarize the findings by severity")
/archcreator                          ask which module; offers archcreator_modules
```

Same pattern as `/cdebug`: switch the session to the `arch-creator` agent, then
prompt it.

### 5.5 Locating archcreator

1. `ARCHCREATOR_BIN` environment variable (full path to `archcreator.exe` or a
   Python launcher), else
2. `archcreator` on `PATH`.

Profile: `profile` argument of the tool (a path outside the project goes
through copcli's external-directory approval), else none: archcreator picks the
matching central profile itself. **(As built, decision 3:** no
`.copcli/archcreator.yaml` default.**)** If archcreator
produces no `summary.json` with schema `archcreator.summary/1`, the tool fails with
archcreator's stderr and the install hint instead of guessing; a summary left by an
earlier run is deleted before every run. **(As built:** the tool checks the
summary schema instead of calling `version --json` first, one process start
less.**)**

## 6. Agent method (draft of `method.txt`)

```
You are the ArchCreator agent of CopCli. You explain architecture findings of a
C module. The findings come only from the archcreator tool; you never create,
re-rate, merge or drop a finding, and you never change rules or profiles.

Method:
1. Run archcreator_analyze for the module the user names. If it exits 2, 3 or 4,
   report the error and its fix from the summary; do not analyze yourself.
2. Report the counts per rule and severity, the parse coverage and the
   commit, exactly as in the summary.
3. For each finding you discuss, quote its rule, symbol and file:line, then
   read the code at that evidence and explain what it means. Mark your
   explanation as explanation, not as a finding.
4. For questions about dependencies, read model.json (outgoing, incoming,
   evidence); answer only from it and say when the answer is outside it
   (see the blind spots in report.md: type-only dependencies, disabled code,
   function pointers, external modules).
5. Fix suggestions are text. Corrections belong in the code or, with a human
   decision, in the rule profile.
```

## 7. Data that leaves the machine

archcreator sends nothing. The copcli agent sends to M365 Copilot (Graph) what it
reads: `summary.json`, parts of `report.md`/`model.json`, and the source lines
it opens at the evidence. That is code leaving the machine. It is acceptable
only where the project's M365 Copilot use is approved for that code (customer
contract, BMW data classification). This needs an explicit decision per
project, not a default.

Path without any upload: run `archcreator analyze` directly; `report.md` is the
readable result. **(As built:** no `--local` flag in copcli; a copcli command
always prompts the model.**)**

## 8. Tests

archcreator:
- `summary.json` schema test on fixture fx01: exact counts, capped list,
  byte-identical across runs, no absolute paths.
- `list-modules --json` and `version --json` contract tests.

copcli (`bun test`, `tsc --noEmit`):
- Agent registered with name, mode and the permission list above; `edit` on a
  source file denied, on `.copcli/x.md` allowed; webfetch/subagent denied.
- `archcreator_analyze` with a fake `ARCHCREATOR_BIN` (a small script that writes a
  canned `summary.json` and exits 0/1/2/3/4): exact argument array, cwd,
  output folder, error text for missing binary, rejected module name
  `a;b`, timeout.
- `/archcreator` switches agent and prompts, as in `c-debug.test.ts`.

End to end (manual, once): `/archcreator alpha` on `tests/fixtures/fx01`, and
`/archcreator DCDC_FSM` on 100554. The agent's answer must contain exactly the
findings of `findings.json`, no others.

## 9. Rollout

| Step | Repo | Content |
|---|---|---|
| 1 | archcreator | `summary.json`, `--json` flags, tests |
| 2 | copcli | plugin, method, tests, MANUAL/install docs (Codex review per that repo's rules) |
| 3 | both | manual end-to-end on fx01 and 100554 |
| 4 | archcreator | EA export: `analyze --xmi` / `export-xmi`, XMI 2.1 for EA 16 (implemented 2026-10-18; first import into EA still to verify) |
| 5 | archcreator | Done 2026-10-18 (see `archcreator_review_2026-10-18.md` section 8): baseline of accepted findings, signal-pool adapter, design comparison straight from the `.qea` (DESIGN findings in the ordinary findings, so `/archcreator` explains them like any other), `archcreator scan` |
| 6 | copcli | `archcreator_scan` tool and `/archcreator scan` for the repository view (graph, cycles, layers); not started |
| 7 | copcli | Done 2026-10-18: `archcreator_analyze` takes `context` (module names or `["all"]`, passed as `--context`), validates the names before anything runs, and tells the model which modules the run parsed; the method says to start without context and re-run with the modules in question for "who calls / who produces" questions, never presenting a partial run's "no caller" as a fact |

Ownership: step 1 here; step 2 in a copcli session on a feature branch off
`copcli_next_gen`, after its uncommitted `bun.lock` / `packages/cli/package.json`
changes are settled.

## 10. Decisions (2026-10-17)

1. Uploading archcreator output and code excerpts from 100554 to M365 Copilot:
   **allowed**.
2. Installation: **both**. A zip with `archcreator.exe` (PyInstaller; Python,
   libclang and the profiles inside) and a wheel. `packaging/build.py` builds
   both; their outputs are byte-identical to the development install.
3. Profiles: **central**. archcreator picks the profile whose `match` (git remote
   or repo folder name) names the target repository, from `ARCHCREATOR_PROFILES`
   or the shipped `profiles/`. The copcli plugin passes `--profile` only when a
   call names one.
4. Names `arch-creator` / `/archcreator`: kept.

## 11. Risks

- copcli next-gen is a large, moving fork; the plugin API (`define`,
  `ctx.tool.transform`) may change. Keep the plugin small and test-covered.
- The model may still paraphrase a finding into something stronger. The method
  forbids it; the end-to-end check in section 8 is the guard.
- Large modules (`Error_Check`: 456 outgoing edges) exceed what fits in a
  prompt; the agent must read `model.json` selectively, never whole.
- Two Python environments (archcreator needs libclang 18.1.1) on user machines;
  the frozen executable in decision 2 avoids that.
