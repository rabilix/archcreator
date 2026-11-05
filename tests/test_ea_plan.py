"""identity + plan (plan section 7), round-tripped through a fake EA: create, nothing to do, update, moved, stale,
revival, duplicates, order and byte stability."""

import json
import sqlite3
from pathlib import Path

import pytest

from archcreator.cli import main
from archcreator.ea import identity
from archcreator.ea.plan import STALE_SUFFIX, TOP_KEY, PlanError, build_plan, dumps
from archcreator.xmi import build

import fake_ea
from ea_model import make_model

FIXTURES = Path(__file__).parent / "fixtures"


def _inputs(tmp_path: Path, module="alpha", fixture="fx02"):
    repo = FIXTURES / fixture
    out = tmp_path / f"out_{module}"
    assert main(["analyze", "--repo", str(repo), "--module", module, "--profile", str(repo / "profile.yaml"),
                 "--out", str(out), "--context", "all"]) in (0, 1)
    load = lambda name: json.loads((out / name).read_text(encoding="utf-8"))  # noqa: E731
    return load("model.json"), load("manifest.json")


def _plan(model_path, model, manifest, trace=True):
    b = build(model, manifest, trace=trace)
    return build_plan(b, identity.read(model_path), model["target"]["id"], "c0ffee", str(model_path))


def _run(model_path, model, manifest, trace=True, fail_at=None):
    plan = _plan(model_path, model, manifest, trace)
    fake_ea.apply(model_path, plan["operations"], fail_at)
    return plan


@pytest.fixture
def copy(tmp_path):
    return make_model(tmp_path / "copy.qea")


def test_an_empty_copy_gets_only_creates_in_order(tmp_path, copy):
    model, manifest = _inputs(tmp_path)
    plan = _plan(copy, model, manifest)
    ops = plan["operations"]
    assert {op["op"] for op in ops} == {"create"}
    assert ops[0]["key"] == TOP_KEY and ops[0]["parent"] == {"model_root": True}
    seen = set()
    for op in ops:
        for end in [op.get("parent"), op.get("client"), op.get("supplier")]:
            if end and "key" in end:
                assert end["key"] in seen, f"op {op['n']} refers to {end['key']} before it is created"
        seen.add(op["key"])
    b = build(model, manifest)
    assert len(ops) == 1 + len(b.packages) + len(b.elements) + len(b.connectors)


def test_after_an_apply_nothing_is_left_to_do(tmp_path, copy):
    model, manifest = _inputs(tmp_path)
    _run(copy, model, manifest)
    again = _plan(copy, model, manifest)
    assert again["operations"] == [] and again["stale"] == [] and again["moved"] == []


def test_the_plan_is_byte_stable(tmp_path, copy):
    model, manifest = _inputs(tmp_path)
    assert dumps(_plan(copy, model, manifest)) == dumps(_plan(copy, model, manifest))


def test_a_changed_note_is_one_update_by_guid(tmp_path, copy):
    model, manifest = _inputs(tmp_path)
    _run(copy, model, manifest)
    con = sqlite3.connect(copy)
    con.execute("UPDATE t_object SET Note = 'edited by hand' WHERE Object_Type = 'Component' AND Stereotype = 'as-built'")
    con.commit()
    con.close()
    ops = _plan(copy, model, manifest)["operations"]
    assert [(op["op"], op["kind"]) for op in ops] == [("update", "element")] and ops[0]["guid"].startswith("{")


def test_trace_off_then_on_updates_only_the_traced_fields(tmp_path, copy):
    model, manifest = _inputs(tmp_path)
    _run(copy, model, manifest, trace=False)
    ops = _plan(copy, model, manifest, trace=True)["operations"]
    assert ops and {op["op"] for op in ops} == {"update"}


def test_an_interrupted_run_is_completed_by_the_next(tmp_path, copy):
    model, manifest = _inputs(tmp_path)
    first = _run(copy, model, manifest, fail_at=10)
    rest = _plan(copy, model, manifest)["operations"]
    assert {op["op"] for op in rest} == {"create"} and len(rest) == len(first["operations"]) - 9
    fake_ea.apply(copy, rest)
    assert _plan(copy, model, manifest)["operations"] == []


def test_a_dependency_dropped_from_the_code_becomes_stale_and_comes_back(tmp_path, copy):
    model, manifest = _inputs(tmp_path)
    _run(copy, model, manifest)
    edge = model["dependencies"]["outgoing"][-1]
    fewer = {**model, "dependencies": {**model["dependencies"],
                                       "outgoing": model["dependencies"]["outgoing"][:-1]}}
    plan = _run(copy, fewer, manifest)
    assert plan["stale"], "the dropped edge's usage is stale"
    found = identity.read(copy)
    for key in plan["stale"]:
        assert found.objects[key].tags["archcreator_stale"] == "c0ffee"
        assert found.objects[key].name.endswith(STALE_SUFFIX) and found.objects[key].stereotype == "stale"
    assert _plan(copy, fewer, manifest)["operations"] == [], "stale stays stale, no new marks"
    revived = _run(copy, model, manifest)
    assert {op["op"] for op in revived["operations"]} <= {"update"} and revived["operations"]
    back = identity.read(copy)
    for key in plan["stale"]:
        assert not back.objects[key].tags["archcreator_stale"] and not back.objects[key].name.endswith(STALE_SUFFIX)
    assert _plan(copy, model, manifest)["operations"] == []
    assert edge  # the edge that went away and came back


def test_an_object_moved_inside_the_root_is_updated_where_it_is(tmp_path, copy):
    model, manifest = _inputs(tmp_path)
    _run(copy, model, manifest)
    con = sqlite3.connect(copy)
    blackbox = con.execute("SELECT Package_ID FROM t_package WHERE Name = 'BlackBox'").fetchone()[0]
    oid = con.execute("SELECT Object_ID FROM t_object WHERE Object_Type = 'Interface' LIMIT 1").fetchone()[0]
    con.execute("UPDATE t_object SET Package_ID = ? WHERE Object_ID = ?", (blackbox, oid))
    con.commit()
    con.close()
    plan = _plan(copy, model, manifest)
    assert plan["operations"] == [] and len(plan["moved"]) == 1


def test_an_object_moved_outside_the_root_stops_the_run(tmp_path, copy):
    model, manifest = _inputs(tmp_path)
    _run(copy, model, manifest)
    con = sqlite3.connect(copy)
    con.execute("UPDATE t_object SET Package_ID = 1 WHERE Object_Type = 'Interface'")
    con.commit()
    con.close()
    with pytest.raises(PlanError, match="moved out"):
        _plan(copy, model, manifest)


def test_a_duplicate_id_stops_the_run(tmp_path, copy):
    model, manifest = _inputs(tmp_path)
    _run(copy, model, manifest)
    con = sqlite3.connect(copy)
    oid, = con.execute("SELECT Object_ID FROM t_object WHERE Object_Type = 'Interface' LIMIT 1").fetchone()
    key = con.execute("SELECT Value FROM t_objectproperties WHERE Object_ID = ? AND Property = 'archcreator_id'",
                      (oid,)).fetchone()[0]
    clone = con.execute("INSERT INTO t_object (Object_Type, Name, Package_ID, ea_guid) VALUES ('Interface', 'x', 1, "
                        "'{X}')").lastrowid
    con.execute("INSERT INTO t_objectproperties (Object_ID, Property, Value) VALUES (?, 'archcreator_id', ?)",
                (clone, key))
    con.commit()
    con.close()
    with pytest.raises(identity.IdentityError, match="two objects"):
        identity.read(copy)


def test_a_persons_own_elements_are_never_touched(tmp_path, copy):
    model, manifest = _inputs(tmp_path)
    _run(copy, model, manifest)
    con = sqlite3.connect(copy)
    root = con.execute("SELECT Package_ID FROM t_package WHERE Name = 'BlackBox'").fetchone()[0]
    con.execute("INSERT INTO t_object (Object_Type, Name, Package_ID, ea_guid) VALUES ('Note', 'my note', ?, '{N}')",
                (root,))
    con.commit()
    con.close()
    assert _plan(copy, model, manifest)["operations"] == []


def test_two_modules_side_by_side_do_not_mark_each_other_stale(tmp_path, copy):
    alpha, beta = _inputs(tmp_path, "alpha"), _inputs(tmp_path, "beta")
    _run(copy, *alpha)
    second = _run(copy, *beta)
    assert second["stale"] == []
    assert _plan(copy, *alpha)["operations"] == [] and _plan(copy, *beta)["operations"] == []
