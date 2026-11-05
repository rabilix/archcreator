"""From the as-built build and what the copy already holds to the ordered operations of one run (plan section 7).

Pure: no EA, no file writes. The operations are data for the fixed driver (section 9): `create`, `update`,
`add_feature`, `rename_feature`, `mark_stale`. Nothing is ever deleted or moved. An object is addressed by `guid` when
it exists, by `key` when this run creates it (the driver remembers what it created), the model's root by `model_root`.
"""

import json

from ..xmi import _EA_CONNECTOR_TYPE, TAG_MAX
from .identity import ID_TAG, MODULE_TAG, STALE_TAG, STEREOTYPE_TAG, Copy, Found

PLAN_SCHEMA = "archcreator.ea_plan/1"
TOP_KEY = "archcreator:root"
TOP_NAME = "archcreator (as-built)"
STALE_STEREOTYPE = "stale"
STALE_SUFFIX = " [stale]"


class PlanError(Exception):
    """The copy cannot be updated safely; the message says where and what to do."""


def _tags(key: str, module: str, extra: dict) -> dict[str, str]:
    tags = {name: str(value)[:TAG_MAX] for name, value in extra.items()}
    tags[ID_TAG] = key
    tags[MODULE_TAG] = module
    return dict(sorted(tags.items()))


def desired(b, module: str) -> list[dict]:
    """Every object the copy should hold, parents before children, ends before connectors."""
    top = {"key": TOP_KEY, "kind": "package", "name": TOP_NAME, "stereotype": "", "parent": None,
           "notes": "As-built views written by archcreator ea-apply. Generated; do not edit.",
           "tags": {ID_TAG: TOP_KEY, MODULE_TAG: ""}}
    out = [top]
    for p in b.packages.values():
        parent = TOP_KEY if p["parent"] is None else b.packages[p["parent"]]["key"]
        out.append({"key": p["key"], "kind": "package", "name": p["name"], "stereotype": "", "parent": parent,
                    "notes": p["doc"], "tags": _tags(p["key"], module, {})})
    for e in b.elements.values():
        item = {"key": e["key"], "kind": "element", "type": e["type"], "name": e["name"],
                "stereotype": e["stereotype"] or "", "parent": b.packages[e["pkg"]]["key"], "notes": e["doc"],
                "tags": _tags(e["key"], module, e["tags"])}
        if e["feature"]:
            item["feature"] = {"kind": e["feature"], "name": e["name"]}
        out.append(item)
    for c in b.connectors:
        out.append({"key": c["key"], "kind": "connector", "type": _EA_CONNECTOR_TYPE.get(c["type"], c["type"]),
                    "name": "", "stereotype": c["stereotype"] or "", "notes": c["doc"],
                    "client": b.elements[c["client"]]["key"], "supplier": b.elements[c["supplier"]]["key"],
                    "tags": _tags(c["key"], module, c["tags"])})
    return out


def _is_stale(f: Found) -> bool:
    return bool(f.tags.get(STALE_TAG))


def _wanted_tags(want: dict, have: Found) -> dict[str, str]:
    tags = dict(want["tags"])
    if STALE_TAG in have.tags or STEREOTYPE_TAG in have.tags:  # revival clears the marks (a tag is never deleted)
        tags[STALE_TAG] = ""
        tags[STEREOTYPE_TAG] = ""
    return tags


def build_plan(b, copy: Copy, module: str, commit: str, model: str) -> dict:
    """The operations that make the copy hold `b` for `module`; raises PlanError when that is not safe."""
    want = desired(b, module)
    keys = {w["key"] for w in want}
    root_key = next(p["key"] for p in b.packages.values() if p["parent"] is None)
    root = copy.objects.get(root_key)
    if root is not None and root.kind != "package":
        raise PlanError(f"{ID_TAG} {root_key!r} belongs to a {root.kind} in the copy, not to a package")
    boundary = root.package_id if root else None

    def ref(key: str | None) -> dict:
        if key is None:
            return {"model_root": True}
        have = copy.objects.get(key)
        return {"guid": have.guid} if have else {"key": key}

    ops: list[dict] = []
    moved: list[str] = []
    unchanged = 0
    for w in want:
        have = copy.objects.get(w["key"])
        if have is None:
            op = {"op": "create", **{k: v for k, v in w.items() if k not in ("parent", "client", "supplier")}}
            if w["kind"] == "connector":
                op["client"], op["supplier"] = ref(w["client"]), ref(w["supplier"])
            else:
                op["parent"] = ref(w["parent"])
            ops.append(op)
            continue
        if have.kind != w["kind"]:
            raise PlanError(f"{ID_TAG} {w['key']!r} belongs to a {have.kind} in the copy, expected a {w['kind']}")
        if w["kind"] != "connector" and w["key"] not in (TOP_KEY, root_key):
            planned = copy.objects.get(w["parent"])
            if boundary is None or boundary not in copy.ancestors(have.parent):
                raise PlanError(f"{w['name']!r} ({w['key']}) was moved out of the as-built package of this module; "
                                f"move it back in EA (or remove it) and run again: nothing is written outside it")
            if planned is not None and have.parent != (planned.package_id if planned.kind == "package" else None):
                moved.append(w["key"])
        tags = _wanted_tags(w, have)
        fields = {"name": w["name"], "stereotype": w["stereotype"], "notes": w["notes"]}
        same = (all(getattr(have, k) == v for k, v in fields.items())
                and all(have.tags.get(k, "") == v for k, v in tags.items()))
        if same:
            unchanged += 1
        else:
            ops.append({"op": "update", "kind": w["kind"], "key": w["key"], "guid": have.guid, **fields,
                        "tags": tags})
        feature = w.get("feature")
        if feature and feature["name"] not in have.features:
            if len(have.features) == 1:  # the one feature ea-apply made, renamed in the code
                ops.append({"op": "rename_feature", "key": w["key"], "guid": have.guid, "feature": feature,
                            "old": have.features[0]})
            else:
                ops.append({"op": "add_feature", "key": w["key"], "guid": have.guid, "feature": feature})
    stale = []
    for key, have in sorted(copy.objects.items()):
        if key in keys or have.tags.get(MODULE_TAG) != module or _is_stale(have):
            continue
        stale.append(key)
        ops.append({"op": "mark_stale", "kind": have.kind, "key": key, "guid": have.guid,
                    "name": have.name + STALE_SUFFIX if not have.name.endswith(STALE_SUFFIX) else have.name,
                    "stereotype": STALE_STEREOTYPE,
                    "tags": {STALE_TAG: commit, STEREOTYPE_TAG: have.stereotype}})
    for i, op in enumerate(ops, 1):
        op["n"] = i
    counts: dict[str, int] = {}
    for op in ops:
        counts[op["op"]] = counts.get(op["op"], 0) + 1
    return {"schema": PLAN_SCHEMA, "module": module, "model": model, "commit": commit, "unchanged": unchanged,
            "counts": dict(sorted(counts.items())), "moved": moved, "stale": stale, "operations": ops}


def dumps(plan: dict) -> str:
    """Canonical JSON: the same model and copy state give the same bytes."""
    return json.dumps(plan, indent=1, sort_keys=True, ensure_ascii=False) + "\n"
