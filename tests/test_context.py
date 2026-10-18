"""Partial context: what a run can and cannot know when it parses only some modules, and what it says about it."""

import json
import shutil
from pathlib import Path

from archrev.cli import main
from archrev.discovery import resolve_module
from archrev.pipeline import EXIT_OK, EXIT_USAGE

FIXTURE = Path(__file__).parent / "fixtures" / "fx01"
PROFILE = FIXTURE / "profile.yaml"


def _analyze(out: Path, *extra: str, repo: Path = FIXTURE, profile: Path = PROFILE) -> int:
    return main(["analyze", "--repo", str(repo), "--module", "alpha", "--profile", str(profile), "--out", str(out),
                 *extra])


def _json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_alone_the_target_keeps_its_outgoing_uses_with_unknown_owner(tmp_path):
    assert _analyze(tmp_path) == EXIT_OK
    model = _json(tmp_path / "model.json")
    assert model["analyzed_modules"] == ["alpha"]
    kinds = {m["name"]: m["kind"] for m in model["modules"]}
    assert kinds["beta"] == "c_module_unparsed", "known through beta.h, not parsed"
    # beta_hidden is declared only by alpha's own extern prototype: the call is a use of an unknown owner,
    # not an internal reference.
    hidden = next(e for e in model["dependencies"]["outgoing"] if e["symbol"] == "beta_hidden")
    assert hidden["to"] == "unknown:beta_hidden" and kinds["beta_hidden"] == "unknown"
    assert model["dependencies"]["incoming"] == [] and model["accounting"]["ok"]
    findings = _json(tmp_path / "findings.json")["findings"]
    interface = {f["symbol"]: f["message"] for f in findings if f["rule_id"] == "INTERFACE-001"}
    assert "through its own declaration" in interface["beta_hidden"]
    assert "alpha_internal" not in interface, "the incoming bypass needs beta parsed"
    summary = _json(tmp_path / "summary.json")
    assert summary["analyzed_modules"] == ["alpha"] and summary["baseline"]["stale_checked"] is False
    assert "(callers, producers and consumers are known among these only)" in (tmp_path / "report.md").read_text(encoding="utf-8")


def test_context_all_restores_the_full_view(tmp_path):
    assert _analyze(tmp_path / "all", "--context", "all") == EXIT_OK
    assert _analyze(tmp_path / "beta", "--context", "beta") == EXIT_OK
    for d in ("all", "beta"):
        model = _json(tmp_path / d / "model.json")
        assert next(e for e in model["dependencies"]["outgoing"] if e["symbol"] == "beta_hidden")["to"].endswith("beta.c")
        assert len(model["dependencies"]["incoming"]) == 3
    assert _json(tmp_path / "all" / "summary.json")["baseline"]["stale_checked"] is True
    assert _json(tmp_path / "beta" / "summary.json")["baseline"]["stale_checked"] is True, "unused_mod is not in the build"


def test_partial_context_never_reports_stale_baseline_entries(tmp_path):
    assert _analyze(tmp_path / "full", "--context", "all") == EXIT_OK
    incoming = next(f for f in _json(tmp_path / "full" / "findings.json")["findings"]
                    if f["rule_id"] == "INTERFACE-001" and f["symbol"] == "alpha_internal")
    bl = tmp_path / "b.yaml"
    bl.write_text("baseline_version: 1\naccepted:\n"
                  f"  - fingerprint: {incoming['fingerprint']}\n    rule: INTERFACE-001\n    module: alpha\n"
                  "    reason: accepted\n    approved_by: reviewer\n    date: 2026-10-18\n", encoding="utf-8")
    assert _analyze(tmp_path / "partial", "--baseline", str(bl)) == EXIT_OK
    manifest = _json(tmp_path / "partial" / "manifest.json")
    assert manifest["baseline_stale"] == [] and manifest["baseline_stale_checked"] is False
    assert "stale entries not checked (partial context)" in (tmp_path / "partial" / "report.md").read_text(encoding="utf-8")
    assert _analyze(tmp_path / "complete", "--baseline", str(bl), "--context", "all") == EXIT_OK
    assert _json(tmp_path / "complete" / "manifest.json")["baseline_stale_checked"] is True


def test_overlapping_scopes_and_scope_order(tmp_path):
    res = resolve_module(FIXTURE, "alpha", ["src", "src/App"])
    assert res.module_file.name == "alpha.c"
    assert _analyze(tmp_path / "a", "--scope", "src/App", "--scope", "src/lib") == EXIT_OK
    assert _analyze(tmp_path / "b", "--scope", "src/lib", "--scope", "src/App") == EXIT_OK
    assert _json(tmp_path / "a" / "manifest.json")["scope"] == ["src/App", "src/lib"]
    assert (tmp_path / "a" / "model.json").read_bytes() == (tmp_path / "b" / "model.json").read_bytes()


def test_empty_selections_are_usage_errors(tmp_path):
    assert main(["scan", "--repo", str(FIXTURE), "--profile", str(PROFILE), "--out", str(tmp_path / "s"),
                 "--modules", ""]) == EXIT_USAGE
    assert _analyze(tmp_path / "c", "--context", "") == EXIT_USAGE
    assert _analyze(tmp_path / "d", "--context", "all,beta") == EXIT_USAGE
    assert _analyze(tmp_path / "e", "--context", "nope") == EXIT_USAGE
    assert "nope" in _json(tmp_path / "e" / "summary.json")["error"]


def test_design_004_covers_symbols_of_unparsed_modules(tmp_path):
    import sqlite3
    from test_design import Qea, ROOT
    q = Qea(tmp_path / "d.qea")
    q.component("alpha", provided=[], required=[])
    q.done()
    repo = tmp_path / "fx01"
    shutil.copytree(FIXTURE, repo)
    profile = repo / "profile.yaml"
    profile.write_text(PROFILE.read_text(encoding="utf-8") + f"design:\n  model: ../d.qea\n  component_root: {ROOT}\n",
                       encoding="utf-8")
    assert _analyze(tmp_path / "out", repo=repo, profile=profile) == EXIT_OK
    symbols = {f["symbol"] for f in _json(tmp_path / "out" / "findings.json")["findings"] if f["rule_id"] == "DESIGN-004"}
    assert {"beta_api", "beta_api_cb", "beta_hidden"} <= symbols
