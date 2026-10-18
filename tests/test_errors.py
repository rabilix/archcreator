"""Exit-code contract on error paths, profile validation, and the provenance/identity fixes of the 2026-10-18 review."""

import json
import shutil
from pathlib import Path

import pytest

import archrev.cli as cli
from archrev.cli import main
from archrev.pipeline import EXIT_INTERNAL, EXIT_OK, EXIT_PARSE_GATE, EXIT_USAGE
from archrev.profile import ProfileError, load_profile

FIXTURE = Path(__file__).parent / "fixtures" / "fx01"
PROFILE = FIXTURE / "profile.yaml"


def _analyze(out: Path, profile: Path = PROFILE, module: str = "alpha", repo: Path = FIXTURE) -> int:
    return main(["analyze", "--repo", str(repo), "--module", module, "--profile", str(profile), "--out", str(out),
                 "--context", "all"])


def _json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _copy_fixture(tmp_path: Path) -> Path:
    dst = tmp_path / "fx01"
    shutil.copytree(FIXTURE, dst)
    return dst


def test_missing_profile_is_usage_error_with_summary(tmp_path, capsys):
    assert _analyze(tmp_path / "out", tmp_path / "nope.yaml") == EXIT_USAGE
    assert "nope.yaml" in capsys.readouterr().err
    s = _json(tmp_path / "out" / "summary.json")
    assert s["exit_code"] == EXIT_USAGE and "nope.yaml" in s["error"]


def test_malformed_yaml_is_usage_error(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("profile_version: 1\ncompile: [\n", encoding="utf-8")
    assert _analyze(tmp_path / "out", bad) == EXIT_USAGE
    assert "not valid YAML" in _json(tmp_path / "out" / "summary.json")["error"]


@pytest.mark.parametrize("snippet, match", [
    ("gate:\n  min_parse_coverage: .nan\n", "min_parse_coverage"),
    ("gate:\n  min_parse_coverage: -0.5\n", "min_parse_coverage"),
    ("gate:\n  min_parse_coverage: '0.9'\n", "min_parse_coverage"),
    ("gate:\n  min_parse_coverage: true\n", "min_parse_coverage"),
    ("rules:\n  severity: []\n", "rules.severity"),
    ("rules:\n  severity: {NOPE-001: major}\n", "rules.severity"),
    ("rules:\n  complexity: 3\n", "rules.complexity"),
    ("rules:\n  complexity: {ccn_max: true}\n", "ccn_max"),
    ("scope: 3\n", "scope"),
    ("scope: [a, a]\n", "twice"),
    ("scope: x\napp_dir: y\n", "cannot both"),
    ("compile:\n  working_dir: 3\n", "working_dir"),
    ("compile:\n  123: []\n", "not a string"),
    ("build_list:\n  makefile: []\n  variables: [A]\n  base_dir: .\n", "build_list.makefile"),
    ("build_list:\n  makefile: m\n  variables: [A]\n  base_dir: [x]\n", "build_list.base_dir"),
    ("modules:\n  header_owner: {3: a}\n", "header_owner"),
])
def test_profile_type_errors_are_profile_errors(tmp_path, snippet, match):
    p = tmp_path / "p.yaml"
    p.write_text("profile_version: 1\n" + snippet, encoding="utf-8")
    with pytest.raises(ProfileError, match=match):
        load_profile(p)


def test_profile_with_invalid_utf8_is_usage_error(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_bytes(b"profile_version: 1\nname: \xff\xfe\n")
    assert _analyze(tmp_path / "out", bad) == EXIT_USAGE
    assert "not UTF-8" in _json(tmp_path / "out" / "summary.json")["error"]


def test_argument_error_replaces_stale_summary(tmp_path, capsys):
    out = tmp_path / "out"
    assert _analyze(out) == EXIT_OK
    assert _json(out / "summary.json")["exit_code"] == EXIT_OK
    code = main(["analyze", "--repo", str(FIXTURE), "--module", "alpha", "--profile", str(PROFILE),
                 "--out", str(out), "--fail-on", "warning"])
    assert code == EXIT_USAGE
    assert "invalid choice" in capsys.readouterr().err
    s = _json(out / "summary.json")
    assert s["exit_code"] == EXIT_USAGE and s["module"] == "alpha" and "invalid choice" in s["error"]


def test_argument_error_summary_goes_where_argparse_would_have_put_it(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # the second call has no usable --out and falls back to ./archrev-out
    first, second = tmp_path / "first", tmp_path / "second"
    code = main(["analyze", "--repo", str(FIXTURE), "--module", "alpha", "--profile", str(PROFILE),
                 "--out", str(first), "--out", str(second), "--fail-on", "warning"])
    assert code == EXIT_USAGE
    assert (second / "summary.json").is_file() and not first.exists()
    assert main(["analyze", "--repo", str(FIXTURE), "--module", "alpha", "--out", "--fail-on", "major"]) == EXIT_USAGE
    assert not (Path.cwd() / "--fail-on").exists()


def test_internal_error_exits_5_with_summary(tmp_path, monkeypatch, capsys):
    def boom(*_args, **_kwargs):
        raise RuntimeError("libclang exploded")
    monkeypatch.setattr(cli, "run_analysis", boom)
    assert _analyze(tmp_path / "out") == EXIT_INTERNAL
    assert "internal error" in capsys.readouterr().err
    s = _json(tmp_path / "out" / "summary.json")
    assert s["exit_code"] == EXIT_INTERNAL and "libclang exploded" in s["error"]


def test_parse_gate_failure_explains_itself(tmp_path):
    repo = _copy_fixture(tmp_path)
    beta = repo / "src" / "App" / "beta.c"
    beta.write_text(beta.read_text(encoding="utf-8") + "\nthis is not C\n", encoding="utf-8")
    out = tmp_path / "out"
    assert _analyze(out, repo / "profile.yaml", repo=repo) == EXIT_PARSE_GATE
    s = _json(out / "summary.json")
    assert s["exit_code"] == EXIT_PARSE_GATE
    assert "parse coverage" in s["error"] and "src/App/beta.c" in s["error"] and "100.00%" in s["error"]
    manifest = _json(out / "manifest.json")
    assert manifest["missing_include_dir_count"] == 0 and "missing_include_dirs" not in manifest


def test_build_finding_is_identical_for_every_target(tmp_path):
    assert _analyze(tmp_path / "a") == EXIT_OK
    assert _analyze(tmp_path / "b", module="beta") == EXIT_OK
    build = [[f for f in _json(tmp_path / d / "findings.json")["findings"] if f["rule_id"] == "BUILD-001"]
             for d in ("a", "b")]
    assert len(build[0]) == 1 and build[0][0]["module"] == "unused_mod"
    assert build[0][0]["fingerprint"] == build[1][0]["fingerprint"]


def test_not_in_build_target_is_parsed_and_says_so(tmp_path):
    assert _analyze(tmp_path, module="unused_mod") == EXIT_OK
    s = _json(tmp_path / "summary.json")
    assert s["target_in_build"] is False
    build = [f for f in _json(tmp_path / "findings.json")["findings"] if f["rule_id"] == "BUILD-001"]
    assert "analyzed module" in build[0]["message"]


def test_macro_provenance_is_per_expansion_not_per_name(tmp_path):
    """alpha.c and an external header both define DUP_CALL; alpha's own expansion must not count as mediated."""
    repo = _copy_fixture(tmp_path)
    lib = repo / "src" / "Aaa"  # sorts before "App": the old name-based lookup picked this file
    lib.mkdir()
    (lib / "dup.h").write_text("#ifndef DUP_H\n#define DUP_H\n#define DUP_CALL() 0\n#endif\n", encoding="utf-8")
    profile = repo / "profile.yaml"
    profile.write_text(profile.read_text(encoding="utf-8").replace("[App, lib]", "[App, lib, Aaa]"), encoding="utf-8")
    beta = repo / "src" / "App" / "beta.c"
    beta.write_text('#include "dup.h"\n' + beta.read_text(encoding="utf-8").replace("return 1;", "return DUP_CALL() + 1;"),
                    encoding="utf-8")
    alpha = repo / "src" / "App" / "alpha.c"
    alpha.write_text(alpha.read_text(encoding="utf-8")
                     .replace("extern int beta_hidden(void);", "extern int beta_hidden(void);\n#define DUP_CALL() beta_hidden()")
                     .replace("return ALPHA_LIMIT;", "return ALPHA_LIMIT + DUP_CALL();"), encoding="utf-8")
    out = tmp_path / "out"
    assert _analyze(out, profile, repo=repo) == EXIT_OK
    model = _json(out / "model.json")
    edge = next(e for e in model["dependencies"]["outgoing"] if e["symbol"] == "beta_hidden")
    via = [(ev["via_macro"], ev["via_macro_file"]) for ev in edge["evidence"] if ev["via_macro"]]
    assert via == [("DUP_CALL", "src/App/alpha.c")]
    finding = next(f for f in _json(out / "findings.json")["findings"]
                   if f["rule_id"] == "INTERFACE-001" and f["symbol"] == "beta_hidden")
    assert any(ev["via_macro"] == "DUP_CALL" for ev in finding["evidence"]), "the expansion is a bypass, not mediated"
