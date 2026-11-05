"""What an earlier ea-apply left in a copy: every object with an `archcreator_id`, read with SQLite (plan section 7).

Read-only (`mode=ro`). EA keeps a package twice, as a row of t_package and as its element in t_object (Object_Type
'Package', PDATA1 = Package_ID); a package's tags live on that element.
"""

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

ID_TAG = "archcreator_id"
MODULE_TAG = "archcreator_module"
STALE_TAG = "archcreator_stale"
STEREOTYPE_TAG = "archcreator_stereotype"


class IdentityError(Exception):
    """The copy cannot be updated safely (duplicate ids); the message names the places."""


@dataclass
class Found:
    key: str
    kind: str                     # package | element | connector
    guid: str
    name: str
    stereotype: str
    notes: str
    tags: dict[str, str]
    parent: int | None            # Package_ID that holds it (package: its parent package; connector: None)
    package_id: int | None = None  # for a package: its own Package_ID
    features: list[str] = field(default_factory=list)  # operation and attribute names of an element


@dataclass
class Copy:
    objects: dict[str, Found]
    parents: dict[int, int]       # Package_ID -> Parent_ID

    def ancestors(self, package_id: int | None) -> list[int]:
        chain, seen = [], set()
        while package_id and package_id not in seen:
            chain.append(package_id)
            seen.add(package_id)
            package_id = self.parents.get(package_id)
        return chain


def read(model: Path) -> Copy:
    con = sqlite3.connect(f"{model.resolve().as_uri()}?mode=ro", uri=True, timeout=1)
    try:
        parents = {pid: parent for pid, parent in con.execute("SELECT Package_ID, Parent_ID FROM t_package")}
        etags: dict[int, dict[str, str]] = {}
        for oid, prop, value in con.execute("SELECT Object_ID, Property, Value FROM t_objectproperties"):
            etags.setdefault(oid, {})[prop] = value or ""
        ctags: dict[int, dict[str, str]] = {}
        for cid, prop, value in con.execute("SELECT ElementID, Property, VALUE FROM t_connectortag"):
            ctags.setdefault(cid, {})[prop] = value or ""
        features: dict[int, list[str]] = {}
        for oid, name in con.execute("SELECT Object_ID, Name FROM t_operation UNION ALL "
                                     "SELECT Object_ID, Name FROM t_attribute"):
            features.setdefault(oid, []).append(name or "")
        packages = {pid: (name, notes, guid) for pid, name, notes, guid in
                    con.execute("SELECT Package_ID, Name, Notes, ea_guid FROM t_package")}
        found: list[Found] = []
        for oid, otype, name, note, pkg, stereo, pdata1, guid in con.execute(
                "SELECT Object_ID, Object_Type, Name, Note, Package_ID, Stereotype, PDATA1, ea_guid FROM t_object"):
            tags = etags.get(oid, {})
            if ID_TAG not in tags:
                continue
            if otype == "Package" and pdata1 and str(pdata1).isdigit() and int(pdata1) in packages:
                pid = int(pdata1)
                pname, pnotes, pguid = packages[pid]
                found.append(Found(tags[ID_TAG], "package", pguid or guid, pname or "", stereo or "", pnotes or "",
                                   tags, parents.get(pid), package_id=pid))
            else:
                found.append(Found(tags[ID_TAG], "element", guid, name or "", stereo or "", note or "", tags, pkg,
                                   features=sorted(features.get(oid, []))))
        for cid, name, notes, stereo, guid in con.execute(
                "SELECT Connector_ID, Name, Notes, Stereotype, ea_guid FROM t_connector"):
            tags = ctags.get(cid, {})
            if ID_TAG in tags:
                found.append(Found(tags[ID_TAG], "connector", guid, name or "", stereo or "", notes or "", tags, None))
    finally:
        con.close()
    objects: dict[str, Found] = {}
    for f in found:
        if f.key in objects:
            raise IdentityError(f"two objects in the copy carry {ID_TAG} = {f.key!r} ({objects[f.key].guid} and "
                                f"{f.guid}); remove or retag one in EA, then run again")
        objects[f.key] = f
    return Copy(objects, parents)
