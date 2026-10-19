"""Baseline: accepted findings, the ratchet, stale entries, validation, and the draft command."""

import json
import shutil
from pathlib import Path

import pytest

from archcreator.baseline import BaselineError, load_baseline
from archcreator.cli import main
from archcreator.pipeline import EXIT_FINDINGS, EXIT_OK, EXIT_USAGE

FIXTURE = Path(__file__).parent / "fixtures" / "fx01"
PROFILE = FIXTURE / "profile.yaml"


def _analyze(out: Path, *extra: str, profile: Path = PROFILE, repo: Path = FIXTURE) -> int:
    return main(["analyze", "--repo", str(repo), "--module", "alpha", "--profile", str(profile), "--out", str(out),
                 "--context", "all", *extra])


def _json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _findings(out: Path) -> list[dict]:
    return _json(out / "findings.json")["findings"]


def _entry(f: dict, **extra) -> str:
    lines = [f"  - fingerprint: {f['fingerprint']}", f"    rule: {f['rule_id']}", "    reason: legacy, ticket 100554-1",
             "    approved_by: reviewer", "    date: 2026-10-18"]
    lines += [f"    {k}: {v}" for k, v in extra.items()]
    return "\n".join(lines)


def _baseline_file(path: Path, *entries: str) -> Path:
    path.write_text("baseline_version: 1\naccepted:\n" + "\n".join(entries) + "\n", encoding="utf-8")
    return path


@pytest.fixture(scope="module")
def plain(tmp_path_factory) -> list[dict]:
    out = tmp_path_factory.mktemp("plain")
    assert _analyze(out) == EXIT_OK
    return _findings(out)


def _naming(findings: list[dict]) -> dict:
    return next(f for f in findings if f["rule_id"] == "NAMING-001")


def test_accepted_findings_keep_their_place_but_do_not_gate(tmp_path, plain):
    majors = [f for f in plain if f["severity"] == "major"]
    assert len(majors) == 3
    bl = _baseline_file(tmp_path / "b.yaml", *(_entry(f) for f in majors))
    out = tmp_path / "out"
    assert _analyze(out, "--baseline", str(bl), "--fail-on", "major") == EXIT_OK
    s = _json(out / "summary.json")
    assert s["finding_counts"]["by_status"] == {"new": 3, "accepted": 3, "regressed": 0}
    assert s["finding_counts"]["total"] == 6, "accepting never removes a finding"
    assert s["baseline"] == {"file": "b.yaml", "stale_entries": 0, "stale_checked": True}
    assert [f["status"] for f in s["top_findings"]] == ["new"] * 3 + ["accepted"] * 3, "accepted sort last"
    accepted = next(f for f in _findings(out) if f["status"] == "accepted")
    assert accepted["baseline"] == {"reason": "legacy, ticket 100554-1", "approved_by": "reviewer",
                                    "date": "2026-10-18", "measured_max": None, "ticket": None}
    report = (out / "report.md").read_text(encoding="utf-8")
    assert "accepted (reviewer, 2026-10-18: legacy, ticket 100554-1)" in report
    # One major left unaccepted still fails the gate.
    bl2 = _baseline_file(tmp_path / "b2.yaml", *(_entry(f) for f in majors[:2]))
    assert _analyze(tmp_path / "out2", "--baseline", str(bl2), "--fail-on", "major") == EXIT_FINDINGS


def test_ratchet_marks_growth_as_regressed(tmp_path, plain):
    ccn = next(f for f in plain if f["rule_id"] == "COMPLEXITY-001" and f["measured_value"] == 7)
    within = _baseline_file(tmp_path / "ok.yaml", _entry(ccn, measured_max=7))
    assert _analyze(tmp_path / "ok", "--baseline", str(within), "--fail-on", "minor") == EXIT_FINDINGS  # others
    assert next(f for f in _findings(tmp_path / "ok") if f["fingerprint"] == ccn["fingerprint"])["status"] == "accepted"
    worse = _baseline_file(tmp_path / "worse.yaml", _entry(ccn, measured_max=6))
    assert _analyze(tmp_path / "worse", "--baseline", str(worse)) == EXIT_OK
    f = next(f for f in _findings(tmp_path / "worse") if f["fingerprint"] == ccn["fingerprint"])
    assert f["status"] == "regressed"
    assert _json(tmp_path / "worse" / "summary.json")["finding_counts"]["by_status"]["regressed"] == 1


def test_stale_entries_are_reported_for_the_analyzed_module_only(tmp_path, plain):
    naming = _naming(plain)
    stale = "\n".join(["  - fingerprint: 0123456789abcdef0123", "    rule: NAMING-001", "    module: alpha",
                       "    symbol: gone", "    reason: once upon a time", "    approved_by: reviewer",
                       "    date: 2025-01-01"])
    # Entries of other modules (the baseline is shared) and entries without module are not this run's business.
    other = stale.replace("0123456789abcdef0123", "aaaaaaaaaaaaaaaaaaaa").replace("module: alpha", "module: beta")
    unscoped = "\n".join(l for l in stale.replace("0123456789abcdef0123", "bbbbbbbbbbbbbbbbbbbb").splitlines()
                         if "module:" not in l)
    bl = _baseline_file(tmp_path / "b.yaml", _entry(naming), stale, other, unscoped)
    out = tmp_path / "out"
    assert _analyze(out, "--baseline", str(bl)) == EXIT_OK
    manifest = _json(out / "manifest.json")
    assert manifest["baseline_stale"] == [{"fingerprint": "0123456789abcdef0123", "rule": "NAMING-001",
                                           "module": "alpha", "symbol": "gone", "file": None}]
    assert manifest["baseline_file"] == "b.yaml" and len(manifest["baseline_hash"]) == 64
    assert _json(out / "summary.json")["baseline"]["stale_entries"] == 1
    assert "match no finding any more" in (out / "report.md").read_text(encoding="utf-8")


def test_profile_names_its_baseline_relative_to_itself(tmp_path, plain):
    repo = tmp_path / "fx01"
    shutil.copytree(FIXTURE, repo)
    (repo / "conf").mkdir()
    profile = repo / "conf" / "p.yaml"
    profile.write_text(PROFILE.read_text(encoding="utf-8") + "baseline: accepted.yaml\n", encoding="utf-8")
    _baseline_file(repo / "conf" / "accepted.yaml", _entry(_naming(plain)))
    out = tmp_path / "out"
    assert _analyze(out, profile=profile, repo=repo) == EXIT_OK
    assert _json(out / "summary.json")["finding_counts"]["by_status"]["accepted"] == 1


def test_byte_identical_with_baseline(tmp_path, plain):
    bl = _baseline_file(tmp_path / "b.yaml", _entry(_naming(plain)))
    assert _analyze(tmp_path / "a", "--baseline", str(bl)) == EXIT_OK
    assert _analyze(tmp_path / "b", "--baseline", str(bl)) == EXIT_OK
    for name in ("findings.json", "summary.json", "report.md", "manifest.json"):
        assert (tmp_path / "a" / name).read_bytes() == (tmp_path / "b" / name).read_bytes(), name


@pytest.mark.parametrize("text, match", [
    ("baseline_version: 2\naccepted: []\n", "baseline_version"),
    ("baseline_version: 1\naccepted: {}\n", "must be a list"),
    ("baseline_version: 1\nextra: 1\naccepted: []\n", "unknown key"),
    ("baseline_version: 1\naccepted:\n  - fingerprint: 0123456789abcdef0123\n    rule: NAMING-001\n"
     "    approved_by: x\n    date: 2026-01-01\n", "missing reason"),
    ("baseline_version: 1\naccepted:\n  - fingerprint: 0123456789abcdef0123\n    rule: NAMING-001\n    reason: TODO\n"
     "    approved_by: x\n    date: 2026-01-01\n", "'reason' must be filled in"),
    ("baseline_version: 1\naccepted:\n  - fingerprint: 0123456789abcdef0123\n    rule: NAMING-001\n    reason: r\n"
     "    approved_by: x\n    date: 18.10.2026\n", "YYYY-MM-DD"),
    ("baseline_version: 1\naccepted:\n  - fingerprint: xyz\n    rule: NAMING-001\n    reason: r\n"
     "    approved_by: x\n    date: 2026-01-01\n", "20 hex digits"),
    ("baseline_version: 1\naccepted:\n  - fingerprint: 0123456789abcdef0123\n    rule: NOPE-001\n    reason: r\n"
     "    approved_by: x\n    date: 2026-01-01\n", "unknown rule"),
    ("baseline_version: 1\naccepted:\n  - fingerprint: 0123456789abcdef0123\n    rule: []\n    reason: r\n"
     "    approved_by: x\n    date: 2026-01-01\n", "unknown rule"),
    ("baseline_version: 1\naccepted:\n  - fingerprint: 0123456789abcdef0123\n    rule: NAMING-001\n    reason: r\n"
     "    approved_by: x\n    date: 2026-02-30\n", "not valid YAML"),
    ("baseline_version: 1\naccepted:\n  - fingerprint: 0123456789abcdef0123\n    rule: NAMING-001\n    reason: r\n"
     "    approved_by: x\n    date: 2026-01-01\n    measured_max: .nan\n", "measured_max"),
    ("baseline_version: 1\naccepted:\n  - fingerprint: 0123456789abcdef0123\n    rule: NAMING-001\n    reason: r\n"
     "    approved_by: x\n    date: 2026-01-01\n  - fingerprint: 0123456789abcdef0123\n    rule: NAMING-001\n"
     "    reason: r\n    approved_by: x\n    date: 2026-01-01\n", "listed twice"),
    ("baseline_version: 1\naccepted:\n  - fingerprint: 0123456789abcdef0123\n    rule: NAMING-001\n    reason: r\n"
     "    approved_by: x\n    date: 2026-01-01\n    note: n\n", "unknown key"),
])
def test_baseline_validation(tmp_path, text, match):
    p = tmp_path / "b.yaml"
    p.write_text(text, encoding="utf-8")
    with pytest.raises(BaselineError, match=match):
        load_baseline(p)


def test_wrong_rule_for_fingerprint_is_usage_error(tmp_path, plain):
    f = dict(_naming(plain), rule_id="COMPLEXITY-001")
    bl = _baseline_file(tmp_path / "b.yaml", _entry(f))
    assert _analyze(tmp_path / "out", "--baseline", str(bl)) == EXIT_USAGE
    assert "says rule COMPLEXITY-001" in _json(tmp_path / "out" / "summary.json")["error"]


def test_draft_lists_every_finding_and_is_rejected_until_filled_in(tmp_path, plain, capsys):
    out = tmp_path / "out"
    assert _analyze(out) == EXIT_OK
    assert main(["baseline-draft", "--in", str(out), "--out", str(tmp_path / "draft.yaml")]) == 0
    assert "6 entries" in capsys.readouterr().out
    text = (tmp_path / "draft.yaml").read_text(encoding="utf-8")
    assert text.count("fingerprint:") == 6 and text.count("reason: TODO") == 6
    assert text.count("module: alpha") == 5 and "module: unused_mod" in text
    assert "measured_max: 7" in text, "numeric findings get their current value as the ratchet"
    with pytest.raises(BaselineError, match="must be filled in"):
        load_baseline(tmp_path / "draft.yaml")
    filled = text.replace("reason: TODO", "reason: legacy").replace("approved_by: TODO", "approved_by: me") \
                 .replace("date: TODO", "date: 2026-10-18")
    (tmp_path / "filled.yaml").write_text(filled, encoding="utf-8")
    assert _analyze(tmp_path / "out2", "--baseline", str(tmp_path / "filled.yaml"), "--fail-on", "info") == EXIT_OK
    assert _json(tmp_path / "out2" / "summary.json")["finding_counts"]["by_status"]["accepted"] == 6
    assert main(["baseline-draft", "--in", str(tmp_path / "nowhere")]) == EXIT_USAGE
