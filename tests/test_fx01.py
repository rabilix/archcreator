import json
import shutil
import sys
from pathlib import Path

import pytest

from archcreator.cli import main
from archcreator.pipeline import EXIT_FINDINGS, EXIT_OK, EXIT_USAGE, run_analysis
from archcreator.profile import ProfileError, load_profile

FIXTURE = Path(__file__).parent / "fixtures" / "fx01"
CANONICAL = ("ir.json", "model.json", "findings.json", "report.md", "manifest.json")


def _copy_fixture(tmp_path: Path) -> Path:
    dst = tmp_path / "fx01"
    shutil.copytree(FIXTURE, dst)
    return dst


def _run(repo: Path, out: Path, module: str = "alpha", profile=None, fail_on=None) -> tuple[int, dict, list]:
    profile = profile or load_profile(repo / "profile.yaml")
    code = run_analysis(repo, module, profile, None, out, fail_on, context=["all"])
    model = json.loads((out / "model.json").read_text(encoding="utf-8"))
    findings = json.loads((out / "findings.json").read_text(encoding="utf-8"))["findings"]
    return code, model, findings


def _edges(model: dict, direction: str) -> set[tuple]:
    return {(e["to" if direction == "outgoing" else "from"].split("/")[-1], e["kind"], e["symbol"])
            for e in model["dependencies"][direction]}


@pytest.fixture(scope="module")
def baseline(tmp_path_factory):
    out = tmp_path_factory.mktemp("base")
    return _run(FIXTURE, out) + (out,)


def test_seeded_violations_detected(baseline):
    _, _, findings, _ = baseline
    got = {(f["rule_id"], f["symbol"], (f["measured_value"] if f["rule_id"] == "COMPLEXITY-001" else None))
           for f in findings}
    assert got == {
        ("INTERFACE-001", "beta_hidden", None),
        ("INTERFACE-001", "alpha_internal", None),
        ("NAMING-001", "wrongName", None),
        ("COMPLEXITY-001", "alpha_step", 7),
        ("COMPLEXITY-001", "alpha_step", 4),
        ("BUILD-001", None, None),
    }


def test_no_false_positives(baseline):
    _, model, findings, _ = baseline
    flagged = {f["symbol"] for f in findings if f["rule_id"] in ("INTERFACE-001", "NAMING-001")}
    # Declared in beta.h / alpha.h, used properly: must not be flagged.
    assert not flagged & {"beta_api", "beta_api_cb", "alpha_counter", "alpha_init", "alpha_step", "helper"}
    names = {f["name"] for f in model["provided_interface"]["functions"]}
    assert "beta_hidden" not in names, "a foreign extern declaration is a use, not a provided symbol"


def test_dependencies_are_complete_and_typed(baseline):
    _, model, _, _ = baseline
    assert _edges(model, "outgoing") == {
        ("beta.c", "call", "beta_api"),
        ("beta.c", "call", "beta_hidden"),
        ("beta.c", "call", "beta_rte_op"),
        ("beta.c", "fn_address", "beta_api_cb"),
        ("beta.c", "include", "src/App/beta.h"),
        ("ext.h", "global_read", "ext_value"),
        ("ext.h", "include", "src/lib/ext.h"),
        ("gamma_helper.h", "include", "src/App/gamma_helper.h"),
        ("gamma_helper.h", "macro_use", "GAMMA_MAX"),
        ("rte_like.h", "include", "src/lib/rte_like.h"),
        ("rte_like.h", "macro_use", "Rte_Call_Beta_Op"),
    }
    assert _edges(model, "incoming") == {
        ("beta.c", "call", "alpha_internal"),
        ("beta.c", "global_write", "alpha_counter"),
        ("beta.c", "include", "src/App/alpha.h"),
    }
    kinds = {m["name"]: m["kind"] for m in model["modules"]}
    assert kinds == {"alpha": "c_module", "beta": "c_module", "ext.h": "external", "gamma_helper.h": "header_only",
                     "rte_like.h": "external"}
    assert [u["expression"] for u in model["unresolved_calls"]] == ["alpha_cb"]
    assert model["accounting"]["ok"]


def test_call_through_external_macro_is_edge_but_not_bypass(baseline):
    _, model, findings, _ = baseline
    edge = next(e for e in model["dependencies"]["outgoing"] if e["symbol"] == "beta_rte_op")
    assert [(ev["via_macro"], ev["via_macro_file"]) for ev in edge["evidence"]] == [
        ("Rte_Call_Beta_Op", "src/lib/rte_like.h")]
    assert not any(f["symbol"] == "beta_rte_op" for f in findings)
    direct = next(e for e in model["dependencies"]["outgoing"] if e["symbol"] == "beta_hidden")
    assert all(ev["via_macro"] is None for ev in direct["evidence"])


def test_same_named_static_functions_stay_separate(baseline):
    _, model, _, _ = baseline
    assert not any(e["symbol"] == "helper" for d in ("outgoing", "incoming") for e in model["dependencies"][d])


def test_header_ownership_by_prefix(baseline):
    _, model, _, _ = baseline
    assert model["target"]["files"] == ["src/App/alpha.c", "src/App/alpha.h", "src/App/alpha_cfg.h"]


def test_byte_identical_outputs(tmp_path, baseline):
    _, _, _, base_out = baseline
    _run(FIXTURE, tmp_path / "again")
    for name in CANONICAL:
        assert (base_out / name).read_bytes() == (tmp_path / "again" / name).read_bytes(), name


def test_no_absolute_paths_in_canonical_outputs(baseline):
    _, _, _, out = baseline
    root = str(FIXTURE.resolve()).replace("\\", "/").lower()
    for name in CANONICAL:
        text = (out / name).read_text(encoding="utf-8").replace("\\\\", "/").lower()
        assert root not in text, name
        assert "f_laufwerk" not in text, name


def test_conditional_compilation(tmp_path):
    profile = load_profile(FIXTURE / "profile.yaml")
    _, model, _ = _run(FIXTURE, tmp_path / "off", profile=profile)
    assert ("beta.c", "call", "beta_feature") not in _edges(model, "outgoing")
    profile.defines.append("FEATURE_X")
    _, model, _ = _run(FIXTURE, tmp_path / "on", profile=profile)
    assert ("beta.c", "call", "beta_feature") in _edges(model, "outgoing")


def test_ids_stable_when_lines_shift(tmp_path, baseline):
    _, base_model, base_findings, _ = baseline
    repo = _copy_fixture(tmp_path)
    # Shift the source and the headers that define macros: macro identities must not carry offsets.
    for name in ("alpha.c", "alpha.h", "gamma_helper.h", "alpha_cfg.h"):
        src = repo / "src" / "App" / name
        src.write_text("/* shifted */\n\n\n" + src.read_text(encoding="utf-8"), encoding="utf-8")
    _, model, findings = _run(repo, tmp_path / "out")
    assert {m["guid"] for m in model["modules"]} == {m["guid"] for m in base_model["modules"]}
    for d in ("outgoing", "incoming"):
        assert {e["guid"] for e in model["dependencies"][d]} == {e["guid"] for e in base_model["dependencies"][d]}
    assert {f["fingerprint"] for f in findings} == {f["fingerprint"] for f in base_findings}


def test_injected_edge_appears_and_disappears(tmp_path):
    repo = _copy_fixture(tmp_path)
    beta_h = repo / "src" / "App" / "beta.h"
    beta_c = repo / "src" / "App" / "beta.c"
    alpha_c = repo / "src" / "App" / "alpha.c"
    beta_h.write_text(beta_h.read_text().replace("#endif", "int beta_new(void);\n\n#endif"))
    beta_c.write_text(beta_c.read_text() + "\nint beta_new(void)\n{\n    return 3;\n}\n")
    original = alpha_c.read_text()
    alpha_c.write_text(original.replace("return ALPHA_LIMIT;", "return ALPHA_LIMIT + beta_new();"))
    _, model, _ = _run(repo, tmp_path / "with")
    assert ("beta.c", "call", "beta_new") in _edges(model, "outgoing")
    alpha_c.write_text(original)
    _, model, _ = _run(repo, tmp_path / "without")
    assert ("beta.c", "call", "beta_new") not in _edges(model, "outgoing")


@pytest.mark.skipif(not sys.platform.startswith("win"), reason="mixed-case includes only resolve on Windows")
def test_include_spelling_does_not_split_identities(tmp_path):
    repo = _copy_fixture(tmp_path)
    beta_c = repo / "src" / "App" / "beta.c"
    beta_c.write_text('#include "ALPHA_CFG.H"\n' + beta_c.read_text().replace("return 7;", "return ALPHA_CFG_MODE;"))
    _, model, _ = _run(repo, tmp_path / "out")
    macros = [(m["name"], m["file"]) for m in model["provided_interface"]["macros"]]
    assert len(macros) == len(set(macros))
    uses = [e for e in model["dependencies"]["incoming"] if e["symbol"] == "ALPHA_CFG_MODE"]
    assert len(uses) == 1 and uses[0]["kind"] == "macro_use"


def test_fail_on_exit_code(tmp_path):
    code, _, _ = _run(FIXTURE, tmp_path / "a", fail_on="major")
    assert code == EXIT_FINDINGS
    code, _, _ = _run(FIXTURE, tmp_path / "b", fail_on="critical")
    assert code == EXIT_OK


def test_unknown_module_is_usage_error(tmp_path, capsys):
    code = main(["analyze", "--repo", str(FIXTURE), "--module", "nope", "--context", "all",
                 "--profile", str(FIXTURE / "profile.yaml"), "--out", str(tmp_path)])
    assert code == EXIT_USAGE
    assert "no nope.c" in capsys.readouterr().err


def test_profile_rejects_unknown_keys(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("profile_version: 1\ncompile:\n  include_dir: [x]\n")
    with pytest.raises(ProfileError, match="include_dir"):
        load_profile(bad)
