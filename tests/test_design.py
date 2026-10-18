"""Design comparison against a synthetic Enterprise Architect .qea (the three tables archrev reads)."""

import json
import shutil
import sqlite3
from pathlib import Path

import pytest

from archrev.cli import main
from archrev.design import DesignError, DesignSpec, open_design, read_module
from archrev.pipeline import EXIT_OK, EXIT_USAGE

FIXTURE = Path(__file__).parent / "fixtures" / "fx02"
PROFILE = FIXTURE / "profile.yaml"
ROOT = "Model/Component/MCU/Flex/APPL"


class Qea:
    """Builds a minimal .qea: packages, components with ports, interfaces with operations."""

    def __init__(self, path: Path):
        self.path = path
        self.con = sqlite3.connect(path)
        self.con.executescript("""
            CREATE TABLE t_package (Package_ID INTEGER, Name TEXT, Parent_ID INTEGER);
            CREATE TABLE t_object (Object_ID INTEGER, Object_Type TEXT, Name TEXT, Stereotype TEXT,
                                   Package_ID INTEGER, ParentID INTEGER, Classifier INTEGER);
            CREATE TABLE t_operation (OperationID INTEGER, Object_ID INTEGER, Name TEXT, Pos INTEGER);
        """)
        self._pid = 0
        self._oid = 0
        self._opid = 0
        self.packages: dict[str, int] = {}

    def package(self, path: str) -> int:
        if path in self.packages:
            return self.packages[path]
        parent = self.package(path.rsplit("/", 1)[0]) if "/" in path else 0
        self._pid += 1
        self.con.execute("INSERT INTO t_package VALUES (?, ?, ?)", (self._pid, path.rsplit("/", 1)[-1], parent))
        self.packages[path] = self._pid
        return self._pid

    def object(self, typ: str, name: str, package: str, stereotype=None, parent=0, classifier=0) -> int:
        self._oid += 1
        self.con.execute("INSERT INTO t_object VALUES (?, ?, ?, ?, ?, ?, ?)",
                         (self._oid, typ, name, stereotype, self.package(package), parent, classifier))
        return self._oid

    def interface(self, name: str, package: str, ops: list[str]) -> int:
        oid = self.object("Interface", name, package, "Function")
        for i, op in enumerate(ops):
            self._opid += 1
            self.con.execute("INSERT INTO t_operation VALUES (?, ?, ?, ?)", (self._opid, oid, op, i))
        return oid

    def component(self, name: str, root: str = ROOT, provided=(), required=(), stereotype="ASIL B") -> int:
        cid = self.object("Component", name, f"{root}/{name}/BlackBox", stereotype)
        for port, classifier in provided:
            self.object("ProvidedInterface", port, f"{root}/{name}/BlackBox", parent=cid, classifier=classifier)
        for port, classifier in required:
            self.object("RequiredInterface", port, f"{root}/{name}/BlackBox", parent=cid, classifier=classifier)
        return cid

    def done(self) -> Path:
        self.con.commit()
        self.con.close()
        return self.path


def _qea(tmp_path: Path) -> Path:
    q = Qea(tmp_path / "design.qea")
    main_if = q.interface("alpha_main", f"{ROOT}/alpha/Interfaces", ["alpha_main"])
    helper_if = q.interface("alpha_helper", f"{ROOT}/alpha/Interfaces", ["alpha_helper"])
    reset_if = q.interface("alpha_reset", f"{ROOT}/alpha/Interfaces", ["alpha_reset"])
    params_if = q.interface("alpha_params", f"{ROOT}/alpha/Interfaces", ["alpha_params"])
    beta_if = q.interface("beta_main", f"{ROOT}/beta/Interfaces", ["beta_main"])
    rte_if = q.interface("Rte_Read_Mode", "Model/Component/MCU/BSW/RTE/Interfaces", ["Rte_Read_Mode"])
    # A function interface whose operations are accessor macros: satisfied by the signal access itself.
    get_if = q.interface("Signal_Get_IF", "Model/Component/MCU/Flex/SYSTEM/Interfaces", ["SP_Get_Temp", "SP_Get_Nope"])
    temp = q.interface("Temp", "Model/SYSTEM/Signals/Interfaces", ["SP_Get_Temp", "SP_Set_Temp"])
    orphan = q.interface("Orphan", "Model/SYSTEM/Signals/Interfaces", ["SP_Get_Orphan"])
    missing = q.interface("Missing_Sig", "Model/SYSTEM/Signals/Interfaces", ["SP_Get_Missing_Sig"])
    q.component("alpha",
                provided=[("alpha_main", main_if), ("alpha_helper", helper_if), ("alpha_reset", reset_if),
                          ("alpha_params", params_if), ("alpha_notify", 0)],
                required=[("beta_main", beta_if), ("Rte_Read_Mode", rte_if), ("Signal_Get_IF", get_if),
                          ("Temp", temp), ("Orphan", orphan), ("Missing_Sig", missing)])
    # A same-named component in another subsystem: the component root must tell them apart.
    q.component("alpha", root="Model/Component/SCU/appl")
    q.component("gamma", provided=[], required=[])
    return q.done()


def _analyze(out: Path, module: str, *extra: str, repo: Path = FIXTURE, profile: Path = PROFILE) -> int:
    return main(["analyze", "--repo", str(repo), "--module", module, "--profile", str(profile), "--out", str(out),
                 "--context", "all", *extra])


def _json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _profile_with_design(tmp_path: Path, qea: Path, root: str | None = ROOT) -> tuple[Path, Path]:
    repo = tmp_path / "fx02"
    shutil.copytree(FIXTURE, repo)
    shutil.copy(qea, repo / "design.qea")
    text = PROFILE.read_text(encoding="utf-8") + "design:\n  model: design.qea\n"
    if root:
        text += f"  component_root: {root}\n"
    (repo / "profile.yaml").write_text(text, encoding="utf-8")
    return repo, repo / "profile.yaml"


def test_read_module_needs_the_component_root_when_names_repeat(tmp_path):
    con = open_design(_qea(tmp_path))
    with pytest.raises(DesignError, match="2 components named alpha"):
        read_module(con, "alpha", DesignSpec("design.qea"))
    design = read_module(con, "alpha", DesignSpec("design.qea", ROOT))
    assert design.path == f"{ROOT}/alpha/BlackBox" and design.stereotype == "ASIL B"
    assert [(i.name, i.ops) for i in design.provided] == [
        ("alpha_helper", ("alpha_helper",)), ("alpha_main", ("alpha_main",)), ("alpha_notify", ()),
        ("alpha_params", ("alpha_params",)), ("alpha_reset", ("alpha_reset",))]
    assert [(i.name, i.kind) for i in design.required] == [
        ("Missing_Sig", "signal"), ("Orphan", "signal"), ("Rte_Read_Mode", "function"),
        ("Signal_Get_IF", "function"), ("Temp", "signal"), ("beta_main", "function")]
    assert read_module(con, "nobody", DesignSpec("design.qea", ROOT)) is None
    con.close()


def test_design_findings_for_alpha(tmp_path):
    repo, profile = _profile_with_design(tmp_path, _qea(tmp_path))
    out = tmp_path / "out"
    assert _analyze(out, "alpha", repo=repo, profile=profile) == EXIT_OK
    findings = _json(out / "findings.json")["findings"]
    design = {(f["rule_id"], f["symbol"]) for f in findings if f["rule_id"].startswith("DESIGN")}
    assert design == {
        ("DESIGN-001", "alpha_reset"),        # provided in the design, not exported
        ("DESIGN-001", "alpha_notify"),       # port without classifier: the port name is the function
        ("DESIGN-001", "alpha_params"),       # declared in alpha.h, never defined
        ("DESIGN-006", "a_helper"),           # alpha_helper matched through the a_ alias
        ("DESIGN-003", "beta_main"),          # required function interface never used
        ("DESIGN-003", "Rte_Read_Mode"),      # Signal_Get_IF is satisfied by the Temp read, so it is absent here
        ("DESIGN-005", "Missing_Sig"),        # required signal never accessed
        ("DESIGN-004", "Speed"),              # accessed signals the design does not require
        ("DESIGN-004", "Shared"),
    }
    assert not any(s == "alpha_main" for _, s in design), "exact match raises nothing"
    declared_only = next(f for f in findings if f["symbol"] == "alpha_params")
    assert "only declares 'alpha_params' in src/App/alpha.h" in declared_only["message"]
    record = _json(out / "design_compare.json")
    assert record["found"] and record["design"]["path"] == f"{ROOT}/alpha/BlackBox"
    assert {(r["design"], r["code"], r["how"]) for r in record["provided"]} == {
        ("alpha_helper", "a_helper", "alias"), ("alpha_main", "alpha_main", "exact"),
        ("alpha_notify", None, "missing"), ("alpha_params", "alpha_params", "declared_only"),
        ("alpha_reset", None, "missing")}
    assert next(r for r in record["required_functions"] if r["port"] == "Signal_Get_IF")["used"] == ["SP_Get_Temp"]
    assert record["required_signals"] == [{"signal": "Missing_Sig", "accessed": []},
                                          {"signal": "Orphan", "accessed": ["read"]},
                                          {"signal": "Temp", "accessed": ["read"]}]
    manifest = _json(out / "manifest.json")
    assert manifest["design_file"] == "design.qea" and manifest["design_found"] is True
    assert len(manifest["design_hash"]) == 64 and "design_compare.json" in manifest["outputs"]
    summary = _json(out / "summary.json")
    assert summary["design"] == {"file": "design.qea", "module_found": True}
    report = (out / "report.md").read_text(encoding="utf-8")
    assert "## Design comparison" in report and "| `alpha_helper` | `a_helper` | alias |" in report
    assert "signal `Shared`, signal `Speed`" in report


def test_module_missing_in_design_and_the_opt_outs(tmp_path):
    repo, profile = _profile_with_design(tmp_path, _qea(tmp_path))
    out = tmp_path / "beta"
    assert _analyze(out, "beta", repo=repo, profile=profile) == EXIT_OK
    findings = _json(out / "findings.json")["findings"]
    assert [(f["rule_id"], f["severity"]) for f in findings if f["rule_id"].startswith("DESIGN")] == [("DESIGN-000", "major")]
    assert _json(out / "design_compare.json") == {"schema": "archrev.design_compare/1", "module": "beta",
                                                  "design_file": "design.qea", "found": False}
    assert _analyze(tmp_path / "skip", "beta", "--no-design", repo=repo, profile=profile) == EXIT_OK
    assert not (tmp_path / "skip" / "design_compare.json").exists()
    assert _json(tmp_path / "skip" / "summary.json")["design"] == {"file": None, "module_found": None}


def test_design_errors_are_usage_errors(tmp_path):
    repo, profile = _profile_with_design(tmp_path, _qea(tmp_path))
    (repo / "design.qea").unlink()
    assert _analyze(tmp_path / "a", "alpha", repo=repo, profile=profile) == EXIT_USAGE
    assert "design model not found" in _json(tmp_path / "a" / "summary.json")["error"]
    (repo / "design.qea").write_text("not a database", encoding="utf-8")
    assert _analyze(tmp_path / "b", "alpha", repo=repo, profile=profile) == EXIT_USAGE
    assert "not a SQLite" in _json(tmp_path / "b" / "summary.json")["error"]
    (repo / "design.qea").unlink()
    sqlite3.connect(repo / "design.qea").executescript("CREATE TABLE t_object (a);")
    assert _analyze(tmp_path / "c", "alpha", repo=repo, profile=profile) == EXIT_USAGE
    assert "missing t_operation, t_package" in _json(tmp_path / "c" / "summary.json")["error"]
    (tmp_path / "two").mkdir()
    repo2, profile2 = _profile_with_design(tmp_path / "two", _qea(tmp_path / "two"), root=None)
    assert _analyze(tmp_path / "d", "alpha", repo=repo2, profile=profile2) == EXIT_USAGE
    assert "component_root" in _json(tmp_path / "d" / "summary.json")["error"]


def test_cli_design_overrides_and_is_byte_stable(tmp_path):
    qea = _qea(tmp_path)
    profile = tmp_path / "p.yaml"
    profile.write_text(PROFILE.read_text(encoding="utf-8") + f"design:\n  model: nowhere.qea\n  component_root: {ROOT}\n",
                       encoding="utf-8")
    for d in ("a", "b"):
        assert _analyze(tmp_path / d, "alpha", "--design", str(qea), profile=profile) == EXIT_OK
    for name in ("design_compare.json", "findings.json", "summary.json", "report.md"):
        assert (tmp_path / "a" / name).read_bytes() == (tmp_path / "b" / name).read_bytes(), name
    assert _json(tmp_path / "a" / "summary.json")["design"]["module_found"] is True
