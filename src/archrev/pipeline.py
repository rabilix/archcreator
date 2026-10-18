"""Orchestrates analysis runs. Pure stages; only this module (and scan.py) touch the filesystem for outputs.

The scope (a folder of the repository) holds the modules; the build list says which are built. A run parses only
the modules it names: `analyze` the target plus `--context`, `scan` `--modules` or `--all`. `analyze` writes one
module's view; `scan` writes every named module's view plus the graph and rules over them (see scan.py). Both share
`_prepare` and `_analyze_module`.
"""

import json
import platform
import subprocess
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from . import __version__
from .baseline import Baseline, apply_baseline, gating
from .canonical import write_json, write_text
from .design import DesignSpec, compare as compare_design, design_hash, open_design, read_module
from .discovery import (DiscoveryError, PathMapper, assign_modules, find_app_dir_candidates, list_app_files,
                        module_id_for_source, read_make_list, resolve_module, scope_dirs)
from .metrics import coupling, function_metrics, lizard_version
from .model import ModuleIndex, build_model
from .parser import libclang_version, parse_units
from .report import render
from .rules import (SEVERITY_ORDER, check_build, check_complexity, check_interface, check_naming, check_signals,
                    sort_findings)
from .xmi import render as render_xmi

EXIT_OK = 0
EXIT_FINDINGS = 1
EXIT_USAGE = 2
EXIT_PARSE_GATE = 3
EXIT_ACCOUNTING = 4
EXIT_INTERNAL = 5  # a bug or a broken installation, never a finding

SUMMARY_SCHEMA = "archrev.summary/1"
TOP_FINDINGS = 20
MODULES_SCHEMA = "archrev.modules/1"


def _git(repo: Path, *args: str) -> str | None:
    try:
        out = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True)
        return out.stdout
    except (OSError, subprocess.CalledProcessError):
        return None


def _compile_args(repo: Path, profile, source_dirs: list[Path], log: dict) -> list[str]:
    wd = repo / profile.working_dir
    args = list(profile.args)
    args += [f"-D{d}" for d in profile.defines]
    missing = []
    for d in source_dirs:
        args += ["-I", str(d)]
    for inc in profile.include_dirs:
        p = (wd / inc)
        if not p.is_dir():
            missing.append(inc)
        args += ["-I", str(p.resolve())]
    for sysdir in profile.system_include_dirs:
        p = Path(sysdir)
        p = p if p.is_absolute() else wd / p
        if not p.is_dir():
            missing.append(sysdir)
        args += ["-isystem", str(p.resolve())]
    log["compile_args"] = args
    log["missing_include_dirs"] = missing
    return args


def _system_dirs(repo: Path, profile) -> list[Path]:
    wd = repo / profile.working_dir
    return [Path(s) if Path(s).is_absolute() else wd / s for s in profile.system_include_dirs]


def _build_list(repo: Path, profile, mapper: PathMapper) -> tuple[set[str] | None, list[str], set[Path]]:
    """Labels of the files in the profile's build list (None without one), unresolved make tokens, source dirs."""
    if profile.build_list is None:
        return None, [], set()
    bl = profile.build_list
    built: set[str] = set()
    unresolved: list[str] = []
    source_dirs: set[Path] = set()
    for tok in read_make_list(repo / bl.makefile, bl.variables):
        if "$(" in tok:
            unresolved.append(tok)
            continue
        path = repo / bl.base_dir / tok
        built.add(mapper.label(str(path)))
        if bl.add_source_dirs_to_includes:
            source_dirs.add(path.parent.resolve())
    return built, unresolved, source_dirs


def build_summary(module: str, manifest: dict, model: dict, findings: list[dict], exit_code: int,
                  error: str | None = None, top: int = TOP_FINDINGS) -> dict:
    """Small, canonical digest of a run for front ends (copcli); the full data stays in the other files."""
    return {
        "schema": SUMMARY_SCHEMA,
        "module": module,
        "exit_code": exit_code,
        "error": error,
        "target_commit": manifest["target_commit"],
        "target_dirty": manifest["target_dirty"],
        "target_in_build": manifest["target_in_build"],
        "parse_coverage": manifest["parse_coverage"]["ratio"],
        "accounting_ok": manifest["accounting_ok"],
        "profile": {"name": manifest["profile_name"], "origin": manifest["profile_origin"],
                    "file": manifest["profile_file"]},
        "analyzed_modules": manifest["analyzed_modules"],
        "baseline": {"file": manifest["baseline_file"], "stale_entries": len(manifest["baseline_stale"]),
                     "stale_checked": manifest["baseline_stale_checked"]},
        "design": {"file": manifest["design_file"], "module_found": manifest["design_found"]},
        "finding_counts": finding_counts(findings),
        "top_findings": top_findings(findings, top),
        "top_findings_truncated": len(findings) > top,
        "dependencies": {"outgoing": len(model["dependencies"]["outgoing"]),
                         "incoming": len(model["dependencies"]["incoming"]),
                         "unresolved_calls": len(model["unresolved_calls"]),
                         "signals": len(model["signals"])},
        "outputs": {"report": "report.md", "findings": "findings.json", "model": "model.json",
                    "manifest": "manifest.json"},
    }


def finding_counts(findings: list[dict]) -> dict:
    return {
        "by_rule": dict(sorted(Counter(f["rule_id"] for f in findings).items())),
        "by_severity": dict(sorted(Counter(f["severity"] for f in findings).items())),
        "by_status": {s: sum(1 for f in findings if f["status"] == s) for s in ("new", "accepted", "regressed")},
        "total": len(findings),
    }


def top_findings(findings: list[dict], top: int = TOP_FINDINGS) -> list[dict]:
    return [{k: f[k] for k in ("rule_id", "severity", "status", "module", "symbol", "file", "line", "message",
                               "fingerprint")} for f in findings[:top]]


def error_summary(module: str, exit_code: int, message: str) -> dict:
    return {"schema": SUMMARY_SCHEMA, "module": module, "exit_code": exit_code, "error": message}


def export_xmi(out_dir: Path, target: Path | None = None) -> Path:
    """Write the EA 16 XMI of an analysis output folder (model.json + manifest.json); returns the file written."""
    try:
        model = json.loads((out_dir / "model.json").read_text(encoding="utf-8"))
        manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise DiscoveryError(f"not an archrev output folder (run analyze first): {exc.filename}") from None
    target = target or out_dir / "model.xmi"
    write_text(target, render_xmi(model, manifest))
    return target


def gate_code(findings: list[dict], fail_on: str | None) -> int:
    """EXIT_FINDINGS when a finding a person has not accepted reaches the --fail-on severity."""
    if fail_on is not None and any(SEVERITY_ORDER[f["severity"]] >= SEVERITY_ORDER[fail_on] for f in gating(findings)):
        return EXIT_FINDINGS
    return EXIT_OK


def _stage(log: dict, name: str, start: float) -> float:
    now = time.perf_counter()
    log["stages"][name] = round(log["stages"].get(name, 0) + now - start, 3)
    return now


@dataclass
class Prepared:
    """Everything a run shares between modules: the scope, the build list and the parsed IR."""
    mapper: PathMapper
    scope_label: list[str]
    sources: list[str]
    headers: list[str]
    in_build: list[str]
    not_in_build: list[str]
    parsed: list[str]
    unresolved_tokens: list[str]
    file_owner: dict[str, str]
    ir: dict
    index: ModuleIndex
    coverage: dict
    commit: str
    dirty: bool | None
    missing_include_dirs: int
    design_con: object | None = None
    hash_cache: dict[str, str] = field(default_factory=dict)

    @property
    def complete(self) -> bool:
        """Every in-build module of the scope was parsed: context-dependent results are final."""
        return set(self.parsed) >= set(self.in_build)


def _select(prep_sources: list[str], names: list[str], what: str, scope_label: list[str]) -> list[str]:
    """Source labels of the named modules; 'all' means every one. Unknown names are a usage error."""
    by_name = _by_name(prep_sources)
    if names == ["all"]:
        return sorted(by_name.values())
    unknown = sorted(set(names) - set(by_name))
    if unknown:
        raise DiscoveryError(f"{what}: not in-build modules of {', '.join(scope_label)}: {', '.join(unknown)}; "
                             "see list-modules")
    return sorted(by_name[n] for n in set(names))


def _by_name(sources: list[str]) -> dict[str, str]:
    by_name: dict[str, str] = {}
    for s in sources:
        stem = Path(s).stem
        if stem in by_name:
            raise DiscoveryError(f"module names must be unique within the scope: {stem} is {by_name[stem]} and {s}")
        by_name[stem] = s
    return by_name


def _prepare(repo: Path, profile, scope: list[Path], parse: "list[str] | callable", log: dict) -> Prepared:
    """`parse`: the module names to parse (['all'] for every in-build one), or a function of the in-build
    sources returning the source labels to parse."""
    t = time.perf_counter()
    mapper = PathMapper(repo, _system_dirs(repo, profile))
    scope_label = [mapper.label(str(d)) for d in scope]
    sources, headers = list_app_files(scope, mapper)
    built, unresolved_tokens, source_dirs = _build_list(repo, profile, mapper)
    in_build = [s for s in sources if built is None or s in built]
    not_in_build = [s for s in sources if s not in set(in_build)]
    log["unresolved_make_tokens"] = unresolved_tokens
    file_owner = assign_modules(sources, headers, profile.header_owner)
    args = _compile_args(repo, profile, sorted(source_dirs), log)
    # An empty selection (nothing in the build) is not an error: BUILD-001 still reports the unbuilt sources.
    parse_set = sorted(parse(in_build) if callable(parse) else _select(in_build, parse, "--modules", scope_label))
    log["parsed_units"] = parse_set
    t = _stage(log, "discovery", t)

    ir = parse_units([(s, str(repo / s)) for s in parse_set], args, mapper, set(sources) | set(headers))
    _stage(log, "parse", t)

    # Gate on errors inside App files: they directly hide App symbols. Errors confined to
    # external headers are reported separately and do not count against coverage.
    clean = sum(1 for u in ir["units"] if u["parsed"] and u["errors_app"] == 0)
    coverage = {"units": len(ir["units"]), "clean_units": clean,
                "units_with_external_errors": sum(1 for u in ir["units"] if u["errors_external"]),
                "ratio": round(clean / len(ir["units"]), 4) if ir["units"] else 1.0}
    commit = _git(repo, "rev-parse", "HEAD")
    status = _git(repo, "status", "--porcelain", "--untracked-files=no")
    return Prepared(mapper, scope_label, sources, headers, in_build, not_in_build, parse_set, unresolved_tokens,
                    file_owner, ir, ModuleIndex(file_owner, set(in_build), sources, set(parse_set)), coverage,
                    commit.strip() if commit else "unknown", bool(status.strip()) if status is not None else None,
                    len(log["missing_include_dirs"]))


def _scope(repo: Path, profile, scope: list[str] | None) -> list[Path]:
    """The scope folders: --scope, else the profile's, else the single App* folder of the repository."""
    chosen = scope if scope else profile.scope
    if chosen:
        return scope_dirs(repo, chosen)
    candidates = find_app_dir_candidates(repo)
    if len(candidates) != 1:
        listing = ", ".join(mapper_free_label(repo, c) for c in candidates) or "none"
        raise DiscoveryError(f"no scope given and {len(candidates)} App* folders found ({listing}); "
                             "set scope in the profile or pass --scope")
    return candidates


def mapper_free_label(repo: Path, path: Path) -> str:
    return path.resolve().relative_to(repo.resolve()).as_posix()


@dataclass
class ModuleResult:
    module: str
    code: int
    error: str | None
    model: dict
    findings: list[dict]
    manifest: dict


def _design_module(prep: Prepared, repo: Path, module: str, design: DesignSpec | None, design_path: Path | None,
                   log: dict):
    """The module's design, or None; the connection is opened once per run and closed by the caller."""
    if design is None:
        return None, None
    path = design_path if design_path is not None else repo / design.model
    if prep.design_con is None:
        log["design_model"] = str(path)
        prep.design_con = open_design(path)
    return read_module(prep.design_con, module, design), path


def _analyze_module(repo: Path, profile, prep: Prepared, module: str, target_label: str, out_dir: Path,
                    fail_on: str | None, xmi: bool, baseline: Baseline | None, design: DesignSpec | None,
                    design_path: Path | None, log: dict) -> ModuleResult:
    t = time.perf_counter()
    target_id = module_id_for_source(target_label)
    model = build_model(prep.ir, target_id, prep.index, prep.scope_label, profile.signal_pool)
    metrics = function_metrics(repo, list(model["target"]["files"]))
    coup = coupling(model)
    model["metrics"] = {"functions": metrics, "coupling": coup}
    t = _stage(log, "model", t)

    findings = (check_naming(model, profile)
                + check_complexity(metrics, model, profile)
                + check_interface(model, prep.file_owner, profile)
                + check_build(prep.not_in_build, target_label, profile)
                + check_signals(model, profile))
    dm, dpath = _design_module(prep, repo, module, design, design_path, log)
    design_record = None
    design_manifest = {"design_file": None, "design_hash": None, "design_found": None}
    if design is not None:
        design_findings, design_record = compare_design(model, dm, profile, dpath.name)
        findings += design_findings
        design_manifest = {"design_file": dpath.name, "design_hash": _cached_hash(prep, dpath),
                           "design_found": dm is not None}
    # A baseline entry can only be judged stale when the run could have produced the finding: a context-dependent
    # finding (an incoming use, a signal with several writers) is absent from a partial run without being gone.
    stale = apply_baseline(findings, baseline, modules={module} if prep.complete else set())
    findings = sort_findings(findings)
    t = _stage(log, "rules", t)

    out_dir.mkdir(parents=True, exist_ok=True)
    outputs = {
        "ir.json": write_json(out_dir / "ir.json", prep.ir),
        "model.json": write_json(out_dir / "model.json", model),
        "findings.json": write_json(out_dir / "findings.json", {"schema": "archrev.findings/1", "findings": findings}),
    }
    if design_record is not None:
        outputs["design_compare.json"] = write_json(out_dir / "design_compare.json", design_record)
    manifest = {
        "schema": "archrev.run_manifest/1",
        "tool_version": __version__,
        "analyzer_versions": {"libclang": libclang_version(), "lizard": lizard_version(),
                              "python": platform.python_version()},
        "profile_name": profile.name,
        "profile_origin": profile.origin,
        "profile_file": profile.file_name,
        "profile_hash": profile.source_hash,
        "baseline_file": baseline.file_name if baseline else None,
        "baseline_hash": baseline.source_hash if baseline else None,
        "baseline_stale": stale,
        "baseline_stale_checked": prep.complete,
        **design_manifest,
        "target_commit": prep.commit,
        "target_dirty": prep.dirty,
        "module": module,
        "module_id": target_id,
        "scope": prep.scope_label,
        "scope_sources": len(prep.sources),
        "analyzed_modules": model["analyzed_modules"],
        "target_in_build": target_label in set(prep.in_build),
        "not_in_build": prep.not_in_build,
        "unresolved_make_tokens": len(prep.unresolved_tokens),
        # The paths themselves are profile text and may be absolute; they stay in run_log.json.
        "missing_include_dir_count": prep.missing_include_dirs,
        "parse_coverage": prep.coverage,
        "accounting_ok": model["accounting"]["ok"],
        "finding_counts": dict(sorted(Counter(f["severity"] for f in findings).items())),
    }
    code, error = EXIT_OK, None
    acc = model["accounting"]
    if not acc["ok"]:
        code = EXIT_ACCOUNTING
        error = (f"accounting check failed: {acc['target_references']} references in the target files, but "
                 f"{acc['internal']} internal + {acc['outgoing_evidence']} outgoing + {acc['unresolved_calls']} "
                 "unresolved; the model is incomplete. Report this run with run_log.json and ir.json.")
    elif prep.coverage["ratio"] < profile.min_parse_coverage:
        code = EXIT_PARSE_GATE
        bad = [u["file"] for u in prep.ir["units"] if not (u["parsed"] and u["errors_app"] == 0)]
        error = (f"parse coverage {prep.coverage['ratio']:.2%} is below the gate {profile.min_parse_coverage:.2%}: "
                 f"{len(bad)} of {prep.coverage['units']} units have errors in App files "
                 f"({', '.join(bad[:5])}{', ...' if len(bad) > 5 else ''}). Fix the include paths or defines in "
                 "the profile; the messages are under 'Parse results' in report.md.")
    else:
        code = gate_code(findings, fail_on)

    report = render({**manifest, "outputs": outputs}, prep.ir, model, metrics, coup, findings, design_record)
    outputs["report.md"] = write_text(out_dir / "report.md", report)
    outputs["summary.json"] = write_json(out_dir / "summary.json",
                                         build_summary(module, manifest, model, findings, code, error))
    if xmi:
        outputs["model.xmi"] = write_text(out_dir / "model.xmi", render_xmi(model, manifest))
    manifest["outputs"] = dict(sorted(outputs.items()))
    write_json(out_dir / "manifest.json", manifest)
    _stage(log, "write", t)
    return ModuleResult(module, code, error, model, findings, manifest)


def _cached_hash(prep: Prepared, path: Path) -> str:
    """One hash of the (large) design model per run, never across runs: the file may change between them."""
    key = str(path.resolve())
    if key not in prep.hash_cache:
        prep.hash_cache[key] = design_hash(path)
    return prep.hash_cache[key]


def _finish_log(log: dict, t0: float, code: int) -> None:
    log["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    log["total_seconds"] = round(time.perf_counter() - t0, 3)
    log["exit_code"] = code


def _new_log(repo: Path) -> dict:
    return {"started_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "repo": str(repo.resolve()), "stages": {}}


def run_analysis(repo: Path, module: str, profile, scope: list[str] | None, out_dir: Path,
                 fail_on: str | None, xmi: bool = False, baseline: Baseline | None = None,
                 design: DesignSpec | None = None, design_path: Path | None = None,
                 context: list[str] | None = None) -> int:
    """One module's view. Only the target and the `context` modules (['all'] for every in-build one) are parsed."""
    log = _new_log(repo)
    t0 = time.perf_counter()
    chosen = scope if scope else profile.scope
    res = resolve_module(repo, module, chosen or None)
    dirs = scope_dirs(repo, chosen) if chosen else [res.app_dir]
    mapper = PathMapper(repo, _system_dirs(repo, profile))
    target_label = mapper.label(str(res.module_file))

    def parse(in_build: list[str]) -> list[str]:
        labels = [mapper.label(str(d)) for d in dirs]
        extra = _select(in_build, context, "--context", labels) if context else []
        return sorted({target_label, *extra})

    prep = _prepare(repo, profile, dirs, parse, log)
    try:
        result = _analyze_module(repo, profile, prep, module, target_label, out_dir, fail_on, xmi, baseline, design,
                                 design_path, log)
    finally:
        if prep.design_con is not None:
            prep.design_con.close()
    _finish_log(log, t0, result.code)
    write_json(out_dir / "run_log.json", log)
    return result.code


def run_scan(repo: Path, profile, scope: list[str] | None, out_dir: Path, fail_on: str | None, xmi: bool = False,
             baseline: Baseline | None = None, design: DesignSpec | None = None, design_path: Path | None = None,
             modules: list[str] | None = None) -> int:
    """The named modules' views (['all'] for every in-build one) from one parse, plus the graph and the rules
    over exactly those modules (scan.py)."""
    from .scan import finish_scan

    log = _new_log(repo)
    t0 = time.perf_counter()
    prep = _prepare(repo, profile, _scope(repo, profile, scope), modules or ["all"], log)
    selected = {Path(s).stem: s for s in prep.parsed}
    results: list[ModuleResult] = []
    try:
        for name in sorted(selected):
            results.append(_analyze_module(repo, profile, prep, name, selected[name], out_dir / name, fail_on, xmi,
                                           baseline, design, design_path, log))
    finally:
        if prep.design_con is not None:
            prep.design_con.close()
    all_models = {r.module: r.model for r in results}
    code = finish_scan(repo, profile, prep, results, all_models, out_dir, fail_on, baseline, design, design_path, log)
    _finish_log(log, t0, code)
    write_json(out_dir / "scan_log.json", log)
    return code


def list_modules_data(repo: Path, profile, scope: list[str] | None) -> dict:
    mapper = PathMapper(repo, _system_dirs(repo, profile))
    chosen = scope if scope else profile.scope
    dirs = scope_dirs(repo, chosen) if chosen else find_app_dir_candidates(repo)
    built, _, _ = _build_list(repo, profile, mapper)
    scopes = []
    for d in dirs:
        sources, _ = list_app_files(d, mapper)
        scopes.append({"scope": mapper.label(str(d)),
                       "modules": [{"name": Path(s).stem, "file": s, "in_build": built is None or s in built}
                                   for s in sources]})
    return {"schema": MODULES_SCHEMA, "scopes": scopes}


def list_modules(repo: Path, profile, scope: list[str] | None) -> list[str]:
    lines = []
    for d in list_modules_data(repo, profile, scope)["scopes"]:
        lines.append(f"{d['scope']}/  ({len(d['modules'])} modules)")
        lines += [f"  {m['name']:40s} {m['file']}{'' if m['in_build'] else '  (not in build)'}" for m in d["modules"]]
    return lines
