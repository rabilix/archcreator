"""Applies ea-apply operations to a test model the way EA's Automation Interface would store them (SQLite rows).

Stands in for the PowerShell driver in offline tests: same operation set, same addressing (guid / key /
model_root), one result line per operation.
"""

import sqlite3
import uuid
from pathlib import Path


def apply(model: Path, operations: list[dict], fail_at: int | None = None) -> list[dict]:
    con = sqlite3.connect(model)
    made: dict[str, tuple[str, int]] = {}  # key -> (kind, id): what this run created
    lines = []
    try:
        for op in operations:
            if fail_at is not None and op["n"] >= fail_at:
                lines.append({"n": op["n"], "ok": False, "detail": "simulated EA failure"})
                break
            _one(con, op, made)
            con.commit()
            lines.append({"n": op["n"], "ok": True, "detail": op["op"]})
    finally:
        con.close()
    return lines


def _guid() -> str:
    return "{" + str(uuid.uuid4()).upper() + "}"


def _resolve(con, ref: dict, made: dict, want: str) -> int:
    """Package_ID (want='package') or Object_ID (want='element') of a reference."""
    if ref.get("model_root"):
        return 1
    if "key" in ref:
        return made[ref["key"]][1]
    if want == "package":
        return con.execute("SELECT Package_ID FROM t_package WHERE ea_guid = ?", (ref["guid"],)).fetchone()[0]
    return con.execute("SELECT Object_ID FROM t_object WHERE ea_guid = ?", (ref["guid"],)).fetchone()[0]


def _set_tags(con, kind: str, row: int, tags: dict) -> None:
    table, owner, value = (("t_connectortag", "ElementID", "VALUE") if kind == "connector"
                           else ("t_objectproperties", "Object_ID", "Value"))
    for name, val in tags.items():
        hit = con.execute(f"SELECT PropertyID FROM {table} WHERE {owner} = ? AND Property = ?", (row, name)).fetchone()
        if hit:
            con.execute(f"UPDATE {table} SET {value} = ? WHERE PropertyID = ?", (val, hit[0]))
        else:
            con.execute(f"INSERT INTO {table} ({owner}, Property, {value}) VALUES (?, ?, ?)", (row, name, val))


def _package_element(con, package_id: int) -> int:
    return con.execute("SELECT Object_ID FROM t_object WHERE Object_Type = 'Package' AND PDATA1 = ?",
                       (str(package_id),)).fetchone()[0]


def _one(con, op: dict, made: dict) -> None:
    kind = op.get("kind")
    if op["op"] == "create":
        guid = _guid()
        if kind == "package":
            parent = _resolve(con, op["parent"], made, "package")
            pid = con.execute("INSERT INTO t_package (Name, Parent_ID, Notes, ea_guid) VALUES (?, ?, ?, ?)",
                              (op["name"], parent, op["notes"], guid)).lastrowid
            oid = con.execute("INSERT INTO t_object (Object_Type, Name, Package_ID, PDATA1, Stereotype, ea_guid) "
                              "VALUES ('Package', ?, ?, ?, ?, ?)", (op["name"], parent, str(pid), op["stereotype"],
                                                                     guid)).lastrowid
            _set_tags(con, kind, oid, op["tags"])
            made[op["key"]] = ("package", pid)
        elif kind == "element":
            parent = _resolve(con, op["parent"], made, "package")
            oid = con.execute("INSERT INTO t_object (Object_Type, Name, Note, Package_ID, Stereotype, ea_guid) "
                              "VALUES (?, ?, ?, ?, ?, ?)", (op["type"], op["name"], op["notes"], parent,
                                                            op["stereotype"], guid)).lastrowid
            _set_tags(con, kind, oid, op["tags"])
            if op.get("feature"):
                _add_feature(con, oid, op["feature"])
            made[op["key"]] = ("element", oid)
        else:
            start = _resolve(con, op["client"], made, "element")
            end = _resolve(con, op["supplier"], made, "element")
            cid = con.execute("INSERT INTO t_connector (Name, Notes, Connector_Type, Start_Object_ID, End_Object_ID, "
                              "Stereotype, ea_guid) VALUES (?, ?, ?, ?, ?, ?, ?)",
                              (op["name"], op["notes"], op["type"], start, end, op["stereotype"], guid)).lastrowid
            _set_tags(con, kind, cid, op["tags"])
            made[op["key"]] = ("connector", cid)
    elif op["op"] in ("update", "mark_stale"):
        guid = op["guid"]
        if kind == "package":
            pid = con.execute("SELECT Package_ID FROM t_package WHERE ea_guid = ?", (guid,)).fetchone()[0]
            con.execute("UPDATE t_package SET Name = ?, Notes = COALESCE(?, Notes) WHERE Package_ID = ?",
                        (op["name"], op.get("notes"), pid))
            oid = _package_element(con, pid)
            con.execute("UPDATE t_object SET Name = ?, Stereotype = ? WHERE Object_ID = ?",
                        (op["name"], op["stereotype"], oid))
            _set_tags(con, kind, oid, op["tags"])
        elif kind == "element":
            oid = con.execute("SELECT Object_ID FROM t_object WHERE ea_guid = ?", (guid,)).fetchone()[0]
            con.execute("UPDATE t_object SET Name = ?, Stereotype = ?, Note = COALESCE(?, Note) WHERE Object_ID = ?",
                        (op["name"], op["stereotype"], op.get("notes"), oid))
            _set_tags(con, kind, oid, op["tags"])
        else:
            cid = con.execute("SELECT Connector_ID FROM t_connector WHERE ea_guid = ?", (guid,)).fetchone()[0]
            con.execute("UPDATE t_connector SET Name = ?, Stereotype = ?, Notes = COALESCE(?, Notes) "
                        "WHERE Connector_ID = ?", (op["name"], op["stereotype"], op.get("notes"), cid))
            _set_tags(con, kind, cid, op["tags"])
    elif op["op"] == "add_feature":
        oid = con.execute("SELECT Object_ID FROM t_object WHERE ea_guid = ?", (op["guid"],)).fetchone()[0]
        _add_feature(con, oid, op["feature"])
    elif op["op"] == "rename_feature":
        oid = con.execute("SELECT Object_ID FROM t_object WHERE ea_guid = ?", (op["guid"],)).fetchone()[0]
        table = "t_operation" if op["feature"]["kind"] == "operation" else "t_attribute"
        con.execute(f"UPDATE {table} SET Name = ? WHERE Object_ID = ? AND Name = ?", (op["feature"]["name"], oid,
                                                                                       op["old"]))
    else:
        raise AssertionError(f"unknown operation {op['op']}")


def _add_feature(con, oid: int, feature: dict) -> None:
    table = "t_operation" if feature["kind"] == "operation" else "t_attribute"
    con.execute(f"INSERT INTO {table} (Object_ID, Name, ea_guid) VALUES (?, ?, ?)", (oid, feature["name"], _guid()))
