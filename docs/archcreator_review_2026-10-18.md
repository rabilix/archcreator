# archcreator review (2026-10-18)

Scope: the whole tool as it stands before its first commit: `src/archcreator/*`, the
100554 profile, packaging, tests, the copcli concept and the EA export.

Method:

- Full read of every source file.
- Three real runs on 100554 (`SEV`, `DCDC_FSM`, `Error_Check`) and inspection of
  `ir.json`, `model.json`, `findings.json`, `run_log.json`.
- Error-path probes (broken and missing `--profile`).
- Two Codex passes (`codex review --uncommitted`, `codex exec` with targeted
  questions); see section 5.

## 1. Verdict

The core is sound: the libclang model is complete per the accounting check,
outputs are byte-identical across the development install, wheel and exe, and
the trust boundary towards copcli is clean. The tool is an MVP in the literal
sense: it measures one module at a time, it has no notion of the design model,
and on this project its only architecture rule (INTERFACE-001) is blind to the
dominant coupling mechanism. The defects below are small; the gaps are where
the value is.

Numbers from the three runs:

| Module | Run time | Units parsed | Findings | NAMING | COMPLEXITY | INTERFACE | BUILD |
|---|---|---|---|---|---|---|---|
| SEV | 11.6 s | 19 | 6 | 0 | 1 | 0 | 5 |
| DCDC_FSM | 10.9 s | 19 | 14 | 0 | 9 | 0 | 5 |
| Error_Check | 10.8 s | 19 | 27 | 3 | 19 | 0 | 5 |

Parse coverage 100 % in all three; the two units with external errors fail only
on Diab `asm` in `Os_Hal_Compiler_Diab.h`.

## 2. Defects (confirmed)

| # | Where | Defect | Effect |
|---|---|---|---|
| D1 | `cli.py:90` | A malformed or missing `--profile` file raises `yaml.YAMLError` / `FileNotFoundError` outside the `except`. | Traceback and **exit 1**, which the contract defines as "findings at or above --fail-on". A CI gate reads a crash as a rule violation. Verified. |
| D2 | `cli.py:73-95` | Any other unexpected exception (libclang missing, I/O) also exits 1. | Same collision. No `error` summary is written, so copcli shows a raw traceback tail. |
| D3 | `rules.py:140-149` | BUILD-001 is a repository fact but is attributed to the target module (`module=target`, fingerprint includes the module name). | The same five files are reported in every module run with a different fingerprint each time; five findings of noise per run, never deduplicable. |
| D4 | `model.py:58-62` | A symbol defined in two App `.c` files is attributed to the lexicographically first file, silently. | Not present on 100554 today (0 duplicates), but the accounting check still passes while ownership is wrong. Should be a reported inconsistency. |
| D5 | `pipeline.py:43-55`, `profiles/100554.yaml:28-31` | Missing include directories are only logged in `run_log.json` / `manifest.missing_include_dirs`; the profile hash does not change. | On a machine where the Diab install is elsewhere, the model changes (fewer resolved symbols) under the same profile hash. The coverage gate catches most cases late, with an unhelpful message. |
| D6 | `discovery.py:196-222` | `read_make_list` ignores `ifdef`/`ifeq` blocks. | Conditionally compiled sources count as in-build. Harmless on `100554.mak` (its conditionals only change flags and directories), wrong on repositories that gate source lists. |
| D7 | `pipeline.py:172` | Only the superproject commit is recorded. | The BSW headers of `bmw_bac_sip` shape every external edge; the submodule SHA is not in the manifest, so a run is not fully reproducible from the manifest alone. |
| D8 | `rules.py:40-57` | NAMING-001 has no exemption list. | `BswM_Cbk_BusOff_Entry_HCAN` / `_Exit_HCAN` in `Error_Check` are AUTOSAR-mandated callback names and are reported as violations with no way to accept them other than lowering the rule severity for the whole module. |
| D9 | `pipeline.py:43-49` | Source directories from the build list are added as `-I` **before** the profile include directories, on the assumption that make's SPATH is an include path. | Include order decides which same-named header wins. 100554 has several `*/Appl` images with their own `Source/flex` copies. Needs verification against the Diab command line; if SPATH is not `-I`, this ordering can select the wrong header without any diagnostic. |
| D10 | `parser.py:230-233` | `ENUM_DECL` children are recorded as declarations but their initializers are not walked. | `enum { A = OTHER_ENUM + 1 }` across modules produces no `enum_use` edge. Macro uses inside are still caught (top-level cursors). Minor. |
| D11 | `profile.py:157`, `profile.py:142` | `min_parse_coverage` is not validated: a non-numeric value raises `ValueError`, `.nan` is accepted and disables the gate (`ratio < nan` is always false), negative values are accepted; `rules: {severity: []}` raises `AttributeError`. | Traceback instead of exit 2, or a silently disabled gate. |
| D12 | `parser.py:270-278`, `rules.py:84-91` | The file a macro expansion came from is looked up **by macro name across the whole run**, lexicographically first file wins. | Wrong `via_macro_file` evidence when two headers define the same name (17 names on 100554, all generated RTE/BSW: 12 references affected, no finding changes today). Constructed case: a caller-owned macro that shares its name with an external one is treated as mediated and an INTERFACE-001 bypass is suppressed. Fix: resolve provenance per expansion from the `MACRO_INSTANTIATION` cursor at the expansion site. |
| D13 | `parser.py:156-163`, `model.py:120` | Macro USRs keep libclang's byte offset (`c:<file>@<offset>@macro@NAME`), so `macro_use` dependency ids and GUIDs change when the defining header shifts. | Verified on fx01: two blank lines before `#define GAMMA_MAX` give the `GAMMA_MAX` edge a new GUID. The stability test only shifts `alpha.c`. The README's "stable across line shifts" claim is false for macro edges. Fix: normalize macro USRs to `c:<file>@macro@NAME`. |
| D14 | `pipeline.py:85-116` | `build_summary` writes `"error": null` for exit 3 (coverage gate) and 4 (accounting). | The copcli method tells the agent to "report the error and its fix from the summary" on exits 2-4; for 3 and 4 there is nothing to report. Add the reason and the measured/required values. |
| D15 | `pipeline.py:56`, `pipeline.py:198` | `missing_include_dirs` copies the raw profile strings into the canonical manifest. | With the shipped profile that means `F:/Programme/...` in `manifest.json` on a machine without that install. The absolute text comes from the profile, not from the machine, but the "no absolute paths" property of canonical files is broken. Keep it in `run_log.json`. |
| D16 | `pipeline.py:145`, `rules.py:147` | A target that is not in the build list is parsed anyway, while the BUILD-001 message says "it was not parsed". | Analyzing a not-in-build module exits 0 with a model and a finding that contradicts it. Decide: refuse (exit 2) or parse and say so in summary and report. |
| D17 | `xmi.py` (fixed today) | Context components, required interfaces and Usage connectors were keyed without the target module, so exports of two modules shared element ids. | Importing SEV and DCDC_FSM into one EA model would have moved DCDC_FSM's BlackBox component into SEV's Context. **Fixed**: every key now contains the target; new test `test_exports_of_different_modules_share_no_ids`; the three real exports share no id. |

Found by Codex and confirmed: D12, D13, D14, D15, D16, D17 and the extensions of D1 and D11.

Not a defect, but noted: NAMING-001 reports `ec_repetitive_counters` in
`Error_Check` because the alias `EC_` is matched case-sensitively. That is a
genuine finding under the declared rule; keep it.

## 3. Gaps against the governance goal

These are the differences between what archcreator measures and what an
architecture review of this project needs. Ordered by impact.

### G1. The signal pool is invisible as module coupling

All three modules show **zero** INTERFACE-001 findings. The reason is not clean
code: cross-module data flow in 100554 goes through `SP_Get_<signal>`,
`SP_Set_<signal>`, `SP_Test_<signal>` macros from `flex/system/signals.h`
(outside the App folder). archcreator attributes those uses to the external module
`signalpool.h` and treats them as mediated. So:

- The producer/consumer relation between App modules (who sets a signal, who
  reads it) is not an edge in the model. `DCDC_FSM`'s 156 outgoing edges into
  `software/source/flex` mostly end at `signals.h` / `signalpool.h`.
- The UML model expresses exactly this relation (RequiredInterface /
  ProvidedInterface per signal under `SYSTEM/Signals`). The one thing the
  design model is most precise about is the one thing archcreator cannot see.

### G2. No design-model input

The question that started this work, "where does the code deviate from the UML
model", is answered only by throw-away scripts in the scratchpad
(`compare.py`, `qea_*.py`). archcreator itself has no notion of a design model. The
XMI export goes the other way (code into EA) and, because the as-built
elements carry their own GUIDs, EA's model comparison cannot pair them with the
design elements. Comparison in EA is therefore by eye.

### G3. One module at a time, no graph

Every run parses all 19 in-build units (10.5 s) and then keeps one module's
view. Consequences:

- No cycle detection, no layering rule (App module calling a driver directly,
  SWC bypassing the RTE), no "exported but never used" rule. These are the
  classic architecture rules and they need the whole graph.
- Reviewing 15 modules costs 15 full parses, and `BUILD-001` is repeated 15
  times (D3).

### G4. No baseline, no accepted findings, no trend

`findings[].status` is always `"new"`. Legacy findings (`Error_Check` has 11 of
44 functions over CCN 15, maximum 90) are reported identically on every run,
forever. There is no way to record "accepted, reason, approver, date", so:

- `--fail-on` is unusable as a CI gate on legacy code (it would fail on day 1
  and every day after).
- The project rule "corrections only in code or rules, with human approval"
  has no tool support: an approval cannot be written anywhere archcreator reads.
- No "did it get worse" (ratchet) check, which is the only complexity rule
  that works on legacy code.

### G5. The central profile is not portable

`profiles/100554.yaml` hard-codes `F:/Programme/WindRiver_5_9/...`. A
central profile that only works on one machine defeats decision 3. No variable
expansion (`${DIAB_HOME}`), no override from the command line or environment.

### G6. Complexity thresholds are placeholders

The profile says "to be calibrated". With the defaults, 25 % of `Error_Check`
and `DCDC_FSM` functions are over the CCN limit. Either the thresholds are
calibrated from the measured distribution or the rule is reported as
informational until a baseline (G4) exists.

### G7. No CI entry point

One module per invocation, no aggregate exit code, no machine-readable format a
pipeline understands (SARIF for Bitbucket Code Insights, JUnit). The exit codes
exist; the loop around them does not.

### G8. Rule-set approval is not recorded

The profile is the rule set, and its hash is in every manifest. Nothing records
who approved that hash. For an ASIL project an approved-rule-set register
(profile hash, approver, date) is the minimum.

### G9. Schema and compatibility policy

Schemas are `archcreator.*/1` with no changelog and no statement of what a `/2`
would mean for copcli or stored results.

## 4. Enhancements (proposed, in order)

| # | Enhancement | Addresses | Effort |
|---|---|---|---|
| E1 | **Signal-pool adapter.** Profile section `signal_pool: {patterns: ["SP_Get_{signal}", "SP_Set_{signal}", "SP_Test_{signal}_Errorflag", ...], header: flex/system/signals.h}`. Each macro use becomes a `signal_read` / `signal_write` / `signal_test` edge to a `signal:<name>` node; the producer is the module that writes it. New rules: SIGNAL-001 consumed but no producer in build; SIGNAL-002 produced by more than one module; SIGNAL-003 signal declared in UML as required but never read (the 24/102 unused interfaces found by hand in `Error_Check`). | G1, and it is the bridge to the UML signal interfaces | 2-3 days |
| E2 | **`archcreator compare --design <model.qea>`.** Read the EA model read-only through SQLite (the approach already proven in the scratchpad), match by names (component, `«Function»` interfaces, signal interfaces, states), and emit DESIGN-00x findings: in UML not in code, in code not in UML, name mismatches (`DCDC_Safety_Functions_Init` vs `DCDC_SF_Init`), state machine state sets. Deterministic, needs no EA installation, writes nothing to the model. Alternatively accept an EA XMI export as the design input for machines without the `.qea`. | G2 | 3-4 days |
| E3 | **`archcreator scan`: whole-App run.** Parse once, write every module's model plus `graph.json`; rules ARCH-001 (dependency cycles between App modules), LAYER-001 (allowed-dependency matrix in the profile, e.g. `swc -> driver: deny`), EXPORT-001 (exported symbol never used in the build). `analyze` stays as the per-module view. BUILD-001 moves here (fixes D3). | G3, D3 | 2-3 days |
| E4 | **Baseline file.** `baseline.yaml` next to the central profile: `{fingerprint, rule, reason, approved_by, date}`. Findings get `status: new / accepted / regressed`; `--fail-on` counts `new` only; `summary.json` reports all three counts. Ratchet for COMPLEXITY-001: a function may not exceed its baselined value. | G4, G6 | 1-2 days |
| E5 | **Exit-code hygiene.** Catch everything in `cli.main`: profile load errors are exit 2, anything unexpected is a new exit 5 (`internal error`) with an error `summary.json`. Validate `min_parse_coverage` (finite, 0..1) and nested profile types. Fill `summary.error` on exits 3 and 4. Move `missing_include_dirs` to `run_log.json`. | D1, D2, D11, D14, D15 | 1 day |
| E5a | **Stable macro identities and per-expansion provenance.** Normalize macro USRs (drop the offset); resolve `via_macro_file` from the `MACRO_INSTANTIATION` cursor covering the expansion instead of by name; extend the line-shift test to headers. | D12, D13 | 1 day |
| E6 | **Portable profiles.** `${VAR}` expansion in `compile.*` paths from the environment with the resolved values written to `run_log.json`; `gate.require_include_dirs: true` makes a missing directory exit 2; record submodule SHAs (`git submodule status`) in the manifest. | G5, D5, D7 | 1 day |
| E7 | **NAMING exemptions.** `modules.naming_exempt: ["BswM_Cbk_*", "Rte_*", "*_Callback"]` per profile; exempted symbols are listed in the report, not silently dropped. | D8 | 0.5 day |
| E8 | **Duplicate-definition check.** Two App definitions of one external symbol are a reported inconsistency (exit 4 class), not a silent pick. | D4 | 0.5 day |
| E9 | **Include order verification.** Capture the real Diab command line from one `make -n` run and put the `-I` order into the profile as-is; drop `add_source_dirs_to_includes` unless `make -n` proves SPATH is passed as `-I`. | D9 | 0.5 day |
| E10 | **CI outputs.** `archcreator scan --format sarif` and a JUnit variant; a Bitbucket pipeline example in the README. | G7 | 1 day |
| E11 | **Governance register.** `approved_profiles.yaml` (profile hash, approver, date, note) checked by `analyze`; a profile not in the register is reported as `unapproved` in summary and report. | G8 | 0.5 day |
| E12 | **IR cache.** Key `(file sha256, compile args, libclang version)` per unit; a second run on an unchanged tree skips the 10 s parse. Determinism is unaffected because the cache stores the same IR the parse would produce. | G3 cost | 1 day |
| E13 | **Make conditionals.** Evaluate `ifdef/ifndef/ifeq/ifneq` in `read_make_list` against `build_list.defines` from the profile; unresolved conditions are reported like unresolved tokens. | D6 | 1 day |
| E14 | **Test gaps.** Unit tests for `_classify_access` (compound assignment, `++`, `&x`, write through pointer), `read_make_list` (`+=`, continuation, `$(VAR)`, unresolved), `assign_modules` error paths, `PathMapper` system directories, exit 3 (coverage gate), exit 2 on broken profile, and a fixture with AUTOSAR `FUNC(ret, MEM) name(args)` definitions (lizard names were verified correct on 100554 but nothing pins that). | section 2 | 1 day |

Suggested sequence: E5, E5a, E7, E8 (one week, removes the noise, the exit
code trap and the unstable GUIDs), then E4 (makes the gate usable), then E1 and
E2 (the architecture value), then E3.

## 5. Codex review

Two passes with Codex CLI 0.161.0 (gpt-5-codex, reasoning effort high):
`codex review --uncommitted` over the whole repository and `codex exec` in a
read-only sandbox with targeted questions. Raw output:
`scratchpad/codex_archcreator.txt`, `scratchpad/codex_archcreator_exec.txt` (session
scratchpad, not in the repository).

| Codex item | Assessment | Where in this review |
|---|---|---|
| XMI ids shared across module exports (P1) | Confirmed, fixed, test added | D17 |
| Macro provenance by name suppresses INTERFACE-001 (P2) | Confirmed; constructed case only, no effect on 100554 findings today, wrong evidence in 12 references | D12 |
| Macro dependency GUIDs change on whitespace shifts (P2) | Confirmed by experiment | D13 |
| Profile loading errors escape exit 2 (P2) | Confirmed, matches D1; nested-type case added | D1, D11 |
| `min_parse_coverage: .nan` disables the gate (P2) | Confirmed | D11 |
| Gate failures leave `summary.error` null (P2) | Confirmed | D14 |
| Absolute paths from the profile in the canonical manifest (P2) | Confirmed, minor | D15 |
| Not-in-build target is parsed anyway (P2) | Confirmed, documentation/consistency | D16 |
| copcli: `edit .copcli/*` also covers archcreator's outputs (concept) | Confirmed and **fixed in the copcli worktree**: `edit .copcli/archcreator/*` is now denied after the notes rule (last match wins), with a test; `method.txt` and the concept say so. 21 tests pass, typecheck clean. | section 6 |

Codex did not find anything in the dependency model's reference walking,
ownership or accounting beyond D12; nor in profile matching or Windows path
handling. It could not run the test suite (read-only sandbox) and could not
verify the EA import.

## 6. copcli side (out of scope, one fix)

The permission hole above was the only concept-level gap Codex found and it
is closed. Everything else on the copcli side is unchanged and still waits for
the first live `/archcreator` session, the Codex review of the branch and the
commit.

## 8. Implementation status (same day, proposals 1-5)

All five proposals were implemented in sequence, each followed by a Codex
review (`codex exec`, read-only sandbox, high reasoning) whose confirmed points
were fixed before the next step. Test suite: 43 → 118 tests, all passing.

| Step | Delivered | Codex round found, fixed |
|---|---|---|
| 1 Exit codes, identities | Exit 5 for internal errors with an error summary on every failure path (including argparse errors); strict profile validation (types, `.nan`, non-string keys, invalid UTF-8); `summary.error` on exits 3 and 4; `missing_include_dir_count` instead of raw paths; BUILD-001 attributed to the unbuilt file; macro USRs without offsets (GUIDs stable under header edits); macro provenance per expansion: innermost producing macro via the first token's `#define` (D12, D13, D14, D15, D16) | build-list path types, non-string YAML keys, invalid UTF-8, stale summary on argparse errors |
| 2 Baseline | `baseline.py`: strict `accepted` entries (reason, approver, date, optional `measured_max` ratchet, `module`), statuses new/accepted/regressed, stale entries, `--fail-on` on unaccepted only, `baseline-draft` with placeholders the loader rejects; profile `baseline:`; central-profile discovery skips baseline files; `profiles/100554.baseline.yaml` (empty) | stale scoping with a shared baseline (module field), `rule` type, invalid unquoted dates, last `--out` wins |
| 3 Signal pool | Profile `signal_pool` with accessor patterns; `signal_<access>` edges to `signal:` nodes; `model.signals` with writers/readers across the build; SIGNAL-001/002; report table; XMI «Signal» interfaces; fixture fx02; 100554 profile configured (`SP_Get/Set/SetDefault/Test`, `_Errorflag`) | `SP_SetDefault_` missing; wrapper macros hiding accessors (now resolved through recorded macro bodies, with file-scoped resolution); unbuilt target counted as producer |
| 4 Design comparison | `design.py`: read-only `.qea` via SQLite, component under `component_root`, provided/required ports, DESIGN-000..006 with alias matching, `design_compare.json`, report section, `--design`/`--no-design`; 100554 profile points at `100554_BMW_DCDC_GEN3.qea`; the comparison reproduces the hand-made UML-vs-code findings of the first HTML exactly | accessor-operation interfaces satisfied by signal accesses; `.c`-local same-named macros; object-like parenthesised macros (`clang_Cursor_isMacroFunctionLike`); declared-only functions; states deferred (documented) |
| 5 Scan | `pipeline.py` split into `_prepare` (one parse) and `_analyze_module`; `scan.py`: `graph.json`, ARCH-001 (Tarjan, concrete cycle path), LAYER-001 (`layers` in the profile), EXPORT-001 (`modules.export_exempt`), merged `scan_findings.json`, `scan_summary.json`, `scan_report.md`; 100554 whole App folder: 19 modules in 12.6 s instead of 19 × 10.8 s | see section 9 |

What the 100554 scan shows (exit 0, no gate): 1 cycle (`appl -> AplDiag_Flex ->
appl`), 10 signals with several writers, 148 provided-in-design-but-not-in-code
and 199 code-exports-not-in-design, 291 exported symbols no App module uses
(mostly OS hooks and BSW callbacks: the `export_exempt` list in the profile is a
first proposal and needs the team's confirmation), 372 NAMING findings. This is
the legacy inventory the baseline exists for; nothing has been accepted on the
team's behalf.

Open after step 5:

- First import of `model.xmi` into EA 16 and the first live `/archcreator` session
  are still unverified (no EA, no Copilot session on this machine).
- State-machine comparison (part of E2) deferred: states sit in differently
  named packages per module; needs a per-module mapping decision.
- `export_exempt` and `layers` for 100554 are proposals in the profile, marked
  INFERENCE; LAYER-001 is off until `layers` is uncommented.
- SIGNAL-001 reports signals produced outside the App folder (ADC, drivers) as
  "no App writer" (`info`); a `signal_pool.external_producers` list or a
  driver-folder scan would remove that noise.
- The copcli plugin still offers `archcreator_analyze` only; an `archcreator_scan` tool
  is the natural next step there.

## 9. Codex review of step 5

Six points, all confirmed and fixed:

| Codex item | Fix |
|---|---|
| `--modules` subset truncated the graph and lost the cycle | the graph and the repository rules are built from every in-build module; `--modules` only limits which views are written (test: subset graph byte-identical to the full one) |
| Duplicate source basenames silently dropped a module | `scan` rejects duplicate module names (exit 2) |
| An accepted cycle could grow without changing the finding | ARCH-001 identity and measured value come from the strongly connected component, the message shows one concrete path plus the other members (test: a third module joining changes the fingerprint and the size) |
| An empty build selection suppressed BUILD-001 | BUILD-001 is produced at scan level too, independent of module views |
| Design-model hash cached process-wide | cached per run (`Prepared.hash_cache`) |
| LAYER-001/ARCH-001 cited an `#include` as evidence | evidence is kept per edge kind; a rule cites the earliest evidence of a kind it counts |

Final state: 121 tests, all passing; wheel and exe rebuilt, `archcreator.exe scan`
byte-identical to the development install; 100554 full scan 13.3 s.

## 10. Module boundaries and scope (decided after step 5)

Decisions (user, 2026-10-18):

1. The run analyzes what it is told, not always everything: `scan --modules A,B`
   or `scan --all`; `analyze --module X --context A,B|all` (default: none).
2. A module is one `.c` with its headers: the smallest unit. Grouping several
   `.c` files into one component is a later, explicit profile section.
3. The scope is a list of folders; `software/source/flex` is the start,
   delivered customer applications are added as further entries. The build
   list still decides what is built.

Implemented: profile `scope` (list; `app_dir` kept as the old name), `--scope`
repeatable, `--context`, `--modules`/`--all`, kind `c_module_unparsed` for
in-build modules outside the run, `analyzed_modules` in every manifest,
summary, report and the scan summary, wording "App folder" replaced by scope.

Consequences, deliberately accepted: callers, signal producers and consumers,
cycles and unused exports are known among the parsed modules only, and every
output says which those are. INTERFACE-001 still works against unparsed
modules through their headers.

Measured on 100554: `analyze DCDC_FSM` alone 1.9 s (was 10.8 s), with three
context modules 5.2 s, `scan --all` over the 43 in-build `flex/` modules 35 s
at 100 % parse coverage, after three more Diab shims in the profile
(`-fbracket-depth=4096`, implicit declarations downgraded, byte-swap builtins
renamed). The widened scope also exposed a parser bug: a Windows-1252 byte
inside a `#define` made the libclang binding raise while collecting macro
bodies; now tolerated (test added). The full scan shows one cycle group of ten
modules across `appl` and `system` (`appl`, `AplDiag_Flex`, `AplFlex`,
`DCDC_Control`, `DCDC_FSM`, `Error_Check`, `NvM`, `ad_values`,
`comm_error_handler`, `temp`), 27 scope modules without a design component
and 672 NAMING findings, most of them in driver and system code that never
followed the `<Module>_` convention.

Codex review of this step: see section 11.

## 11. Codex review of the scope step

Six points, all confirmed and fixed:

| Codex item | Fix |
|---|---|
| Without context, an `extern` prototype inside the target's own `.c` made an unparsed module's function count as *internal*; the use vanished from outgoing edges, INTERFACE-001 and DESIGN-003 went wrong | `owner_of` prefers a parsed definition, then a header, then another module's source; a prototype in the using source itself yields an `unknown:<symbol>` node (kind `unknown`). INTERFACE-001 reports it ("through its own declaration"), DESIGN-004 counts it |
| DESIGN-004 ignored symbols of `c_module_unparsed` modules | included, as INTERFACE-001 already did |
| A narrower context made baseline entries of context-dependent findings look stale | stale detection runs only when every in-build module was parsed (`baseline_stale_checked`, `baseline.stale_checked` in summaries; the report says "not checked (partial context)") |
| Overlapping scope folders (`src`, `src/App`) made `analyze` report a unique file as ambiguous | hits deduplicated by physical path |
| `--modules ""` and `--context ""` silently meant "all" / "none" | presence tested with `is not None`; empty lists are usage errors |
| The order of scope folders changed canonical outputs | scope folders deduplicated and sorted |

Codex also confirmed what was intended: unknown names, `all` mixed with
names, a missing scan selection and an empty build all behave as documented;
header ownership across folders needs an explicit `header_owner`. `analyze
--context all` is not byte-identical to the earlier whole-App output (new
fields `scope`, `analyzed_modules`, wording), which is accepted: nothing has
been committed or baselined yet.

Final state: 130 tests, all passing; wheel and exe rebuilt and byte-identical
to the development install on a real module with context.

## 7. Changes made during this review

- `src/archcreator/xmi.py`: all export keys include the target module (D17).
- `tests/test_xmi.py`: `test_exports_of_different_modules_share_no_ids`.
- copcli worktree `feature/archcreator`: `arch-creator.ts` deny rules for
  `.copcli/archcreator/*`, test, `method.txt` wording; concept section 5.3.

No other code was changed; the defects in section 2 are reported, not fixed.
