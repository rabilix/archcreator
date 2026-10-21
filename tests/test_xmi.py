"""XMI 2.1 export for Enterprise Architect 16: well-formed, self-consistent, complete and byte-stable."""

import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path

from archcreator.cli import main
from archcreator.pipeline import EXIT_USAGE

FIXTURE = Path(__file__).parent / "fixtures" / "fx01"
PROFILE = str(FIXTURE / "profile.yaml")
NS = {"xmi": "http://schema.omg.org/spec/XMI/2.1", "uml": "http://schema.omg.org/spec/UML/2.1"}
XMI_ID = f"{{{NS['xmi']}}}id"
XMI_TYPE = f"{{{NS['xmi']}}}type"
XMI_IDREF = f"{{{NS['xmi']}}}idref"


def _analyze(out: Path, *extra: str) -> int:
    return main(["analyze", "--repo", str(FIXTURE), "--module", "alpha", "--profile", PROFILE, "--out", str(out),
                 "--context", "all", *extra])


def _model(out: Path) -> dict:
    return json.loads((out / "model.json").read_text(encoding="utf-8"))


def test_xmi_written_by_analyze_and_hashed_in_manifest(tmp_path):
    assert _analyze(tmp_path, "--xmi") == 0
    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    assert "model.xmi" in manifest["outputs"]
    assert _analyze(tmp_path / "plain") == 0
    assert not (tmp_path / "plain" / "model.xmi").exists()


def test_export_xmi_equals_analyze_xmi_and_is_byte_identical(tmp_path):
    assert _analyze(tmp_path / "a", "--xmi") == 0
    assert _analyze(tmp_path / "b") == 0
    assert main(["export-xmi", "--in", str(tmp_path / "b"), "--out", str(tmp_path / "b.xmi")]) == 0
    assert (tmp_path / "a" / "model.xmi").read_bytes() == (tmp_path / "b.xmi").read_bytes()
    root = str(FIXTURE.resolve()).replace("\\", "/").lower()
    assert root not in (tmp_path / "b.xmi").read_text(encoding="utf-8").replace("\\", "/").lower()


def test_export_xmi_without_analysis_is_usage_error(tmp_path):
    assert main(["export-xmi", "--in", str(tmp_path)]) == EXIT_USAGE


def test_xmi_structure_matches_model(tmp_path):
    assert _analyze(tmp_path, "--xmi") == 0
    model = _model(tmp_path)
    tree = ET.parse(tmp_path / "model.xmi")
    uml = tree.getroot().find("uml:Model", NS)

    ids = [e.get(XMI_ID) for e in tree.iter() if e.get(XMI_ID)]
    assert len(ids) == len(set(ids)), "duplicate xmi:id"
    assert all(re.fullmatch(r"EA(ID|PK)_[0-9A-F]{8}(_[0-9A-F]{4}){3}_[0-9A-F]{12}", i) for i in ids)
    known = set(ids)
    for e in tree.iter():
        for attr in ("supplier", "client", XMI_IDREF, "subject", "package", "modelElement"):
            if e.get(attr):
                assert e.get(attr) in known, f"dangling {attr}={e.get(attr)}"
        for attr, value in e.attrib.items():
            if attr.startswith("base_"):
                assert value in known

    root = uml.find("packagedElement")
    assert root.get("name") == "alpha (as-built)"
    assert [p.get("name") for p in root.findall("packagedElement[@{%s}type='uml:Package']" % NS["xmi"])] == \
        ["BlackBox", "Interfaces", "Context"]

    def of_type(t):
        return [e for e in uml.iter("packagedElement") if e.get(XMI_TYPE) == t]

    provided = model["provided_interface"]
    interfaces_pkg = root.find("packagedElement[@name='Interfaces']")
    assert sorted(i.get("name") for i in interfaces_pkg.findall("packagedElement")) == \
        sorted(f["name"] for f in provided["functions"] + provided["globals"])
    assert len(of_type("uml:Realization")) == len(provided["functions"]) + len(provided["globals"])
    # EA names connector types its own way (t_connector.Connector_Type, as in the XMI EA 16 itself writes)
    ea_types = {c.find("properties").get("ea_type") for c in tree.getroot().find("xmi:Extension/connectors", NS)}
    assert ea_types == {"Realisation", "Usage", "Dependency"}

    out_edges = model["dependencies"]["outgoing"]
    provided_usrs = {f["usr"] for f in provided["functions"] + provided["globals"]}
    incoming_on_interface = [e for e in model["dependencies"]["incoming"] if e["usr"] in provided_usrs]
    assert len(of_type("uml:Usage")) == sum(e["kind"] != "include" for e in out_edges) + len(incoming_on_interface)

    others = {e["to"] for e in out_edges} | {e["from"] for e in model["dependencies"]["incoming"]}
    directions = {("out", e["to"]) for e in out_edges} | {("in", e["from"]) for e in model["dependencies"]["incoming"]}
    assert len(of_type("uml:Dependency")) == len(directions)
    assert len(root.find("packagedElement[@name='Context']").findall("packagedElement")) == len(others)

    diagram = tree.getroot().find("xmi:Extension/diagrams/diagram", NS)
    assert diagram.find("properties").get("name") == "alpha (as-built)"
    subjects = [e.get("subject") for e in diagram.find("elements")]
    assert len(subjects) == 1 + len(provided_usrs) + len(others)


def test_exports_of_different_modules_share_no_ids(tmp_path):
    assert _analyze(tmp_path / "a", "--xmi") == 0
    assert main(["analyze", "--repo", str(FIXTURE), "--module", "beta", "--profile", PROFILE,
                 "--out", str(tmp_path / "b"), "--xmi", "--context", "all"]) == 0
    ids = [set(e.get(XMI_ID) for e in ET.parse(tmp_path / d / "model.xmi").iter() if e.get(XMI_ID))
           for d in ("a", "b")]
    assert not (ids[0] & ids[1]), "beta appears in alpha's Context and alpha in beta's; EA would merge them"


def test_usage_carries_kind_and_evidence(tmp_path):
    assert _analyze(tmp_path, "--xmi") == 0
    model = _model(tmp_path)
    edge = next(e for e in model["dependencies"]["outgoing"] if e["kind"] == "call")
    tree = ET.parse(tmp_path / "model.xmi")
    connectors = tree.getroot().find("xmi:Extension/connectors", NS)
    match = [c for c in connectors
             if c.find("properties").get("stereotype") == "call" and c.find("target/model").get("name") == edge["symbol"]]
    assert len(match) == 1
    ev = edge["evidence"][0]
    doc = match[0].find("documentation").get("value")
    assert doc.splitlines()[0].startswith(f"{ev['file']}:{ev['line']}")
    tags = {t.get("name"): t.get("value") for t in match[0].find("tags")}
    assert tags["evidence_count"] == str(len(edge["evidence"]))
