"""Design model (Enterprise Architect `.qea`, read-only) and its comparison with the as-built model.

A `.qea` file is a SQLite database; archrev opens it read-only through a URI and reads three tables:
`t_package` (the package tree), `t_object` (components, ports, interfaces) and `t_operation`. Nothing is written,
no EA installation is needed, and `.eap` (Access) files are not supported.

For one module the design is: the Component named like the module in a `BlackBox` package (below `component_root`
when given), its ProvidedInterface ports (classifier: an Interface whose operations are the function names) and its
RequiredInterface ports, each either a signal (the Interface lives in a package named like one of `signal_packages`)
or a function interface.

Comparison rules (findings join the ordinary ones, so they can be baselined and gated):

    DESIGN-000  the module has no Component in the design model
    DESIGN-001  a provided function of the design is not exported by the code
    DESIGN-002  an exported function of the code is not provided in the design
    DESIGN-003  a required function interface of the design is not used by the code
    DESIGN-004  the code uses a signal or an App module's symbol the design does not require
    DESIGN-005  a required signal of the design is not accessed by the code
    DESIGN-006  a provided function is matched only through a module prefix alias (name differs)
"""

import hashlib
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from .canonical import guid
from .model import SIGNAL_PREFIX
from .rules import INTERFACE_KINDS, _finding

DESIGN_SCHEMA = "archrev.design_compare/1"
DEFAULT_SIGNAL_PACKAGES = ("Signals",)


class DesignError(Exception):
    pass


@dataclass(frozen=True)
class DesignInterface:
    name: str           # port name
    kind: str           # "function" or "signal"
    ops: tuple[str, ...]  # operation names of the classifier interface; empty without classifier
    path: str           # package path of the classifier interface, "" without one

    def names(self) -> tuple[str, ...]:
        """What the code would use: the operations, or the port name when there are none."""
        return self.ops or (self.name,)


@dataclass
class DesignModule:
    name: str
    path: str
    stereotype: str | None
    provided: list[DesignInterface] = field(default_factory=list)
    required: list[DesignInterface] = field(default_factory=list)


@dataclass
class DesignSpec:
    """The `design` section of a profile."""
    model: str                          # path of the .qea, relative to the repository
    component_root: str | None = None   # package path prefix that disambiguates same-named components
    signal_packages: tuple[str, ...] = DEFAULT_SIGNAL_PACKAGES


def design_hash(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def open_design(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise DesignError(f"design model not found: {path}")
    uri = f"file:{path.resolve().as_posix()}?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    try:
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        missing = {"t_package", "t_object", "t_operation"} - tables
        if missing:
            raise DesignError(f"{path.name} is not an Enterprise Architect model: "
                              f"missing {', '.join(sorted(missing))}")
    except sqlite3.DatabaseError as exc:
        con.close()
        raise DesignError(f"{path.name} is not a SQLite (.qea) model: {exc}") from None
    except DesignError:
        con.close()
        raise
    return con


def read_module(con: sqlite3.Connection, module: str, spec: DesignSpec) -> DesignModule | None:
    """The design of one module, or None when the model has no such component."""
    packages = {pid: (name, parent) for pid, name, parent in
                con.execute("SELECT Package_ID, Name, Parent_ID FROM t_package")}

    def path_of(pid) -> str:
        parts = []
        seen = set()
        while pid in packages and pid not in seen:
            seen.add(pid)
            name, pid = packages[pid]
            parts.append(name)
        return "/".join(reversed(parts))

    rows = con.execute(
        "SELECT o.Object_ID, o.Stereotype, o.Package_ID FROM t_object o JOIN t_package p ON p.Package_ID = o.Package_ID "
        "WHERE o.Name = ? AND o.Object_Type = 'Component' AND p.Name = 'BlackBox' ORDER BY o.Object_ID",
        (module,)).fetchall()
    candidates = [(oid, stereo, path_of(pkg)) for oid, stereo, pkg in rows]
    if spec.component_root:
        root = spec.component_root.rstrip("/") + "/"
        candidates = [c for c in candidates if c[2].startswith(root)]
    if not candidates:
        return None
    if len(candidates) > 1:
        raise DesignError(f"design model has {len(candidates)} components named {module} in a BlackBox package: "
                          f"{'; '.join(c[2] for c in candidates)}; set design.component_root in the profile")
    oid, stereo, path = candidates[0]
    design = DesignModule(module, path, stereo or None)

    ports = con.execute(
        "SELECT Name, Object_Type, Classifier FROM t_object WHERE ParentID = ? "
        "AND Object_Type IN ('ProvidedInterface', 'RequiredInterface') ORDER BY Object_Type, Name, Object_ID",
        (oid,)).fetchall()
    for name, typ, classifier in ports:
        iface = DesignInterface(name, "function", (), "")
        if classifier:
            found = con.execute("SELECT Name, Package_ID FROM t_object WHERE Object_ID = ?", (classifier,)).fetchone()
            if found:
                ipath = path_of(found[1])
                ops = tuple(r[0] for r in con.execute(
                    "SELECT Name FROM t_operation WHERE Object_ID = ? ORDER BY Pos, Name, OperationID", (classifier,)))
                kind = "signal" if set(ipath.split("/")) & set(spec.signal_packages) else "function"
                iface = DesignInterface(name, kind, ops, ipath)
        (design.provided if typ == "ProvidedInterface" else design.required).append(iface)
    return design


def _strip_prefix(name: str, prefixes: list[str]) -> str:
    for p in sorted(prefixes, key=len, reverse=True):
        if name.startswith(p) and len(name) > len(p):
            return name[len(p):]
    return name


def compare(model: dict, design: DesignModule | None, profile, design_file: str) -> tuple[list[dict], dict]:
    """DESIGN findings and the canonical comparison record for design_compare.json."""
    target = model["target"]
    module = target["name"]
    sev = profile.severity
    findings: list[dict] = []
    record: dict = {"schema": DESIGN_SCHEMA, "module": module, "design_file": design_file, "found": design is not None}
    if design is None:
        findings.append(_finding("DESIGN-000", sev["DESIGN-000"], module, None, None, module,
                                 f"{module} has no Component in a BlackBox package of the design model",
                                 element=f"design:{module}"))
        return findings, record

    prefixes = [f"{module}_"] + list(profile.prefix_aliases.get(module, []))
    code_functions = {f["name"]: f for f in model["provided_interface"]["functions"]}
    by_stem: dict[str, list[str]] = {}
    for name in code_functions:
        by_stem.setdefault(_strip_prefix(name, prefixes), []).append(name)

    provided_rows = []
    matched_code: set[str] = set()
    for iface in design.provided:
        for uml_name in iface.names():
            if uml_name in code_functions:
                how, code_name = "exact", uml_name
            else:
                alias = sorted(by_stem.get(_strip_prefix(uml_name, prefixes), []))
                how, code_name = ("alias", alias[0]) if alias else ("missing", None)
            if code_name and not code_functions[code_name]["defined_at"]:
                how = "declared_only"  # a prototype in the module's header without a definition in its sources
            provided_rows.append({"design": uml_name, "code": code_name, "how": how, "port": iface.name})
            if code_name:
                matched_code.add(code_name)
            if how == "missing":
                findings.append(_finding(
                    "DESIGN-001", sev["DESIGN-001"], module, None, None, uml_name,
                    f"the design provides '{uml_name}' (port {iface.name}) but the code does not export it",
                    element=f"design:{design.path}:{uml_name}"))
            elif how == "declared_only":
                f = code_functions[code_name]
                findings.append(_finding(
                    "DESIGN-001", sev["DESIGN-001"], module, _file_of(f), _line_of(f), uml_name,
                    f"the design provides '{uml_name}' (port {iface.name}); the code only declares "
                    f"'{code_name}' in {', '.join(f['declared_in'])} and does not define it",
                    element=f"design:{design.path}:{uml_name}", discriminator="declared_only"))
            elif how == "alias":
                findings.append(_finding(
                    "DESIGN-006", sev["DESIGN-006"], module, _file_of(code_functions[code_name]), _line_of(code_functions[code_name]),
                    code_name, f"'{uml_name}' in the design is '{code_name}' in the code (matched through a prefix alias)",
                    element=f"design:{design.path}:{uml_name}"))
    # Only defined functions are exports; a stray prototype is not something the design should provide.
    code_only = sorted(n for n, f in code_functions.items() if n not in matched_code and f["defined_at"])
    for name in code_only:
        f = code_functions[name]
        findings.append(_finding(
            "DESIGN-002", sev["DESIGN-002"], module, _file_of(f), _line_of(f), name,
            f"the code exports '{name}' but the design does not provide it",
            element=f"design:{design.path}:{name}"))

    used_symbols: dict[str, dict] = {}
    app_symbols: dict[str, dict] = {}
    kinds = {m["id"]: m["kind"] for m in model["modules"]}
    for e in model["dependencies"]["outgoing"]:
        if e["kind"] == "include" or e["kind"].startswith("signal_"):
            continue
        used_symbols.setdefault(e["symbol"], e)
        if e["kind"] in INTERFACE_KINDS and kinds.get(e["to"]) in ("c_module", "c_module_unparsed", "not_in_build",
                                                                   "unknown"):
            app_symbols.setdefault(e["symbol"], e)
    accessed = {s["name"]: s for s in model["signals"]}
    # Signal accesses are edges to the signal, not uses of the accessor macro; a required interface whose
    # operations are accessor macros (SP_Get_X) is satisfied by the matching access.
    if profile.signal_pool is not None:
        for s in model["signals"]:
            for acc in profile.signal_pool.accessors:
                if acc.access in s["target_access"]:
                    used_symbols.setdefault(acc.macro.replace("{signal}", s["name"]), {"signal": s["name"]})

    required_functions, required_signals = [], []
    required_symbol_names: set[str] = set()
    required_signal_names: set[str] = set()
    for iface in design.required:
        if iface.kind == "signal":
            required_signal_names.add(iface.name)
            access = accessed.get(iface.name)
            required_signals.append({"signal": iface.name, "accessed": access["target_access"] if access else []})
            if access is None:
                findings.append(_finding(
                    "DESIGN-005", sev["DESIGN-005"], module, None, None, iface.name,
                    f"the design requires signal '{iface.name}' but the code does not access it",
                    element=f"design:{design.path}:{iface.name}"))
        else:
            names = iface.names()
            required_symbol_names.update(names)
            used = sorted(n for n in names if n in used_symbols)
            required_functions.append({"port": iface.name, "operations": list(names), "used": used})
            if not used:
                findings.append(_finding(
                    "DESIGN-003", sev["DESIGN-003"], module, None, None, iface.name,
                    f"the design requires '{iface.name}' ({', '.join(names)}) but the code does not use it",
                    element=f"design:{design.path}:{iface.name}"))

    code_only_signals = sorted(set(accessed) - required_signal_names)
    for name in code_only_signals:
        edge = next(e for e in model["dependencies"]["outgoing"] if e["to"] == SIGNAL_PREFIX + name)
        ev = edge["evidence"][0]
        findings.append(_finding(
            "DESIGN-004", sev["DESIGN-004"], module, ev["file"], ev["line"], name,
            f"the code accesses signal '{name}' ({', '.join(accessed[name]['target_access'])}) "
            "which the design does not require", evidence=[ev], element=edge["id"], discriminator="signal"))
    code_only_symbols = sorted(set(app_symbols) - required_symbol_names)
    for name in code_only_symbols:
        edge = app_symbols[name]
        ev = edge["evidence"][0]
        findings.append(_finding(
            "DESIGN-004", sev["DESIGN-004"], module, ev["file"], ev["line"], name,
            f"the code uses '{name}' of module {_module_name(edge['to'])} which the design does not require",
            evidence=[ev], element=edge["id"], discriminator="symbol"))

    record.update({
        "design": {"path": design.path, "stereotype": design.stereotype,
                   "provided": [{"port": i.name, "operations": list(i.ops), "interface_path": i.path}
                                for i in design.provided],
                   "required": [{"port": i.name, "kind": i.kind, "operations": list(i.ops), "interface_path": i.path}
                                for i in design.required]},
        "provided": provided_rows,
        "code_only_exports": code_only,
        "required_functions": required_functions,
        "required_signals": required_signals,
        "code_only_signals": code_only_signals,
        "code_only_app_symbols": code_only_symbols,
    })
    return findings, record


def _file_of(f: dict) -> str | None:
    where = f["defined_at"] or (f["declared_in"][0] if f["declared_in"] else "")
    return where.rpartition(":")[0] or None


def _line_of(f: dict) -> int | None:
    where = f["defined_at"] or (f["declared_in"][0] if f["declared_in"] else "")
    line = where.rpartition(":")[2]
    return int(line) if line.isdigit() else None


def _module_name(module_id: str) -> str:
    from .model import UNKNOWN_PREFIX, module_name
    if module_id.startswith(UNKNOWN_PREFIX):
        return "a module this run did not parse"
    return module_name(module_id)


__all__ = ["DESIGN_SCHEMA", "DesignError", "DesignInterface", "DesignModule", "DesignSpec", "compare",
           "design_hash", "guid", "open_design", "read_module"]
