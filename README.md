# archrev (MVP)

Deterministic architecture analysis of one C module. Given a repository and a
module name (the C file name without `.c`), archrev parses the App folder with
libclang and writes an as-built model of the module, rule findings and a review
report. No LLM, no network access, no randomness: identical inputs produce
byte-identical outputs.

## Install

Two distributions with identical results (verified byte for byte on fx01 and
100554):

| Distribution | Needs | Profiles |
|---|---|---|
| `archrev-<v>-win64.zip`: unpack, run `archrev\archrev.exe` | nothing (Python and libclang are inside) | `archrev\profiles\` (copied at build time) |
| `archrev-<v>-py3-none-any.whl`: `pip install archrev-<v>-py3-none-any.whl` | Python 3.11+, pip (pulls libclang, lizard, PyYAML) | set `ARCHREV_PROFILES` |

Build both with `python packaging/build.py` (after `pip install -e .[build]`);
they land in `dist/`.

Development:

```
python -m venv .venv
.venv\Scripts\python -m pip install -e .[dev]
```

Pinned analyzers: libclang 18.1.1, lizard 1.17.13, PyYAML 6.0.2.

## Usage

```
archrev analyze --repo <target repo> --module <Name> [--context A,B|all] [--profile <profile.yaml>] [--profiles-dir <dir>]
                [--scope <dir>]... [--out <dir>] [--fail-on info|minor|major|critical] [--xmi]
                [--baseline <accepted.yaml>] [--design <model.qea> | --no-design]
archrev scan    --repo <target repo> (--modules A,B | --all) [--out <dir>]   (same options as analyze; one parse)
archrev export-xmi --in <analyze output folder> [--out <file.xmi>]
archrev baseline-draft --in <analyze output folder> [--out <file.yaml>]
archrev list-modules --repo <target repo> [--profile <profile.yaml>] [--profiles-dir <dir>] [--scope <dir>]... [--json]
archrev version [--json]
```

Module names are letters, digits and `_` only.

Example (BMW PCU48):

```
archrev analyze --repo ..\100554_mc_appl --module DCDC_FSM
```

### Scope, modules and what a run parses

- The **scope** is a list of folders (profile `scope`, or `--scope`, repeatable)
  whose `.c` files are the modules: the project's own code and whatever
  delivered applications are built with it. Without a scope, the single
  folder named `App*` is used. The build list of the profile decides which of
  them are in the build; `.c` files in the scope but not in the build are
  `not_in_build` (BUILD-001) and never parsed.
- A **module** is one `.c` file plus the headers in the same directory whose
  name equals it or is a prefix of it followed by `_` (`SEV.h`, `SEV_Cfg.h` ->
  `SEV`); the longest match wins, `modules.header_owner` overrides. Other
  headers are header-only modules. Everything outside the scope is an EXTERNAL
  leaf, named after the file.
- A run **parses only what it is told**: `analyze` the module plus
  `--context A,B` (or `all`), `scan` the `--modules` named or `--all`. Callers,
  signal producers and consumers, cycles and unused exports are known among
  the parsed modules only; every report and summary lists the analyzed set
  (`analyzed_modules`), and an in-build module outside it has kind
  `c_module_unparsed` in the model. Parsing is the expensive part (about
  0.5 s per unit on 100554), so a single module with no context takes a
  second, `scan --all` over `software/source/flex` about half a minute.

### Outputs (`archrev-out/<Name>/` by default)

| File | Content | Canonical |
|---|---|---|
| `ir.json` | Parsed facts: declarations, functions, references, includes, parse diagnostics | yes |
| `model.json` | Module model: files, provided interface, typed outgoing/incoming dependencies with evidence, unresolved calls, metrics, accounting check | yes |
| `findings.json` | Rule findings with stable fingerprints | yes |
| `report.md` | Review report | yes |
| `summary.json` | Small digest for front ends (schema `archrev.summary/1`): exit code, commit, parse coverage, finding counts, first 20 findings, dependency counts | yes |
| `model.xmi` | Only with `--xmi`: the model as XMI 2.1 for Enterprise Architect 16 (see below) | yes |
| `design_compare.json` | Only with a design model: provided/required interfaces of the design matched against the code | yes |
| `manifest.json` | Versions, profile hash, target commit, parse coverage, output hashes | yes |
| `run_log.json` | Timings, absolute paths, compile arguments | no |

On any error exit `summary.json` is still written, with `error` set. A module
that is not in the build list is analyzed anyway (`target_in_build: false` in
the summary); BUILD-001 is attributed to the unbuilt file's own module, so its
fingerprint is the same whichever module is analyzed.

### Enterprise Architect export (XMI 2.1, EA 16)

`analyze --xmi` or `export-xmi --in <folder>` (from an existing `model.json` and
`manifest.json`; same bytes) writes one package `<Module> (as-built)`; what
appears depends on the context parsed with the module:

| Package | Content |
|---|---|
| `BlackBox` | Component `<Module>` «as-built», run info as tagged values (commit, archrev version, profile, parse coverage), diagram `<Module> (as-built)` |
| `Interfaces` | One Interface per provided function («Function», one operation) or global («Variable», one attribute), Realization from the component |
| `Context/<Other>` | Per other module a Component (stereotype `c_module`, `header_only`, `external`, `not_in_build`) and the Interfaces of the symbols the target uses from it («Function», «Variable», «Macro», «EnumConstant») |

Connectors: Usage per used symbol (stereotype = dependency kind; evidence
`file:line in function via macro` in the notes, count and first evidence as
tags), Usage from other modules to the provided interfaces they use, and one
Dependency «as-built» per other module and direction with the kinds counted.
Includes appear only in those Dependencies.

Import in EA 16: select the target package (a scratch package or a copy of the
model, never the design packages), *Publish > Import > XMI File*. IDs are EA
style UUIDv5, so importing the same module again replaces the earlier as-built
package. Types and signatures are not exported: archrev does not record them.
archrev never writes `.qea`/`.eap` files.

### Front ends

`summary.json`, `list-modules --json` (schema `archrev.modules/1`) and
`version --json` are the contract for front ends. copcli uses them through its
Arch Rev agent (`/archrev <Module>`); see `docs/copcli_integration_concept.md`.
The front end explains findings; the findings and the exit code are archrev's.

Canonical files contain no timestamps or absolute paths. GUIDs are UUIDv5 of
canonical IDs (`module:<path>`, `dep:<from>-><to>:<kind>:<symbol>:<usr>`), so
they stay stable across runs and line shifts, in sources and headers (macro
identities carry no offsets).

Dependency kinds: `include`, `call`, `fn_address`, `global_read`,
`global_write`, `global_addr`, `macro_use`, `enum_use`, and with a signal pool
`signal_read`, `signal_write`, `signal_test`, `signal_error_read`,
`signal_error_write`. Function-pointer calls
are listed under `unresolved_calls`. Each piece of evidence records whether the
use was expanded from a macro (`via_macro`, `via_macro_file`).

### Exit codes

| Code | Meaning |
|---|---|
| 0 | Success |
| 1 | A finding at or above `--fail-on` |
| 2 | Usage, profile or module-resolution error |
| 3 | Parse coverage below `gate.min_parse_coverage` |
| 4 | Accounting check failed (internal consistency error) |
| 5 | Internal error (a bug or a broken installation); never a finding |

On 2, 3, 4 and 5 `summary.json` carries the reason in `error`.

## Rules (MVP)

| Rule | Checks |
|---|---|
| NAMING-001 | Exported functions and globals start with `<Module>_` or an alias from `modules.prefix_aliases` |
| COMPLEXITY-001 | lizard CCN, nesting depth, NLOC and parameter count per function vs. `rules.complexity` |
| INTERFACE-001 | Another App module uses a symbol that is not declared in the owner's own headers (both directions). Uses expanded from a macro defined outside the App folder (e.g. generated RTE) or in the owner's own files count as interface use. |
| BUILD-001 | `.c` files in the App folder that are not in the build list |
| SIGNAL-001 | The module reads a signal no App module in the build writes (needs `signal_pool`) |
| SIGNAL-002 | A signal the module writes is written by other App modules too |
| DESIGN-000 | The module has no component in the design model (needs `design`) |
| DESIGN-001 | A provided function of the design is not exported by the code |
| DESIGN-002 | An exported function of the code is not provided in the design |
| DESIGN-003 | A required function interface of the design is not used by the code |
| DESIGN-004 | The code accesses a signal, or uses an App module's symbol, the design does not require |
| DESIGN-005 | A required signal of the design is not accessed by the code |
| DESIGN-006 | A provided function is matched only through a prefix alias (names differ) |
| ARCH-001 | `scan` only: dependency cycle between App modules (call, function address, global access) |
| LAYER-001 | `scan` only: an edge between layers the profile's `layers.allow` does not permit |
| EXPORT-001 | `scan` only: an exported function or global no parsed App module uses (`modules.export_exempt` for runnables and callbacks) |

Severities are configurable per rule in the profile.

## Signal pool

Projects that exchange data through generated accessor macros (`SP_Get_X()`,
`SP_Set_X(v)`, ...) have their real coupling in those macros, not in calls
between modules. The profile describes the accessors once:

```yaml
signal_pool:
  header: software/source/flex/system/signals.h
  accessors:                       # first match wins, so list the more specific patterns first
    - {macro: "SP_Set_{signal}_Errorflag", access: error_write}
    - {macro: "SP_Test_{signal}_Errorflag", access: error_read}
    - {macro: "SP_Get_{signal}", access: read}
    - {macro: "SP_Set_{signal}", access: write}
    - {macro: "SP_Test_{signal}", access: test}
```

A use of such a macro becomes a dependency of kind `signal_<access>` on the
node `signal:<name>` (kind `signal`), with the usual evidence. `model.json`
gets a `signals` section: for every signal the module touches, its own
accesses and the App modules that write and read it. The calls the macros
expand to stay as edges to the signal pool, attributed to the accessor. The
report has a Signals table; the XMI export a `Signals` package with one
Interface «Signal» per signal. Accesses: `read`, `write`, `test`,
`error_read`, `error_write`; `write` makes a module the producer.

## Repository scan

`archrev scan --modules A,B` (or `--all`) parses the named modules once and
writes each one's view into `<out>/<Module>/`, byte for byte what `analyze`
with the same context would write, plus the level above the modules:

| File | Content |
|---|---|
| `graph.json` | Schema `archrev.graph/1`: every module any view mentions (with its kind and layer) and the aggregated edges between modules with their kinds, counts and first evidence per kind |
| `scan_findings.json` | All modules' findings merged by fingerprint (so BUILD-001 appears once) plus ARCH-001, LAYER-001 and EXPORT-001 |
| `scan_summary.json` | Schema `archrev.scan_summary/1`: exit code, per-module exit codes and counts, graph size, the first 30 findings |
| `scan_report.md` | Modules table, repository-level findings, layers, the App module graph |

The exit code is the worst of the modules' codes and the gate over the merged
findings. The graph and the rules cover exactly the parsed modules: a cycle
through a module that was not named is not visible. A baseline applies to the
merged findings too; stale detection there covers entries of every analyzed
module and entries without `module`.

Layers are optional:

```yaml
layers:
  assign:                       # first matching glob (on the module's files) wins
    swc: ["software/source/flex/appl/swc/*"]
    appl: ["software/source/flex/appl/*.c", "software/source/flex/appl/*.h"]
    driver: ["software/source/flex/driver/*"]
  allow:                        # a layer may always use itself
    swc: [appl]
    appl: [swc, driver]
  edge_kinds: [call, fn_address, global_read, global_write, global_addr]   # default
```

## Design comparison (Enterprise Architect)

archrev reads the design model read-only, straight from the `.qea` file (it is
a SQLite database), without an EA installation, and compares it with the
as-built model. Nothing is ever written to the model; `.eap` files are not
supported.

```yaml
design:
  model: 100554_BMW_DCDC_GEN3.qea                        # relative to the repository
  component_root: DCDC_Shared_Model/Component/MCU/Flex/APPL   # where the App components live
  signal_packages: [Signals]                             # interfaces below such a package are signals
```

For the module archrev looks for the Component of that name in a `BlackBox`
package below `component_root`, its ProvidedInterface ports (the operations of
the classifier interface are the function names; a port without classifier
counts by its name) and its RequiredInterface ports (signal interfaces or
function interfaces such as RTE calls). The comparison yields the DESIGN
findings above, `design_compare.json` with the full match tables, and a
"Design comparison" section in the report. Provided names are matched exactly
first, then through the module's `prefix_aliases` (`DCDC_Safety_Functions_Init`
in the design, `DCDC_SF_Init` in the code: DESIGN-006). Required signals match
the signals of the `signal_pool`; required functions match any symbol the code
uses, including macros. DESIGN-004 for symbols covers App modules only: calls
into BSW are outside the design's scope.

`--design <file>` overrides the profile's model (keeping its component root),
`--no-design` skips the comparison. The manifest records the model's file name
and SHA-256.

Not compared yet: state machines. In the 100554 model the states of a module
live in differently named packages per module (`Dynamic View`, `Dynamic View
V2`, `StateManagement`), and the code side needs a per-module enum pattern;
both have to be configured before a comparison means anything. Deferred until
that mapping is decided.

## Baseline: accepted findings

Legacy code has findings nobody will fix this year. A baseline records the
human decision per finding, next to the central profile, so `--fail-on` can
gate on what is new:

```yaml
baseline_version: 1
accepted:
  - fingerprint: 3f1c0d9a7b2e45c6d8e1   # from findings.json / summary.json
    rule: COMPLEXITY-001
    symbol: EC_Check_Power_Cycle         # informational
    reason: "legacy state machine, refactoring planned in 100554-268001"
    approved_by: "R. Mokbel"
    date: 2026-10-18
    measured_max: 34                     # ratchet: a larger measured value is a regression
```

- The profile names it (`baseline: 100554.baseline.yaml`, relative to the
  profile file); `--baseline` overrides.
- Every finding gets a `status`: `new`, `accepted`, or `regressed` (accepted
  but above `measured_max`). `--fail-on` counts `new` and `regressed` only.
  Nothing is removed: accepted findings stay in `findings.json`, the report and
  the counts, sorted last.
- Entries whose fingerprint no longer occurs are listed as stale in the
  manifest, summary and report.
- `archrev baseline-draft --in <folder>` writes every current finding with
  `TODO` placeholders; archrev refuses the file until a person has filled in
  `reason`, `approved_by` and `date`. Unknown keys, bad dates, duplicate or
  malformed fingerprints and a rule that does not match the finding are errors
  (exit 2).

## Parse coverage

A unit counts as clean when it has no errors inside App files; those errors can
hide App symbols. Errors confined to external headers (for example Diab
`asm` functions in vendor OS headers) are reported separately and do not gate.

## Profile

Profiles are kept centrally (`profiles/` in this repository is the source).
Without `--profile`, archrev picks the profile whose `match` names the target
repository:

```yaml
match:
  git_remotes: ["git@bitbucket.org:bitbucket-flex/100554_mc_appl.git"]  # any URL form
  repo_names: ["100554_mc_appl"]                                        # folder name, glob, case-insensitive
```

Search order: `--profiles-dir` (repeatable; replaces the rest), else
`ARCHREV_PROFILES` (`;`-separated), then the `profiles/` folder shipped with the
executable or the source checkout. Several matching profiles are a usage error
(exit 2); no match means built-in defaults. `summary.json` and `manifest.json`
record which profile was used and how (`explicit`, `central`, `none`).

See `profiles/100554.yaml` (real target) and `tests/fixtures/fx01/profile.yaml`
(minimal). Unknown keys are rejected. Sections: `match`, `scope` (`app_dir` is the old name), `baseline`, `signal_pool`, `design`, `layers`,
`compile` (`working_dir`, `include_dirs`, `system_include_dirs`, `defines`, `args`),
`build_list` (`makefile`, `variables`, `base_dir`,
`add_source_dirs_to_includes`), `modules` (`prefix_aliases`, `header_owner`, `export_exempt`),
`rules` (`severity`, `complexity`), `gate` (`min_parse_coverage`).

`build_list` reads plain make assignments (`:=`, `=`, `+=`) and expands
`$(VAR)` from single-line literal assignments in the same makefile.

## Known blind spots

Listed at the end of every report: type-only dependencies, code disabled by the
active defines, function-pointer targets, external modules not parsed, syntactic
write detection, and lizard counting `#if` inside functions in CCN.

## Tests

```
.venv\Scripts\python -m pytest
```

Fixture `tests/fixtures/fx01` seeds every rule with positive and negative cases,
plus conditional compilation, same-named statics, function pointers,
macro-mediated calls, mixed-case includes, line-shift stability, edge
injection/removal and byte-identical reruns.
