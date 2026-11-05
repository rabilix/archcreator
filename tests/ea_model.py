"""A minimal Enterprise Architect model for offline tests: EA's table and column names, only the columns used."""

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE t_package (Package_ID INTEGER PRIMARY KEY, Name TEXT, Parent_ID INTEGER, Notes TEXT, ea_guid TEXT);
CREATE TABLE t_object (Object_ID INTEGER PRIMARY KEY, Object_Type TEXT, Name TEXT, Note TEXT, Package_ID INTEGER,
                       Stereotype TEXT, PDATA1 TEXT, ParentID INTEGER, ea_guid TEXT);
CREATE TABLE t_connector (Connector_ID INTEGER PRIMARY KEY, Name TEXT, Notes TEXT, Connector_Type TEXT,
                          Start_Object_ID INTEGER, End_Object_ID INTEGER, Stereotype TEXT, ea_guid TEXT);
CREATE TABLE t_objectproperties (PropertyID INTEGER PRIMARY KEY, Object_ID INTEGER, Property TEXT, Value TEXT,
                                 Notes TEXT, ea_guid TEXT);
CREATE TABLE t_connectortag (PropertyID INTEGER PRIMARY KEY, ElementID INTEGER, Property TEXT, VALUE TEXT,
                             NOTES TEXT, ea_guid TEXT);
CREATE TABLE t_diagram (Diagram_ID INTEGER PRIMARY KEY, Package_ID INTEGER, Name TEXT, ea_guid TEXT);
CREATE TABLE t_operation (OperationID INTEGER PRIMARY KEY, Object_ID INTEGER, Name TEXT, ea_guid TEXT);
CREATE TABLE t_attribute (ID INTEGER PRIMARY KEY, Object_ID INTEGER, Name TEXT, ea_guid TEXT);
INSERT INTO t_package VALUES (1, 'Model', 0, '', '{00000000-0000-0000-0000-000000000001}');
INSERT INTO t_object (Object_ID, Object_Type, Name, Package_ID, PDATA1, ea_guid)
    VALUES (1, 'Package', 'Model', 0, '1', '{00000000-0000-0000-0000-000000000001}');
"""


def make_model(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    con.commit()
    con.close()
    return path
