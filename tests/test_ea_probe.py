"""Offline checks of tools/ea_probe.py (increment 0 of ea-apply): the read-back and its verdicts, and the driver."""

import importlib.util
import re
import sqlite3
import sys
from pathlib import Path

TOOLS = Path(__file__).parents[1] / "tools"
sys.path.insert(0, str(TOOLS))
SPEC = importlib.util.spec_from_file_location("ea_probe", TOOLS / "ea_probe.py")
ea_probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ea_probe)

from ea_model import make_model  # noqa: E402


def _as_ea_would_store(path: Path) -> Path:
    make_model(path)
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE t_diagramobjects (Diagram_ID INTEGER, Object_ID INTEGER, RectTop INTEGER, RectLeft INTEGER,
                                       RectRight INTEGER, RectBottom INTEGER);
        INSERT INTO t_package VALUES (2, 'ArchCreatorProbe', 1, '', '{P}');
        INSERT INTO t_object (Object_ID, Object_Type, Name, Package_ID, PDATA1, ea_guid)
            VALUES (2, 'Package', 'ArchCreatorProbe', 1, '2', '{P}');
        INSERT INTO t_objectproperties (Object_ID, Property, Value) VALUES (2, 'archcreator_id', 'pkg:probe');
        INSERT INTO t_object (Object_ID, Object_Type, Name, Stereotype, Note, Package_ID) VALUES
            (3, 'Component', 'ProbeComp [stale]', 'stale', 'changed', 2),
            (4, 'Interface', 'ProbeFn', 'Function', '', 2),
            (5, 'Interface', 'ProbeVar', 'Variable', '', 2),
            (6, 'Component', 'ProbeOther', 'c_module', '', 2);
        INSERT INTO t_operation (Object_ID, Name) VALUES (4, 'ProbeFn');
        INSERT INTO t_attribute (Object_ID, Name) VALUES (5, 'ProbeVar');
        INSERT INTO t_connector (Connector_ID, Connector_Type, Stereotype, Start_Object_ID, End_Object_ID, Notes) VALUES
            (1, 'Realisation', '', 3, 4, ''), (2, 'Realisation', '', 3, 5, ''), (3, 'Usage', 'call', 3, 4, ''),
            (4, 'Dependency', 'as-built', 3, 6, '');
        INSERT INTO t_connectortag (ElementID, Property, VALUE) VALUES
            (1, 'archcreator_id', 'conn:real'), (2, 'archcreator_id', 'conn:real_uml'),
            (3, 'archcreator_id', 'conn:use'), (4, 'archcreator_id', 'conn:dep');
        INSERT INTO t_diagram VALUES (1, 2, 'ProbeComp (as-built)', '{D}');
        INSERT INTO t_diagramobjects VALUES (1, 3, -40, 600, 820, -100), (1, 4, -40, 320, 540, -100);
    """)
    con.commit()
    con.close()
    return path


def test_a_complete_store_passes_every_check(tmp_path):
    rep = ea_probe.Report(tmp_path)
    ea_probe.judge(rep, ea_probe.read_back(_as_ea_would_store(tmp_path / "scratch.qea")))
    assert [r["step"] for r in rep.rows if r["status"] == "FAIL"] == []
    assert {r["step"] for r in rep.rows} >= {"S1", "S2", "S3", "S6", "S7", "S8", "S9"}


def test_a_missing_package_fails(tmp_path):
    rep = ea_probe.Report(tmp_path)
    ea_probe.judge(rep, ea_probe.read_back(make_model(tmp_path / "empty.qea")))
    assert [r["status"] for r in rep.rows] == ["FAIL"]


def test_the_driver_opens_only_the_job_model_and_deletes_nothing():
    driver = ea_probe.DRIVER
    assert re.findall(r"OpenFile\(([^)]*)\)", driver) == ["$j.model"]
    for forbidden in ("Delete", "Invoke-Expression", ".Execute(", "SQLQuery", "RunModelSearch"):
        assert forbidden not in driver


def test_dry_run_writes_job_and_driver_and_starts_nothing(tmp_path):
    assert ea_probe.main(["--dry-run", "--out", str(tmp_path)]) == 0
    run = next(tmp_path.iterdir())
    assert {p.name for p in run.iterdir()} >= {"job.json", "driver.ps1", "report.txt"}
