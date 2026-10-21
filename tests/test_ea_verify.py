"""Offline checks of tools/ea_verify.py: the parts that need no Enterprise Architect."""

import importlib.util
import sqlite3
from pathlib import Path

import pytest

from archcreator.cli import main

FIXTURE = Path(__file__).parent / "fixtures" / "fx01"
PROFILE = str(FIXTURE / "profile.yaml")
SPEC = importlib.util.spec_from_file_location("ea_verify", Path(__file__).parents[1] / "tools" / "ea_verify.py")
ea_verify = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ea_verify)


@pytest.fixture
def xmi(tmp_path):
    assert main(["analyze", "--repo", str(FIXTURE), "--module", "alpha", "--profile", PROFILE, "--out", str(tmp_path),
                 "--context", "all", "--xmi"]) == 0
    return tmp_path / "model.xmi"


def test_expected_counts_come_from_the_file(xmi):
    want = ea_verify.expected(xmi)
    assert want["root"] == "alpha (as-built)"
    assert want["connector_types"].keys() == {"Realisation", "Usage", "Dependency"}
    assert want["diagrams"] == 1 and want["diagram_objects"] >= 2
    assert want["element_types"]["Component"] >= 2 and want["element_types"]["Interface"] >= 1
    assert want["operations"] + want["attributes"] >= 1


def test_stored_reads_what_an_ea_model_would_hold(tmp_path):
    """The SQL runs against the table layout of EA's .qea (columns as in EABase.qea)."""
    qea = tmp_path / "m.qea"
    db = sqlite3.connect(qea)
    db.executescript("""
        create table t_package(Package_ID, Name, Parent_ID);
        create table t_object(Object_ID, Object_Type, Name, Stereotype, Package_ID, ea_guid);
        create table t_connector(Connector_ID, Connector_Type, Stereotype, Start_Object_ID, End_Object_ID);
        create table t_objectproperties(Object_ID, Property, Value);
        create table t_connectortag(ElementID, Property, VALUE);
        create table t_operation(Object_ID, Name);
        create table t_attribute(Object_ID, Name);
        create table t_diagram(Diagram_ID, Package_ID);
        create table t_diagramobjects(Diagram_ID, Object_ID);
        insert into t_package values (1,'Model',0),(2,'T',1),(3,'alpha (as-built)',2),(4,'Other',1);
        insert into t_object values (9,'Package','alpha (as-built)',null,3,'gp'),(10,'Component','a','as-built',3,'g1'),(11,'Interface','f','Function',3,'g2'),
                                    (12,'Component','x',null,4,'g3');
        insert into t_connector values (20,'Realisation',null,10,11),(21,'Usage','call',10,11),(22,'Usage',null,12,12);
        insert into t_objectproperties values (10,'k','v');
        insert into t_connectortag values (21,'e','1');
        insert into t_operation values (11,'f');
        insert into t_diagram values (30,3);
        insert into t_diagramobjects values (30,10),(30,11);
    """)
    db.commit()
    db.close()
    got = ea_verify.stored(qea, ["alpha (as-built)"])
    assert got["element_types"] == {"Component": 1, "Interface": 1}
    assert got["connector_types"] == {"Realisation": 1, "Usage": 1}
    assert got["connector_stereos"] == {"call": 1}
    assert (got["element_tags"], got["connector_tags"], got["operations"], got["diagrams"], got["diagram_objects"]) == \
        (1, 1, 1, 1, 2)


def test_only_a_qea_inside_the_run_folder_is_opened(tmp_path):
    ea_verify.guard_model(tmp_path / "scratch.qea", tmp_path)
    with pytest.raises(SystemExit):
        ea_verify.guard_model(Path(r"C:\Program Files\Sparx Systems\EA\EABase.qea"), tmp_path)
    with pytest.raises(SystemExit):
        ea_verify.guard_model(tmp_path / "scratch.eap", tmp_path)
    with pytest.raises(SystemExit):
        ea_verify.guard_model(tmp_path / ".." / "other.qea", tmp_path)


def test_import_phase_needs_go(capsys):
    with pytest.raises(SystemExit) as e:
        ea_verify.main(["import"])
    assert e.value.code == 2


def test_compare_is_exact_and_checks_guids(xmi, tmp_path):
    want = ea_verify.expected(xmi)
    got = {k: (dict(v) if isinstance(v, dict) else v) for k, v in want.items() if k != "element_ids"}
    got["guids"] = sorted(want["element_ids"])
    got["element_guids"] = {i.upper() for i in want["element_ids"]}
    rep = ea_verify.Report(tmp_path)
    assert ea_verify.compare(rep, "VI1", [want], got)
    # an import that left an extra element behind (second import duplicated instead of replacing) must fail
    got["element_types"]["Interface"] += 1
    assert not ea_verify.compare(rep, "VI2", [want], got)
    got["element_types"]["Interface"] -= 1
    got["element_guids"] = {i + "X" for i in got["element_guids"]}  # all ids renewed, same counts
    assert not ea_verify.compare(rep, "VI2", [want], got)
