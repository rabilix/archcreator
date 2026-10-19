"""Signal pool: accessor macros become signal edges, producers and consumers are known, SIGNAL rules fire."""

import json
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from archcreator.cli import main
from archcreator.pipeline import EXIT_OK
from archcreator.profile import ProfileError, load_profile

FIXTURE = Path(__file__).parent / "fixtures" / "fx02"
PROFILE = FIXTURE / "profile.yaml"
NS = {"xmi": "http://schema.omg.org/spec/XMI/2.1", "uml": "http://schema.omg.org/spec/UML/2.1"}


def _run(out: Path, module: str, profile: Path = PROFILE, *extra: str) -> tuple[dict, list[dict]]:
    assert main(["analyze", "--repo", str(FIXTURE), "--module", module, "--profile", str(profile), "--out", str(out),
                 "--context", "all", *extra]) == EXIT_OK
    model = json.loads((out / "model.json").read_text(encoding="utf-8"))
    findings = json.loads((out / "findings.json").read_text(encoding="utf-8"))["findings"]
    return model, findings


def _signal_edges(model: dict) -> set[tuple[str, str]]:
    return {(e["kind"], e["symbol"]) for e in model["dependencies"]["outgoing"] if e["kind"].startswith("signal_")}


def test_alpha_signal_edges_and_view(tmp_path):
    model, findings = _run(tmp_path, "alpha")
    assert _signal_edges(model) == {("signal_read", "Temp"), ("signal_read", "Orphan"), ("signal_write", "Speed"),
                                    ("signal_error_write", "Speed"), ("signal_write", "Shared")}
    # The accessor macros are no longer plain macro_use edges to signals.h; the other macro still is.
    plain = {(e["kind"], e["symbol"]) for e in model["dependencies"]["outgoing"] if e["kind"] == "macro_use"}
    assert plain == {("macro_use", "SP_SIGNALID_Speed")}
    # The calls the accessors expand to stay as evidence of the mechanism, attributed to the accessor; the one
    # hidden in the WRITE_SHARED wrapper is the signal access itself (there is no instantiation of its own).
    call = next(e for e in model["dependencies"]["outgoing"] if e["kind"] == "call" and e["symbol"] == "sp_set")
    assert {ev["via_macro"] for ev in call["evidence"]} == {"SP_Set_Speed", "SP_Set_Speed_Errorflag", "WRITE_SHARED"}
    shared = next(e for e in model["dependencies"]["outgoing"] if e["kind"] == "signal_write" and e["symbol"] == "Shared")
    assert [(ev["line"], ev["via_macro"], ev["via_macro_file"]) for ev in shared["evidence"]] == \
        [(18, "WRITE_SHARED", "src/App/alpha.c")]
    orphan = next(e for e in model["dependencies"]["outgoing"] if e["kind"] == "signal_read" and e["symbol"] == "Orphan")
    assert [ev["via_macro"] for ev in orphan["evidence"]] == ["HELPER"], "alpha's own HELPER, not beta's"
    assert model["accounting"]["ok"]
    by_name = {s["name"]: s for s in model["signals"]}
    assert set(by_name) == {"Temp", "Orphan", "Speed", "Shared"}
    assert by_name["Speed"]["target_access"] == ["error_write", "write"]
    assert by_name["Speed"]["writers"] == ["module:src/App/alpha.c"]
    assert by_name["Speed"]["readers"] == ["module:src/App/beta.c"]
    assert by_name["Shared"]["writers"] == ["module:src/App/alpha.c", "module:src/App/beta.c"]
    assert by_name["Orphan"]["writers"] == [] and by_name["Temp"]["writers"] == ["module:src/App/beta.c"]
    assert by_name["Temp"]["readers"] == ["module:src/App/alpha.c"], "beta's HELPER writes Temp but reads Speed"
    kinds = {m["name"]: m["kind"] for m in model["modules"]}
    assert kinds["Speed"] == "signal" and kinds["signals.h"] == "external"
    assert model["metrics"]["coupling"]["fan_out_modules"] == 2, "signals are not modules: signals.h, signalpool.h"
    got = {(f["rule_id"], f["symbol"], f["severity"]) for f in findings}
    assert got == {("SIGNAL-001", "Orphan", "info"), ("SIGNAL-002", "Shared", "minor")}
    shared = next(f for f in findings if f["rule_id"] == "SIGNAL-002")
    assert "alpha and beta" in shared["message"] and shared["measured_value"] == 2
    report = (tmp_path / "report.md").read_text(encoding="utf-8")
    assert "| `Orphan` | read | - | `alpha` |" in report
    summary = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))
    assert summary["dependencies"]["signals"] == 4


def test_unbuilt_target_keeps_its_accesses_but_is_no_producer(tmp_path):
    import shutil
    repo = tmp_path / "fx02"
    shutil.copytree(FIXTURE, repo)
    (repo / "src" / "build.mk").write_text("SRC_APP := App/beta.c App/gamma.c\n", encoding="utf-8")
    assert main(["analyze", "--repo", str(repo), "--module", "alpha", "--profile", str(repo / "profile.yaml"),
                 "--out", str(tmp_path / "out"), "--context", "all"]) == EXIT_OK
    model = json.loads((tmp_path / "out" / "model.json").read_text(encoding="utf-8"))
    by_name = {s["name"]: s for s in model["signals"]}
    assert by_name["Speed"]["target_access"] == ["error_write", "write"]
    assert by_name["Speed"]["writers"] == [] and by_name["Shared"]["writers"] == ["module:src/App/beta.c"]
    findings = json.loads((tmp_path / "out" / "findings.json").read_text(encoding="utf-8"))["findings"]
    assert not any(f["rule_id"] == "SIGNAL-002" for f in findings), "one in-build writer is no shared ownership"


def test_beta_consumes_and_gamma_touches_nothing(tmp_path):
    model, findings = _run(tmp_path / "b", "beta")
    # HELPER() wraps SP_Set_Temp and, through the object-like READ_SPEED, SP_Get_Speed: both are beta's accesses.
    assert _signal_edges(model) == {("signal_test", "Speed"), ("signal_error_read", "Speed"),
                                    ("signal_read", "Speed"), ("signal_write", "Temp"), ("signal_write", "Shared")}
    helper = [e for e in model["dependencies"]["outgoing"] if e["kind"] in ("signal_read", "signal_write")
              and e["evidence"][0]["via_macro"] == "HELPER"]
    assert sorted((e["kind"], e["symbol"]) for e in helper) == [("signal_read", "Speed"), ("signal_write", "Temp")]
    assert model["accounting"]["ok"] and model["accounting"]["signal_wrapper_extra"] == 1
    assert {(f["rule_id"], f["symbol"]) for f in findings} == {("SIGNAL-002", "Shared")}
    model, findings = _run(tmp_path / "g", "gamma")
    assert model["signals"] == [] and findings == []


def test_without_signal_pool_the_macros_stay_macro_uses(tmp_path):
    text = PROFILE.read_text(encoding="utf-8")
    plain = tmp_path / "plain.yaml"
    plain.write_text(text[:text.index("signal_pool:")], encoding="utf-8")
    model, findings = _run(tmp_path / "out", "alpha", plain)
    assert not _signal_edges(model) and model["signals"] == [] and findings == []
    assert ("macro_use", "SP_Get_Temp") in {(e["kind"], e["symbol"]) for e in model["dependencies"]["outgoing"]}


def test_xmi_has_a_signals_package(tmp_path):
    _run(tmp_path, "alpha", PROFILE, "--xmi")
    tree = ET.parse(tmp_path / "model.xmi")
    root = tree.getroot().find("uml:Model", NS).find("packagedElement")
    signals = root.find("packagedElement[@name='Signals']")
    assert sorted(i.get("name") for i in signals.findall("packagedElement")) == ["Orphan", "Shared", "Speed", "Temp"]
    stereos = {c.find("properties").get("stereotype")
               for c in tree.getroot().find("xmi:Extension/connectors", NS)
               if c.find("target/model").get("name") == "Speed" and c.find("properties").get("ea_type") == "Usage"}
    assert stereos == {"signal_write", "signal_error_write"}


@pytest.mark.parametrize("snippet, match", [
    ("signal_pool:\n  header: h\n", "needs 'header' and 'accessors'"),
    ("signal_pool:\n  header: h\n  accessors: []\n", "non-empty"),
    ("signal_pool:\n  header: h\n  accessors: [{macro: SP_Get_, access: read}]\n", "placeholder"),
    ("signal_pool:\n  header: h\n  accessors: [{macro: 'SP_{signal}_{signal}', access: read}]\n", "placeholder"),
    ("signal_pool:\n  header: h\n  accessors: [{macro: 'SP_Get_{signal}', access: peek}]\n", "access must be"),
    ("signal_pool:\n  header: h\n  accessors: [{macro: 'SP_Get_{signal}', access: read, x: 1}]\n", "unknown key"),
])
def test_signal_pool_validation(tmp_path, snippet, match):
    p = tmp_path / "p.yaml"
    p.write_text("profile_version: 1\n" + snippet, encoding="utf-8")
    with pytest.raises(ProfileError, match=match):
        load_profile(p)


def test_first_listed_accessor_wins():
    sp = load_profile(PROFILE).signal_pool
    assert sp.match("SP_Set_Speed_Errorflag") == ("Speed", "error_write")
    assert sp.match("SP_Set_Speed") == ("Speed", "write")
    assert sp.match("SP_Test_X_Y_Errorflag") == ("X_Y", "error_read")
    assert sp.match("SP_SIGNALID_Speed") is None
