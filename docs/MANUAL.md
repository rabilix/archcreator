# archcreator manual

How to use archcreator day to day. The README is the reference (every option,
every output field); this manual follows the work: install, first run, reading
the result, widening the view, scanning the repository, accepting what stays,
gating in CI, comparing with the design, and the copcli agent. Commands are
written for Windows `cmd.exe` and the 100554 repository; adapt paths for other
projects.

---

## 1. What archcreator is, and is not

archcreator reads C sources with libclang and writes an **as-built model** of a
module: what it exports, what it uses (calls, globals, macros, signals), who
uses it, how complex its functions are. It checks that model against **rules**
(naming, complexity, interface bypass, build membership, signal ownership,
design conformance, cycles, layers, unused exports) and writes **findings**
with evidence (`file:line`) and a stable fingerprint.

Three properties shape everything else:

- **Deterministic.** The same sources, profile and options give byte-identical
  outputs. No model, no network, no randomness. Two people running it on the
  same commit get the same files.
- **It parses only what you tell it.** A run covers the module you name plus
  the modules you pass as context (or everything with `--all`). Callers,
  signal producers and consumers, cycles and unused exports are known among
  those modules only, and every output says which ones they were.
- **It never changes anything.** Not the sources, not the profile, not the
  design model, not the baseline. Corrections happen in the code or, by a
  human decision, in the profile or the baseline.

---

## 2. Install

Two forms, same results byte for byte:

| Form | Needs | Steps |
|---|---|---|
| `archcreator-<v>-win64.zip` | nothing | unzip; `archcreator\archcreator.exe` is the tool, `archcreator\profiles\` the shipped profiles |
| `archcreator-<v>-py3-none-any.whl` | Python 3.11+ | `pip install archcreator-<v>-py3-none-any.whl` |

Then:

```bat
set "ARCHCREATOR_PROFILES=\\server\share\archcreator\profiles"   :: central profiles; optional with the zip
archcreator version --json
```

`version --json` prints the tool version and the schema versions of its
outputs; front ends check it.

**Profiles** describe one target repository: include paths, defines, the
build list, the signal pool, the design model, rule thresholds. They are
central, not in the project. archcreator picks the one whose `match` names the
repository (git remote or folder name) from `ARCHCREATOR_PROFILES`, else from the
profiles shipped with it. `profiles/100554.yaml` matches
`bitbucket-flex/100554_mc_appl`. You never need `--profile` for 100554; it is
there to override.

---

## 3. First run

From anywhere:

```bat
archcreator list-modules --repo F:\Projekte\100554_BMW_PCU48\100554_mc_appl
```

lists the scope (`software/source/flex`, 48 modules) and marks the five that
are not in the build. Then:

```bat
archcreator analyze --repo F:\Projekte\100554_BMW_PCU48\100554_mc_appl --module DCDC_FSM
```

takes about two seconds and prints `archcreator: wrote archcreator-out\DCDC_FSM (exit 0)`.
Pick the output folder with `--out` if the current directory is not where you
want it.

What is in the folder:

| File | Read it when |
|---|---|
| `report.md` | always first: one page with the module's facts, the findings table, provided interface, dependencies, signals, metrics, and the blind spots |
| `summary.json` | you need the result in a script or a front end: exit code, counts, the first findings |
| `findings.json` | you work through all findings, or you draft a baseline |
| `model.json` | you ask "who calls / what does it use / which signals": every edge with evidence |
| `design_compare.json` | the design comparison ran (profile has `design`) |
| `manifest.json` | you need provenance: commit, profile hash, baseline hash, design hash, parse coverage, output hashes |
| `ir.json`, `run_log.json` | something went wrong; `run_log.json` has the compile arguments and timings |

The **exit code** is the verdict:

| Code | Meaning | What to do |
|---|---|---|
| 0 | ran, no finding reached `--fail-on` (or no `--fail-on`) | read the report |
| 1 | a finding nobody has accepted reaches `--fail-on` | fix it, or accept it in the baseline |
| 2 | usage, profile, module or design-model error | the message says what; `summary.json` has it too |
| 3 | parse coverage below the profile gate | `report.md`, section *Parse results*: include paths or defines missing |
| 4 | accounting check failed | archcreator's own consistency check; report it with `run_log.json` and `ir.json` |
| 5 | internal error | a bug or a broken installation; the traceback is on stderr |

Exit 2 to 5 still write `summary.json` with the reason in `error`.

---

## 4. Reading the report

The head of `report.md` for `DCDC_FSM`:

```
| Module | `DCDC_FSM` (c_module) |
| Files | `.../DCDC_FSM.c`, `.../DCDC_FSM.h` |
| Scope | `software/source/flex` |
| Analyzed modules | 1: `DCDC_FSM` (callers, producers and consumers are known among these only) |
| Profile | `100554-release` (sha256 ...) |
| Baseline | `100554.baseline.yaml` (...): 0 accepted, 0 regressed, stale entries not checked (partial context) |
| Design model | `100554_BMW_DCDC_GEN3.qea` (...): module found |
| Parse coverage | 1/1 units without errors in App files (100%) |
| Accounting check | PASS: ... |
```

Then the sections, in the order you usually need them:

1. **Findings** — status, severity, rule, location, symbol, message,
   fingerprint. Accepted findings sort last. See section 7 for what each rule
   means.
2. **Parse results** — only interesting when not clean.
3. **Provided interface** — exported functions and globals, where defined,
   where declared. A function with no "declared in" is exported but not in any
   header: a bypass waiting to happen.
4. **Design comparison** — design vs code, see section 9.
5. **Signals** — every signal the module touches, its own access, and the
   writers and readers among the analyzed modules.
6. **Outgoing / incoming dependencies** — per module, per symbol, with the
   count of uses, the macro they went through, and the first location.
7. **Unresolved calls** — function-pointer calls; archcreator does not guess
   their targets.
8. **Function metrics** — CCN, nesting, NLOC, parameters per function.
9. **Known blind spots** — what archcreator cannot see; read it once.

---

## 5. Widening the view: context

A run alone knows the module from the inside. To see who calls it and who
produces its signals, name the other modules:

```bat
archcreator analyze --repo ... --module DCDC_FSM --context Error_Check,SEV,HCAN_TxRx
archcreator analyze --repo ... --module DCDC_FSM --context all
```

Cost: about half a second per module parsed; `all` is the 43 in-build modules
of `flex/`, about 35 s. With context the report's incoming dependencies,
signal writers and readers, and INTERFACE-001 for uses *of* the module become
visible, and the baseline's stale check runs (it needs a complete run).

A module in the build that was not parsed appears as `c_module_unparsed`: its
headers are known, its body is not. A symbol declared only by a prototype in
the using source, with no parsed definition, goes to an `unknown:` node; the
use is still reported.

---

## 6. The whole repository: scan

```bat
archcreator scan --repo ... --modules DCDC_FSM,Error_Check,SEV --out archcreator-out\scan
archcreator scan --repo ... --all --out archcreator-out\scan
```

One parse, then one output folder per module (exactly what `analyze` with the
same context writes) and, next to them:

| File | Content |
|---|---|
| `scan_report.md` | modules table with exit codes, repository-level findings (cycles, layers, unused exports, unbuilt sources), layers, the module graph |
| `graph.json` | nodes (modules, with kind and layer) and aggregated edges with kinds, counts and first evidence per kind |
| `scan_findings.json` | all findings merged by fingerprint (BUILD-001 once) plus the scan rules |
| `scan_summary.json` | exit code, per-module codes and counts, graph size, first 30 findings |

The exit code is the worst module code or the gate over the merged findings.
The graph covers the parsed modules: a cycle through a module you did not name
is not visible, so a gate on cycles belongs on `--all`.

What `scan --all` shows on 100554 today: 43 modules, 100 % parse coverage,
one dependency cycle group of ten modules across `appl` and `system`
(`appl`, `AplDiag_Flex`, `AplFlex`, `DCDC_Control`, `DCDC_FSM`, `Error_Check`,
`NvM`, `ad_values`, `comm_error_handler`, `temp`), ten signals with several
writers, 148 functions the design provides that the code does not export, 199
the code exports that the design does not provide, 350 exports no analyzed
module uses (most are OS hooks and BSW callbacks: the `export_exempt` list in
the profile is a first proposal), 672 naming findings, mostly in driver and
system code. That inventory is what the baseline is for.

---

## 7. What the findings mean

| Rule | It says | What to do |
|---|---|---|
| NAMING-001 | an exported function or global does not start with `<Module>_` or an alias from the profile | rename, add the alias (`modules.prefix_aliases`), or accept |
| COMPLEXITY-001 | a function exceeds a threshold (CCN, nesting, lines, parameters) | refactor, or accept with a `measured_max` so growth is caught |
| INTERFACE-001 | a module uses another's symbol that the owner declares in no header of its own (or the use goes through the user's own prototype) | declare it in the owner's header, or stop using it |
| BUILD-001 | a `.c` in the scope is not in the build list | delete it, or add it to the makefile |
| SIGNAL-001 | the module reads a signal no analyzed module writes | run with the producers in context; if none exists, the signal is produced by code outside the scope (drivers) or stale |
| SIGNAL-002 | a signal the module writes is written by other modules too | decide the owner; the others stop writing |
| DESIGN-000 | the module has no component in the design model | add it to the design, or it is not an architectural module |
| DESIGN-001 | the design provides a function the code does not export (or only declares) | implement, rename, or remove from the design |
| DESIGN-002 | the code exports a function the design does not provide | add to the design, or make it static |
| DESIGN-003 | the design requires a function interface the code never uses | remove from the design, or the code is missing a call |
| DESIGN-004 | the code uses a signal or an App symbol the design does not require | add to the design, or stop using it |
| DESIGN-005 | the design requires a signal the code never accesses | as DESIGN-003 |
| DESIGN-006 | a provided function is matched only through a prefix alias | name it consistently, or keep the alias |
| ARCH-001 | a dependency cycle between modules (calls, function addresses, globals) | break one edge; the message shows a concrete path and the whole group |
| LAYER-001 | an edge from one layer to another the profile's `layers.allow` forbids | move the code, or change the layer rules |
| EXPORT-001 | an exported symbol no analyzed module uses | make it static, delete it, or add the pattern to `modules.export_exempt` (runnables, callbacks) |

Severities are the profile's; defaults are in the README.

---

## 8. Accepting what stays: the baseline

Legacy findings do not go away by being reported. The baseline records the
human decision per finding, next to the central profile
(`profiles/100554.baseline.yaml`, named by `baseline:` in the profile).

Workflow for a review round:

```bat
archcreator scan --repo ... --all --out archcreator-out\scan
archcreator baseline-draft --in archcreator-out\scan\DCDC_FSM --out draft.yaml
```

The draft lists every finding of that run with `reason: TODO`,
`approved_by: TODO`, `date: TODO`, and for numeric findings the current value
as `measured_max`. Keep the entries you accept, fill in the three fields,
delete the rest, and merge them into the central baseline:

```yaml
baseline_version: 1
accepted:
  - fingerprint: 3f1c0d9a7b2e45c6d8e1
    rule: COMPLEXITY-001
    module: Error_Check
    symbol: EC_Check_Power_Cycle
    reason: "legacy state machine, refactoring planned in 100554-268001"
    approved_by: "R. Mokbel"
    date: 2026-10-18
    measured_max: 34
```

archcreator refuses the file while a placeholder is left, a date is not
`YYYY-MM-DD`, a fingerprint is malformed or listed twice, or the rule does not
match the finding. From then on every finding has a **status**:

- `new` — not in the baseline; the only status `--fail-on` counts, together
  with
- `regressed` — accepted, but the measured value grew past `measured_max`;
- `accepted` — stays in every output, sorted last, with reason, approver and
  date in the report.

Entries whose finding no longer occurs are listed as **stale** so the file can
be cleaned; that check runs only when the run was complete (`--context all`
or `scan --all`), because a partial run cannot tell "gone" from "not parsed".

Nothing is accepted on anyone's behalf: `100554.baseline.yaml` is empty until
the first round.

---

## 9. The design comparison

With `design:` in the profile (100554: `100554_BMW_DCDC_GEN3.qea` at the
repository root, components below
`DCDC_Shared_Model/Component/MCU/Flex/APPL`), every `analyze` and `scan` reads
the Enterprise Architect model straight from the `.qea` file — read-only, no
EA installation — and compares:

- the component's **provided interfaces** (the operations of their
  `«Function»` interfaces) with the code's exported functions, exactly first,
  then through the module's prefix aliases (`DCDC_Safety_Functions_Init` in the
  design is `DCDC_SF_Init` in the code);
- its **required signal interfaces** with the signals the code accesses;
- its **required function interfaces** (RTE calls and the like) with the
  symbols and accessor macros the code uses.

`design_compare.json` holds the full match tables; the report has a *Design
comparison* section; the DESIGN findings join the others, so they can be
accepted and gated like any finding. `--design <file>` points at another
model, `--no-design` skips the comparison. The manifest records the model's
file name and SHA-256.

Not compared: state machines (their packages differ per module in this model;
needs a mapping decision), types, sequences.

---

## 10. Into Enterprise Architect: the XMI export

```bat
archcreator analyze --repo ... --module SEV --context all --xmi
archcreator export-xmi --in archcreator-out\SEV
```

writes `model.xmi` (XMI 2.1 with EA's extensions, for EA 16): one package
`SEV (as-built)` with the component, its provided interfaces, one package per
other module with the interfaces used from it, the signals, Usage connectors
with the evidence in their notes, and a component diagram. Import: open a
**copy** of the model or a scratch package, select it, *Publish > Import > XMI
File*. Different modules never share an element. The design packages are
never written to.

archcreator writes an Enterprise Architect model only through EA's Automation
Interface (`ea-apply`, in development), only
into a copy it made itself or you named, never into a protected model (the
profile's design model, `--design`, the model it copies from, any EA model in the
repository), and never deletes anything. `analyze`, `scan`, `export-xmi` and the
design comparison never write a model.

Checked against EA 16.0 (build 1605): every element, connector, stereotype, tagged
value and diagram object arrives, with the GUIDs of the file. Importing a module
whose package already exists makes EA open its *Import Package from Native/XMI
File* dialog: answer it there, or delete the old `<module> (as-built)` package
first and import again (the result is the same; `tools/ea_verify.py` does the
latter, see `docs/EA_VERIFY.md`).

EA's own compare cannot pair as-built with design elements (different GUIDs);
the comparison lives in archcreator (section 9), not in EA.

---

## 11. In CI

The gate is archcreator's exit code, never a front end:

```bat
archcreator scan --repo %WORKSPACE% --all --out archcreator-out\scan --fail-on major
```

- exit 1 fails the build on a `new` or `regressed` finding of at least `major`;
  accepted ones do not count;
- exit 2 means the profile or the checkout is wrong (missing Diab include
  folders are the usual cause on a new agent: the profile's
  `system_include_dirs` point at the Wind River installation);
- exit 3 means the sources did not parse; keep the gate
  (`gate.min_parse_coverage`) honest rather than lowering it;
- archive `archcreator-out\scan` as the build artifact: `scan_summary.json` for
  dashboards, `scan_report.md` for people, the manifests for provenance.

Runs are reproducible from a manifest: commit, dirty flag, profile hash,
baseline hash, design hash, analyzer versions, parse coverage, output hashes.
Canonical files carry no timestamps and no absolute paths; `run_log.json` does
and is for diagnosis only.

---

## 12. From copcli: the ArchCreator agent

`/archcreator DCDC_FSM` in copcli switches to the ArchCreator agent, which runs
archcreator and explains the result: it quotes findings with their evidence, reads
the code there, answers "who uses this?" from `model.json`, and suggests fixes
as text. It never creates, re-rates or drops a finding and cannot edit
sources, profiles, rules or archcreator's outputs.

- It starts without context (fast). Ask "who calls it?" or "who writes that
  signal?" and it runs again with the modules in question as context, saying
  which modules the run parsed.
- A profile override named by the agent asks you first; the central profile
  folders are read without asking; anything else outside the project asks.
- archcreator must be installed outside the repository (`ARCHCREATOR_BIN` absolute,
  or on `PATH`); an `archcreator` inside the checkout is never run.
- What the agent reads goes to M365 Copilot like any tool result. For a run
  that sends nothing anywhere, use archcreator directly.

Details: `WINDOWS-CMD.md` in copcli (now flexcli), section *Architecture review of a C module*.

---

## 13. As an MCP server

`archcreator mcp` serves the analysis to any MCP client (Claude Code, flexcli, an
IDE) over stdin/stdout. There is nothing to install besides archcreator itself:
the server needs no SDK and makes no network connection.

```
archcreator mcp --repo F:/Projekte/100554_BMW_PCU48/100554_mc_appl --out-root F:/work/archcreator-out
```

The repository, the profile (`--profile`, else the central profile that matches
the repository), the scope (`--scope`) and the output root (`--out-root`) are
fixed when the server starts. A model that calls the tools can name only
modules and a few switches. It cannot pass a path, a profile or a command, and
it can read back only the fixed result files of modules it has analyzed.

| Tool | What it does |
|---|---|
| `archcreator_list_modules` | the modules in the scope and whether each is in the build |
| `archcreator_analyze` | `module`, optional `context` (names or `"all"`), optional `xmi`; returns the summary |
| `archcreator_scan` | `modules` (names) or `all: true`, optional `xmi`; returns the scan summary |
| `archcreator_read` | one result file (`report.md`, `findings.json`, `model.json`, `model.xmi`, ...) of an analyzed module, or of the scan when `module` is omitted |

A run that ends with exit code 2 to 5 comes back as an error result with the
reason in `error`; exit code 1 (unaccepted findings) is an ordinary result: read
`exit_code` and the finding counts in it. Results are written under
`<out-root>/modules/<module>` and `<out-root>/scan` (a scan's modules in
`<out-root>/scan/<module>`), so they stay available as files. Each run starts
from an empty folder: an earlier result of the same module is deleted first, but
only a folder that carries archcreator's own marker file (`.archcreator-output`)
or is empty, so an `--out-root` that holds other files is never wiped. Give the
server a folder of its own.

Claude Code (`.mcp.json` in the project, or `claude mcp add`):

```json
{
  "mcpServers": {
    "archcreator": {
      "command": "F:/Tools/archcreator/archcreator.exe",
      "args": ["mcp", "--repo", "F:/Projekte/100554_BMW_PCU48/100554_mc_appl",
               "--out-root", "F:/work/archcreator-out"]
    }
  }
}
```

flexcli (`flexcli.json`; its default request timeout is 5 seconds, too short for a
scan, so set a larger one):

```json
{
  "mcp": {
    "archcreator": {
      "type": "local",
      "command": ["F:/Tools/archcreator/archcreator.exe", "mcp",
                  "--repo", "F:/Projekte/100554_BMW_PCU48/100554_mc_appl",
                  "--out-root", "F:/work/archcreator-out"],
      "timeout": 180000
    }
  }
}
```

The protection covers what a model can do through the tools. It does not defend
against another process of the same user that changes the output folder while a
tool call is running (swapping a folder for a junction, say): such a process can
do anything to the folder anyway. Keep `--out-root` somewhere only you write to.

Everything a tool returns goes to the model behind the client. With M365 Copilot
that is the same upload as in section 12; for a run that sends nothing anywhere
use the command line. The server handles one request at a time and does not
report progress: `all: true` on a large scope can take a minute.

---

## 14. When something is off

| Symptom | Cause and fix |
|---|---|
| exit 2, "several central profiles match" | two profiles' `match` cover the repository; fix the `match` sections, or pass `--profile` |
| exit 2, "design model not found" | the `.qea` is not at the profile's path; `--no-design` for a run without it |
| exit 2, "module names must be unique within the scope" | two `.c` files with the same stem in the scope; archcreator's smallest unit is one `.c` plus its headers, so one has to be renamed or excluded |
| exit 3 after adding driver or system modules | Diab-specific constructs; the 100554 profile already carries the known shims (`-fbracket-depth`, implicit declarations, byte-swap builtins); new ones go into `compile.args` or `compile.defines` |
| many SIGNAL-001 "no analyzed module writes it" | the producers were not parsed (run with context or `--all`), or they live outside the scope (drivers write temperatures and voltages) |
| EXPORT-001 on `*_Init`, `*_Main`, `BswM_Cbk_*` | called from generated code outside the scope; add patterns to `modules.export_exempt` |
| a finding keeps coming back after a fix elsewhere | fingerprints are per module and symbol; check the `status` and the baseline's stale list on a complete run |
| INTERFACE-001 points at a macro | the use went through a macro of the using module; macros from generated code or the owner's own headers count as the interface |
| runs are slow | parse is ~0.5 s per module; name only the modules you need, keep `--all` for the gate |
| Windows-1252 characters in reports | archcreator keeps bytes as they are; the sources are not UTF-8, the reports are |

---

## 15. Files you may edit, and files you must not

| Edit | Never edit |
|---|---|
| `profiles/<project>.yaml` (with a review: it is the rule set) | anything under an output folder (`archcreator-out`, `.copcli/archcreator`) |
| `profiles/<project>.baseline.yaml` (with reason, approver, date) | the `.qea` through archcreator (it only reads it) |
| the sources, to fix a finding | `findings.json` to make a finding go away |
