"""scan: every in-build module from one parse, the module graph, cycles, layers and unused exports."""

import json
import shutil
from pathlib import Path

import pytest

from archrev.cli import main
from archrev.pipeline import EXIT_FINDINGS, EXIT_OK, EXIT_USAGE
from archrev.profile import ProfileError, load_profile

FIXTURE = Path(__file__).parent / "fixtures" / "fx01"
PROFILE = FIXTURE / "profile.yaml"


def _scan(out: Path, *extra: str, repo: Path = FIXTURE, profile: Path = PROFILE) -> int:
    extra = list(extra)
    if "--modules" not in extra:
        extra.append("--all")
    return main(["scan", "--repo", str(repo), "--profile", str(profile), "--out", str(out), *extra])


def _json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def scanned(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("scan")
    assert _scan(out) == EXIT_OK
    return out


def test_scan_writes_every_in_build_module_and_the_aggregate(scanned):
    summary = _json(scanned / "scan_summary.json")
    assert summary["schema"] == "archrev.scan_summary/1" and summary["exit_code"] == EXIT_OK
    assert [m["name"] for m in summary["modules"]] == ["alpha", "beta"]
    assert summary["not_in_build"] == ["unused_mod"]
    for name in ("alpha", "beta"):
        assert (scanned / name / "summary.json").is_file() and (scanned / name / "report.md").is_file()
        assert _json(scanned / name / "summary.json")["module"] == name
    # Each module's own view matches a standalone analyze of it.
    single = scanned.parent / "single"
    assert main(["analyze", "--repo", str(FIXTURE), "--module", "alpha", "--profile", str(PROFILE),
                 "--out", str(single), "--context", "all"]) == EXIT_OK
    for name in ("model.json", "findings.json", "summary.json"):
        assert (scanned / "alpha" / name).read_bytes() == (single / name).read_bytes(), name
    assert summary["graph"] == {"nodes": 5, "edges": 5, "cycles": 1}
    assert summary["outputs"]["modules"] == {"alpha": "alpha/summary.json", "beta": "beta/summary.json"}


def test_merged_findings_carry_build_once_and_the_scan_rules(scanned):
    findings = _json(scanned / "scan_findings.json")["findings"]
    assert sum(f["rule_id"] == "BUILD-001" for f in findings) == 1
    scan_level = {(f["rule_id"], f["module"], f["symbol"]) for f in findings
                  if f["rule_id"] in ("ARCH-001", "LAYER-001", "EXPORT-001")}
    assert scan_level == {
        ("ARCH-001", "alpha", None),
        ("EXPORT-001", "alpha", "alpha_init"), ("EXPORT-001", "alpha", "alpha_step"),
        ("EXPORT-001", "alpha", "wrongName"),
        ("EXPORT-001", "beta", "beta_feature"),
    }
    # alpha_counter is written by beta: used, so not reported.
    cycle = next(f for f in findings if f["rule_id"] == "ARCH-001")
    assert cycle["message"] == "dependency cycle: alpha -> beta -> alpha" and cycle["severity"] == "major"
    assert cycle["file"] == "src/App/alpha.c" and cycle["measured_value"] == 2
    # Used symbols are not reported: beta_api/beta_api_cb/beta_hidden/beta_rte_op are used by alpha.
    assert not any(f["symbol"] in ("beta_api", "beta_hidden", "beta_rte_op", "alpha_internal") for f in findings
                   if f["rule_id"] == "EXPORT-001")
    report = (scanned / "scan_report.md").read_text(encoding="utf-8")
    assert "dependency cycle: alpha -> beta -> alpha" in report and "| `alpha` | `beta` |" in report


def test_graph_is_canonical(scanned):
    graph = _json(scanned / "graph.json")
    assert graph["schema"] == "archrev.graph/1"
    ids = [n["id"] for n in graph["nodes"]]
    assert ids == sorted(ids) and all(n["layer"] is None for n in graph["nodes"])
    edge = next(e for e in graph["edges"] if e["from"].endswith("beta.c") and e["to"].endswith("alpha.c"))
    assert edge["kinds"] == {"call": 1, "global_write": 1, "include": 1}
    assert edge["first_evidence"] == {"file": "src/App/beta.c", "line": 2, "kind": "include", "symbol": "src/App/alpha.h"}
    assert edge["first_by_kind"]["call"] == {"file": "src/App/beta.c", "line": 14, "kind": "call", "symbol": "alpha_internal"}
    again = scanned.parent / "again"
    assert _scan(again) == EXIT_OK
    for name in ("graph.json", "scan_findings.json", "scan_summary.json", "scan_report.md"):
        assert (scanned / name).read_bytes() == (again / name).read_bytes(), name


def test_export_exemptions_and_layers(tmp_path):
    profile = tmp_path / "p.yaml"
    profile.write_text(PROFILE.read_text(encoding="utf-8").replace(
        "modules:\n  prefix_aliases: {}\n",
        "modules:\n  prefix_aliases: {}\n  export_exempt: ['*_init', 'wrong*']\n"
        "layers:\n  assign:\n    app: ['src/App/alpha.c']\n    service: ['src/App/beta.*']\n"
        "  allow:\n    app: [service]\n"), encoding="utf-8")
    out = tmp_path / "out"
    assert _scan(out, "--fail-on", "major", profile=profile) == EXIT_FINDINGS
    findings = _json(out / "scan_findings.json")["findings"]
    exports = {f["symbol"] for f in findings if f["rule_id"] == "EXPORT-001"}
    assert exports == {"alpha_step", "beta_feature"}
    layer = [f for f in findings if f["rule_id"] == "LAYER-001"]
    assert len(layer) == 1 and layer[0]["module"] == "beta" and layer[0]["symbol"] == "alpha"
    assert "service may use: nothing" in layer[0]["message"]
    assert layer[0]["evidence"][0]["kind"] in ("call", "global_write"), "cites a kind the rule counts, not the include"
    cycle = next(f for f in findings if f["rule_id"] == "ARCH-001")
    assert cycle["evidence"][0]["kind"] in ("call", "fn_address", "global_read", "global_write", "global_addr")
    graph = _json(out / "graph.json")
    assert {n["name"]: n["layer"] for n in graph["nodes"] if n["layer"]} == {"alpha": "app", "beta": "service"}
    assert "- **app**: `alpha`" in (out / "scan_report.md").read_text(encoding="utf-8")


@pytest.mark.parametrize("snippet, match", [
    ("layers:\n  assign: {}\n", "at least one layer"),
    ("layers:\n  assign: {a: ['x']}\n  allow: {b: [a]}\n", "unknown key"),
    ("layers:\n  assign: {a: ['x'], b: ['y']}\n  allow: {a: [c]}\n", "not assigned"),
    ("layers:\n  assign: {a: ['x']}\n  edge_kinds: [teleport]\n", "unknown kind"),
])
def test_layer_validation(tmp_path, snippet, match):
    p = tmp_path / "p.yaml"
    p.write_text("profile_version: 1\n" + snippet, encoding="utf-8")
    with pytest.raises(ProfileError, match=match):
        load_profile(p)


def test_module_subset_and_errors(tmp_path, scanned):
    out = tmp_path / "sub"
    assert _scan(out, "--modules", "beta") == EXIT_OK
    summary = _json(out / "scan_summary.json")
    assert [m["name"] for m in summary["modules"]] == ["beta"] and not (out / "alpha").exists()
    # Only the named modules are parsed: the graph and the rules cover them, and the report says so.
    graph = _json(out / "graph.json")
    assert {n["name"]: n["kind"] for n in graph["nodes"] if n["name"] in ("alpha", "beta")} ==         {"alpha": "c_module_unparsed", "beta": "c_module"}
    assert not any(e["from"].endswith("alpha.c") for e in graph["edges"]), "alpha was not parsed"
    rules = {(f["rule_id"], f["module"], f["symbol"]) for f in _json(out / "scan_findings.json")["findings"]
             if f["rule_id"] in ("ARCH-001", "EXPORT-001", "BUILD-001")}
    assert rules == {("BUILD-001", "unused_mod", None)} | {("EXPORT-001", "beta", s) for s in
                                                            ("beta_api", "beta_api_cb", "beta_feature", "beta_hidden", "beta_rte_op")}
    assert summary["analyzed_modules"] == ["beta"]
    assert "known among the analyzed modules only" in (out / "scan_report.md").read_text(encoding="utf-8")
    assert _scan(tmp_path / "bad", "--modules", "beta,nope") == EXIT_USAGE
    s = _json(tmp_path / "bad" / "scan_summary.json")
    assert s["schema"] == "archrev.scan_summary/1" and "nope" in s["error"] and "module" not in s
    assert _scan(tmp_path / "bad2", "--modules", "a;b") == EXIT_USAGE


def test_empty_build_still_reports_unbuilt_sources(tmp_path):
    repo = tmp_path / "fx01"
    shutil.copytree(FIXTURE, repo)
    (repo / "src" / "build.mk").write_text("SRC_APP :=\n", encoding="utf-8")
    assert _scan(tmp_path / "out", "--fail-on", "info", repo=repo, profile=repo / "profile.yaml") == EXIT_FINDINGS
    s = _json(tmp_path / "out" / "scan_summary.json")
    assert s["modules"] == [] and s["finding_counts"]["by_rule"] == {"BUILD-001": 3}


def test_duplicate_module_names_are_rejected(tmp_path):
    repo = tmp_path / "fx01"
    shutil.copytree(FIXTURE, repo)
    (repo / "src" / "App" / "sub").mkdir()
    (repo / "src" / "App" / "sub" / "alpha.c").write_text("int alpha_sub(void) { return 1; }\n", encoding="utf-8")
    (repo / "src" / "build.mk").write_text("SRC_APP := App/alpha.c App/beta.c App/sub/alpha.c\n", encoding="utf-8")
    assert _scan(tmp_path / "out", repo=repo, profile=repo / "profile.yaml") == EXIT_USAGE
    assert "must be unique" in _json(tmp_path / "out" / "scan_summary.json")["error"]


def test_cycle_identity_follows_the_whole_component(tmp_path, scanned):
    base = next(f for f in _json(scanned / "scan_findings.json")["findings"] if f["rule_id"] == "ARCH-001")
    repo = tmp_path / "fx01"
    shutil.copytree(FIXTURE, repo)
    # gamma joins the alpha<->beta cycle through beta: the component grows, so the finding must change.
    (repo / "src" / "App" / "gamma.c").write_text('#include "beta.h"\nint gamma_run(void) { return beta_api(1); }\n',
                                                  encoding="utf-8")
    beta = repo / "src" / "App" / "beta.c"
    beta.write_text(beta.read_text(encoding="utf-8").replace("return 7;", "return gamma_run();")
                    .replace('#include "alpha.h"', '#include "alpha.h"\nint gamma_run(void);'), encoding="utf-8")
    (repo / "src" / "build.mk").write_text("SRC_APP := App/alpha.c App/beta.c App/gamma.c\n", encoding="utf-8")
    assert _scan(tmp_path / "out", repo=repo, profile=repo / "profile.yaml") == EXIT_OK
    grown = next(f for f in _json(tmp_path / "out" / "scan_findings.json")["findings"] if f["rule_id"] == "ARCH-001")
    assert grown["measured_value"] == 3 and grown["fingerprint"] != base["fingerprint"]
    assert grown["message"] == "dependency cycle: alpha -> beta -> alpha; also in the same cycle group: gamma"


def test_scan_exit_code_is_the_worst_module_or_gate(tmp_path):
    assert _scan(tmp_path / "gate", "--fail-on", "info") == EXIT_FINDINGS
    repo = tmp_path / "fx01"
    shutil.copytree(FIXTURE, repo)
    beta = repo / "src" / "App" / "beta.c"
    beta.write_text(beta.read_text(encoding="utf-8") + "\nthis is not C\n", encoding="utf-8")
    assert _scan(tmp_path / "broken", repo=repo, profile=repo / "profile.yaml") == 3
    s = _json(tmp_path / "broken" / "scan_summary.json")
    assert s["exit_code"] == 3 and "parse coverage" in s["error"]
    assert all(m["exit_code"] == 3 for m in s["modules"])


def test_baseline_applies_to_scan_findings(tmp_path, scanned):
    findings = _json(scanned / "scan_findings.json")["findings"]
    cycle = next(f for f in findings if f["rule_id"] == "ARCH-001")
    bl = tmp_path / "b.yaml"
    bl.write_text("baseline_version: 1\naccepted:\n"
                  f"  - fingerprint: {cycle['fingerprint']}\n    rule: ARCH-001\n    module: alpha\n"
                  "    reason: known, decoupling planned\n    approved_by: reviewer\n    date: 2026-10-18\n"
                  "  - fingerprint: 0123456789abcdef0123\n    rule: NAMING-001\n    module: beta\n"
                  "    reason: gone\n    approved_by: reviewer\n    date: 2026-10-18\n", encoding="utf-8")
    out = tmp_path / "out"
    assert _scan(out, "--baseline", str(bl), "--fail-on", "major") == EXIT_FINDINGS, "majors of alpha remain"
    merged = _json(out / "scan_findings.json")["findings"]
    assert next(f for f in merged if f["rule_id"] == "ARCH-001")["status"] == "accepted"
    s = _json(out / "scan_summary.json")
    assert s["baseline"] == {"file": "b.yaml", "stale_entries": 1, "stale_checked": True}
    assert s["finding_counts"]["by_status"]["accepted"] == 1
