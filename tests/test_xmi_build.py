"""build(): every object carries the key its id is made from, and trace=False drops exactly the code pointers."""

import json
from pathlib import Path

from archcreator.cli import main
from archcreator.xmi import _xid, build

FIXTURES = Path(__file__).parent / "fixtures"


def _inputs(tmp_path: Path, fixture: str = "fx02", module: str = "alpha") -> tuple[dict, dict]:
    repo = FIXTURES / fixture
    assert main(["analyze", "--repo", str(repo), "--module", module, "--profile", str(repo / "profile.yaml"),
                 "--out", str(tmp_path), "--context", "all"]) in (0, 1)
    load = lambda name: json.loads((tmp_path / name).read_text(encoding="utf-8"))  # noqa: E731
    return load("model.json"), load("manifest.json")


def _objects(b):
    return [*b.packages.values(), *b.elements.values(), *b.connectors]


def test_every_id_is_the_hash_of_its_key_and_keys_are_unique(tmp_path):
    b = build(*_inputs(tmp_path))
    for p in b.packages.values():
        assert p["id"] == _xid("EAPK", p["key"])
    for o in [*b.elements.values(), *b.connectors]:
        assert o["id"] == _xid("EAID", o["key"])
    keys = [o["key"] for o in _objects(b)]
    assert len(keys) == len(set(keys))


def test_keys_are_stable_across_runs(tmp_path):
    first = [o["key"] for o in _objects(build(*_inputs(tmp_path / "a")))]
    assert first == [o["key"] for o in _objects(build(*_inputs(tmp_path / "b")))]


def test_trace_off_drops_only_the_code_pointers(tmp_path):
    model, manifest = _inputs(tmp_path)
    full, bare = build(model, manifest), build(model, manifest, trace=False)
    assert [o["key"] for o in _objects(full)] == [o["key"] for o in _objects(bare)], "same objects either way"
    assert any(e["doc"] for e in full.elements.values()) and any(c["doc"] for c in full.connectors)
    # The signal interfaces' fixed description points nowhere into the code, so it stays.
    assert not any(e["doc"] for e in bare.elements.values() if e["stereotype"] != "Signal"), \
        "files, definitions and declarations are trace"
    assert not any(c["doc"] for c in bare.connectors), "evidence lines and per-symbol counts are trace"
    for c_full, c_bare in zip(full.connectors, bare.connectors):
        dropped = set(c_full["tags"]) - set(c_bare["tags"])
        assert dropped <= {"first_evidence", "via_macros"}
        assert {k: v for k, v in c_full["tags"].items() if k not in dropped} == c_bare["tags"]
    for e_full, e_bare in zip(full.elements.values(), bare.elements.values()):
        assert e_full["tags"] == e_bare["tags"], "usr, module_id, signal and run tags are structure"
