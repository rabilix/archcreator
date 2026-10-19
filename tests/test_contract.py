"""Contract of the machine-readable outputs that front ends (copcli) rely on."""

import json
from pathlib import Path

import pytest

from archcreator import __version__
from archcreator.cli import main
from archcreator.pipeline import EXIT_USAGE, TOP_FINDINGS, build_summary

FIXTURE = Path(__file__).parent / "fixtures" / "fx01"
PROFILE = str(FIXTURE / "profile.yaml")


def _analyze(out: Path, *extra: str, module: str = "alpha") -> int:
    return main(["analyze", "--repo", str(FIXTURE), "--module", module, "--profile", PROFILE, "--out", str(out),
                 "--context", "all", *extra])


def _summary(out: Path) -> dict:
    return json.loads((out / "summary.json").read_text(encoding="utf-8"))


def test_summary_matches_findings(tmp_path):
    assert _analyze(tmp_path) == 0
    s = _summary(tmp_path)
    findings = json.loads((tmp_path / "findings.json").read_text(encoding="utf-8"))["findings"]
    assert s["schema"] == "archcreator.summary/1"
    assert s["module"] == "alpha" and s["exit_code"] == 0 and s["error"] is None
    assert s["finding_counts"] == {
        "by_rule": {"BUILD-001": 1, "COMPLEXITY-001": 2, "INTERFACE-001": 2, "NAMING-001": 1},
        "by_severity": {"info": 1, "major": 3, "minor": 2},
        "by_status": {"new": 6, "accepted": 0, "regressed": 0},
        "total": 6,
    }
    assert s["baseline"] == {"file": None, "stale_entries": 0, "stale_checked": True}
    assert [f["fingerprint"] for f in s["top_findings"]] == [f["fingerprint"] for f in findings]
    assert s["top_findings_truncated"] is False
    assert s["dependencies"] == {"outgoing": 11, "incoming": 3, "unresolved_calls": 1, "signals": 0}
    assert s["parse_coverage"] == 1.0 and s["accounting_ok"] is True
    for name in s["outputs"].values():
        assert (tmp_path / name).is_file()
    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    assert "summary.json" in manifest["outputs"]


def test_summary_carries_exit_code_and_is_byte_identical(tmp_path):
    assert _analyze(tmp_path / "a", "--fail-on", "major") == 1
    assert _analyze(tmp_path / "b", "--fail-on", "major") == 1
    assert _summary(tmp_path / "a")["exit_code"] == 1
    assert (tmp_path / "a" / "summary.json").read_bytes() == (tmp_path / "b" / "summary.json").read_bytes()
    root = str(FIXTURE.resolve()).replace("\\", "/").lower()
    assert root not in (tmp_path / "a" / "summary.json").read_text(encoding="utf-8").replace("\\\\", "/").lower()


def test_summary_caps_top_findings():
    findings = [{"rule_id": "R", "severity": "minor", "status": "new", "module": "m", "symbol": f"s{i}", "file": "f.c",
                 "line": i, "message": "m", "fingerprint": f"{i:020d}"} for i in range(TOP_FINDINGS + 5)]
    manifest = {"target_commit": "c", "target_dirty": False, "target_in_build": True, "parse_coverage": {"ratio": 1.0},
                "profile_name": "p", "profile_origin": "explicit", "profile_file": "p.yaml",
                "baseline_file": None, "baseline_stale": [], "baseline_stale_checked": True,
                "design_file": None, "design_found": None,
                "analyzed_modules": ["m"],
                "accounting_ok": True, "finding_counts": {"minor": len(findings)}}
    model = {"dependencies": {"outgoing": [], "incoming": []}, "unresolved_calls": [], "signals": []}
    s = build_summary("m", manifest, model, findings, 0)
    assert len(s["top_findings"]) == TOP_FINDINGS
    assert s["top_findings_truncated"] is True and s["finding_counts"]["total"] == TOP_FINDINGS + 5


def test_usage_error_writes_error_summary(tmp_path):
    assert _analyze(tmp_path, module="nope") == EXIT_USAGE
    s = _summary(tmp_path)
    assert s["exit_code"] == EXIT_USAGE and "no nope.c" in s["error"]


@pytest.mark.parametrize("bad", ["a;b", "../x", "a b", ""])
def test_invalid_module_name_rejected_without_writing_outside(tmp_path, monkeypatch, bad):
    monkeypatch.chdir(tmp_path)
    assert main(["analyze", "--repo", str(FIXTURE), "--module", bad, "--profile", PROFILE]) == EXIT_USAGE
    assert not (tmp_path / "archcreator-out").exists()
    assert not (tmp_path / "x").exists()


def test_list_modules_json(capsys):
    assert main(["list-modules", "--repo", str(FIXTURE), "--profile", PROFILE, "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data == {"schema": "archcreator.modules/1", "scopes": [{"scope": "src/App", "modules": [
        {"name": "alpha", "file": "src/App/alpha.c", "in_build": True},
        {"name": "beta", "file": "src/App/beta.c", "in_build": True},
        {"name": "unused_mod", "file": "src/App/unused_mod.c", "in_build": False},
    ]}]}


def test_version_json(capsys):
    assert main(["version", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "tool": "archcreator", "version": __version__,
        "schemas": {"summary": "archcreator.summary/1", "modules": "archcreator.modules/1",
                    "scan_summary": "archcreator.scan_summary/1", "graph": "archcreator.graph/1"},
        "xmi_target": "ea16"}
