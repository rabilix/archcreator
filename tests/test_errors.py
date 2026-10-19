"""Exit-code contract on error paths, profile validation, and the provenance/identity fixes of the 2026-10-18 review."""

import json
import shutil
from pathlib import Path

import pytest

import archcreator.cli as cli
from archcreator.cli import main
from archcreator.pipeline import EXIT_INTERNAL, EXIT_OK, EXIT_PARSE_GATE, EXIT_USAGE
from archcreator.profile import ProfileError, load_profile

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
    monkeypatch.chdir(tmp_path)  # the second call has no usable --out and falls back to ./archcreator-out
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


def test_parse_gate_message_names_first_error_and_missing_include_folders(tmp_path):
    repo = _copy_fixture(tmp_path)
    beta = repo / "src" / "App" / "beta.c"
    beta.write_text('#include "generated_nowhere.h"\n' + beta.read_text(encoding="utf-8"), encoding="utf-8")
    profile = repo / "profile.yaml"
    profile.write_text(profile.read_text(encoding="utf-8").replace("include_dirs: [App, lib]",
                                                                   "include_dirs: [App, lib, flex/global]"),
                       encoding="utf-8")
    out = tmp_path / "out"
    assert _analyze(out, profile, repo=repo) == EXIT_PARSE_GATE
    error = _json(out / "summary.json")["error"]
    assert "generated_nowhere.h' file not found" in error and "flex/global" in error


def test_include_folder_listed_twice_is_passed_once_and_repo_path_is_as_given(tmp_path):
    repo = _copy_fixture(tmp_path)
    profile = repo / "profile.yaml"
    profile.write_text(profile.read_text(encoding="utf-8").replace("include_dirs: [App, lib]",
                                                                   "include_dirs: [App, lib, App, ./lib]"),
                       encoding="utf-8")
    out = tmp_path / "out"
    assert _analyze(out, profile, repo=repo) == EXIT_OK
    run_log = _json(out / "run_log.json")
    args = run_log["compile_args"]
    folders = [args[i + 1] for i, a in enumerate(args) if a == "-I"]
    assert len(folders) == len({f.lower() for f in folders})
    assert run_log["repo"] == str(repo.absolute())


def test_macro_defined_in_the_using_source_is_not_a_dependency(tmp_path):
    repo = _copy_fixture(tmp_path)
    beta = repo / "src" / "App" / "beta.c"
    beta.write_text("#define BETA_LIMIT 5\nint beta_limit_user(void) { return BETA_LIMIT; }\n"
                    + beta.read_text(encoding="utf-8"), encoding="utf-8")
    out = tmp_path / "out"
    assert _analyze(out, repo / "profile.yaml", module="beta", repo=repo) == EXIT_OK
    targets = [e["to"] for e in _json(out / "model.json")["dependencies"]["outgoing"]]
    assert not any("BETA_LIMIT" in t for t in targets)


def test_gate_message_keeps_absolute_folders_out_and_args_includes_count_for_dedup(tmp_path):
    repo = _copy_fixture(tmp_path)
    beta = repo / "src" / "App" / "beta.c"
    beta.write_text('#include "generated_nowhere.h"\n' + beta.read_text(encoding="utf-8"), encoding="utf-8")
    profile = repo / "profile.yaml"
    absent = (tmp_path / "no_such_sdk" / "include").as_posix()
    text = profile.read_text(encoding="utf-8").replace("include_dirs: [App, lib]", f"include_dirs: [App, lib, {absent}]")
    profile.write_text(text, encoding="utf-8")
    out = tmp_path / "out"
    assert _analyze(out, profile, repo=repo) == EXIT_PARSE_GATE
    error = _json(out / "summary.json")["error"]
    assert "no_such_sdk" not in error and "1 absolute folder(s)" in error

    from archcreator.pipeline import _compile_args
    prof = load_profile(profile)
    prof.args.extend(["-I", str(repo / "src" / "lib")])
    args = _compile_args(repo, prof, [], {})
    folders = [args[i + 1] for i, a in enumerate(args) if a == "-I"]
    assert len({f.lower() for f in folders}) == len(folders)


def test_relative_dash_i_in_args_does_not_hide_the_profile_folder_and_messages_hide_absolute_paths(tmp_path):
    from archcreator.pipeline import _compile_args, _first_error
    repo = _copy_fixture(tmp_path)
    prof = load_profile(repo / "profile.yaml")
    prof.args.extend(["-I", "lib", "-Ilib"])
    args = _compile_args(repo, prof, [], {})
    assert str((repo / "src" / "lib").resolve()) in args

    msgs = ["software/source/a.c:3: 'F:/private-sdk/gen/x.h' file not found",
            "a.c:3: '/home/u/sdk/x.h' file not found, see <outside>/inc/y.h"]
    for msg in msgs:
        text = _first_error({"units": [{"errors_app": 1, "error_messages": [msg]}]})
        assert "private-sdk" not in text and "/home/" not in text and "<absolute path>" in text
    for odd in [r"\\server\share\private x\y.h", "/generated.h", "C:/Program Files/private sdk/z.h",
                "C:/Users/O'Brien/sdk/z.h"]:
        text = _first_error({"units": [{"errors_app": 1, "error_messages": [f"a.c:1: '{odd}' file not found"]}]})
        assert text == "a.c:1: '<absolute path>' file not found.", text
    spaced = _first_error({"units": [{"errors_app": 1, "error_messages": ["src/App folder/config.h:3: 'generated.h' file not found"]}]})
    assert spaced == "src/App folder/config.h:3: 'generated.h' file not found."
    unquoted = _first_error({"units": [{"errors_app": 1, "error_messages": ["a.c:1: #error SDK missing: /generated.h"]}]})
    assert unquoted == "a.c:1: #error SDK missing: <path>."
    from archcreator.pipeline import _missing_hint
    hint = _missing_hint(["flex/global", "C:/private-sdk/include", "/private-sdk/include"])
    assert "flex/global" in hint and "private-sdk" not in hint and "2 absolute folder(s)" in hint
    assert "software/source/a.c:3" in _first_error({"units": [{"errors_app": 1, "error_messages": [msgs[0]]}]})


def test_scan_refuses_module_names_that_cannot_be_output_folders(tmp_path):
    from archcreator.discovery import DiscoveryError
    from archcreator.pipeline import _check_output_names
    _check_output_names(["alpha", "Beta_2"])
    for names, expect in ((["alpha", ".."], "not letters, digits and _ only: .."),
                          (["a-b"], "a-b"), (["Alpha", "ALPHA"], "differ only in case: Alpha/ALPHA")):
        with pytest.raises(DiscoveryError, match="cannot be output folders") as info:
            _check_output_names(names)
        assert expect in str(info.value)


def test_scan_all_with_a_hyphenated_source_name_is_a_usage_error(tmp_path):
    repo = _copy_fixture(tmp_path)
    shutil.copy(repo / "src" / "App" / "beta.c", repo / "src" / "App" / "we-ird.c")
    build = repo / "src" / "build.mk"
    build.write_text(build.read_text(encoding="utf-8").replace("App/beta.c", "App/beta.c App/we-ird.c"),
                     encoding="utf-8")
    out = tmp_path / "out"
    code = main(["scan", "--repo", str(repo), "--profile", str(repo / "profile.yaml"), "--all", "--out", str(out)])
    assert code == EXIT_USAGE
    assert "we-ird" in _json(out / "scan_summary.json")["error"]
