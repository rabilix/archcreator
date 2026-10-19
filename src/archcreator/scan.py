"""Repository-level view of a scan: the module graph and the rules that need it.

    ARCH-001   dependency cycles between App modules (call, function address, global access edges)
    LAYER-001  an edge from one layer to another that the profile's `layers.allow` does not permit
    EXPORT-001 an exported symbol of an in-build module that no parsed module uses

Scan outputs next to the per-module folders: graph.json, scan_findings.json (every module's findings merged by
fingerprint plus the scan rules), scan_summary.json and scan_report.md.
"""

import fnmatch
from collections import Counter, defaultdict
from pathlib import Path

from .baseline import Baseline, apply_baseline
from .canonical import write_json, write_text
from .rules import INTERFACE_KINDS, _finding, check_build, sort_findings

GRAPH_SCHEMA = "archcreator.graph/1"
SCAN_SUMMARY_SCHEMA = "archcreator.scan_summary/1"
SCAN_TOP_FINDINGS = 30


def build_graph(models: dict[str, dict], layers) -> dict:
    """Nodes: every module any view mentions; edges: outgoing edges aggregated per (from, to), with the kinds."""
    nodes: dict[str, dict] = {}
    edges: dict[tuple[str, str], dict] = {}
    for model in models.values():
        for m in model["modules"]:
            nodes.setdefault(m["id"], {"id": m["id"], "name": m["name"], "kind": m["kind"], "files": m["files"]})
        for e in model["dependencies"]["outgoing"]:
            edge = edges.setdefault((e["from"], e["to"]), {"from": e["from"], "to": e["to"], "kinds": {},
                                                           "evidence_count": 0, "first_by_kind": {}})
            edge["kinds"][e["kind"]] = edge["kinds"].get(e["kind"], 0) + len(e["evidence"])
            edge["evidence_count"] += len(e["evidence"])
            ev = e["evidence"][0]
            cand = {"file": ev["file"], "line": ev["line"], "kind": e["kind"], "symbol": e["symbol"]}
            cur = edge["first_by_kind"].get(e["kind"])
            if cur is None or (cand["file"], cand["line"], cand["symbol"]) < (cur["file"], cur["line"], cur["symbol"]):
                edge["first_by_kind"][e["kind"]] = cand
    for node in nodes.values():
        node["layer"] = _layer_of(node, layers)
    out_edges = []
    for (_src, _dst), edge in sorted(edges.items()):
        edge["kinds"] = dict(sorted(edge["kinds"].items()))
        edge["first_by_kind"] = dict(sorted(edge["first_by_kind"].items()))
        edge["first_evidence"] = first_evidence(edge, set(edge["kinds"]))
        out_edges.append(edge)
    return {"schema": GRAPH_SCHEMA, "nodes": sorted(nodes.values(), key=lambda n: n["id"]), "edges": out_edges}


def first_evidence(edge: dict, kinds: set[str]) -> dict:
    """The earliest evidence among the given kinds of an edge: a rule cites the kind that made it fire."""
    candidates = [v for k, v in edge["first_by_kind"].items() if k in kinds]
    return min(candidates, key=lambda c: (c["file"], c["line"], c["kind"], c["symbol"]))


def _layer_of(node: dict, layers) -> str | None:
    if layers is None:
        return None
    for layer, globs in layers.assign.items():
        for f in node["files"]:
            if any(fnmatch.fnmatchcase(f, g) for g in globs):
                return layer
    return None


def _cycles(graph: dict) -> list[tuple[list[str], list[str]]]:
    """Strongly connected components with more than one App module, over the coupling edges.

    Each as (members, one concrete cycle path through the smallest member). Identity and size come from the
    members: a module joining the component changes the finding even when the illustrated path does not.
    """
    kinds = {n["id"]: n["kind"] for n in graph["nodes"]}
    adj: dict[str, list[str]] = defaultdict(list)
    for e in graph["edges"]:
        if kinds.get(e["from"]) == "c_module" and kinds.get(e["to"]) == "c_module" \
                and set(e["kinds"]) & INTERFACE_KINDS:
            adj[e["from"]].append(e["to"])
    for k in adj:
        adj[k] = sorted(set(adj[k]))

    index: dict[str, int] = {}
    low: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    components: list[list[str]] = []

    def strongconnect(root: str) -> None:
        # Iterative: a dependency chain of a thousand modules must not hit the recursion limit.
        index[root] = low[root] = len(index)
        stack.append(root)
        on_stack.add(root)
        work = [(root, iter(adj.get(root, ())))]
        while work:
            v, neighbours = work[-1]
            for w in neighbours:
                if w not in index:
                    index[w] = low[w] = len(index)
                    stack.append(w)
                    on_stack.add(w)
                    work.append((w, iter(adj.get(w, ()))))
                    break
                if w in on_stack:
                    low[v] = min(low[v], index[w])
            else:
                work.pop()
                if work:
                    parent = work[-1][0]
                    low[parent] = min(low[parent], low[v])
                if low[v] == index[v]:
                    comp = []
                    while True:
                        w = stack.pop()
                        on_stack.discard(w)
                        comp.append(w)
                        if w == v:
                            break
                    if len(comp) > 1:
                        components.append(sorted(comp))

    for v in sorted(set(adj) | {w for ws in adj.values() for w in ws}):
        if v not in index:
            strongconnect(v)

    cycles = []
    for comp in sorted(components):
        members = set(comp)
        start = comp[0]
        # One concrete cycle through the smallest member, by depth-first search inside the component.
        path = [start]
        seen = {start}

        # Iterative depth-first search, neighbours in sorted order: the first path back to the start wins.
        work = [iter(adj.get(start, ()))]
        while work:
            for w in work[-1]:
                if w not in members:
                    continue
                if w == start:
                    work.clear()
                    break
                if w not in seen:
                    seen.add(w)
                    path.append(w)
                    work.append(iter(adj.get(w, ())))
                    break
            else:
                work.pop()
                if work:
                    path.pop()
        cycles.append((comp, path + [start]))
    return cycles


def check_cycles(graph: dict, names: dict[str, str], profile) -> list[dict]:
    findings = []
    edges = {(e["from"], e["to"]): e for e in graph["edges"]}
    for members, cycle in _cycles(graph):
        first = first_evidence(edges[(cycle[0], cycle[1])], INTERFACE_KINDS)
        extra = sorted(set(members) - set(cycle))
        message = "dependency cycle: " + " -> ".join(names[m] for m in cycle)
        if extra:
            message += f"; also in the same cycle group: {', '.join(names[m] for m in extra)}"
        findings.append(_finding(
            "ARCH-001", profile.severity["ARCH-001"], names[cycle[0]], first["file"], first["line"], None, message,
            measured=len(members), threshold=0, evidence=[{**first, "function": None}],
            element="cycle:" + ",".join(members), discriminator=",".join(members)))
    return findings


def check_layers(graph: dict, names: dict[str, str], profile) -> list[dict]:
    layers = profile.layers
    if layers is None:
        return []
    layer_of = {n["id"]: n["layer"] for n in graph["nodes"]}
    findings = []
    for e in graph["edges"]:
        src, dst = layer_of.get(e["from"]), layer_of.get(e["to"])
        if src is None or dst is None or src == dst or dst in layers.allow.get(src, ()):
            continue
        kinds = sorted(set(e["kinds"]) & set(layers.edge_kinds))
        if not kinds:
            continue
        first = first_evidence(e, set(kinds))
        findings.append(_finding(
            "LAYER-001", profile.severity["LAYER-001"], names[e["from"]], first["file"], first["line"],
            names[e["to"]],
            f"{names[e['from']]} (layer {src}) depends on {names[e['to']]} (layer {dst}) through "
            f"{', '.join(kinds)}; {src} may use: {', '.join(layers.allow.get(src, ())) or 'nothing'}",
            evidence=[{**first, "function": None}], element=f"layer:{e['from']}->{e['to']}",
            discriminator=e["to"]))
    return findings


def check_exports(models: dict[str, dict], profile) -> list[dict]:
    """Exported symbols no analyzed module uses; runnables and callbacks go into modules.export_exempt."""
    findings = []
    for name in sorted(models):
        model = models[name]
        used = {e["symbol"] for e in model["dependencies"]["incoming"] if e["kind"] in INTERFACE_KINDS}
        for kind, entries in (("function", model["provided_interface"]["functions"]),
                              ("global", model["provided_interface"]["globals"])):
            for entry in entries:
                if entry["name"] in used or not entry["defined_at"]:
                    continue
                if any(fnmatch.fnmatchcase(entry["name"], g) for g in profile.export_exempt):
                    continue
                file, _, line = entry["defined_at"].rpartition(":")
                findings.append(_finding(
                    "EXPORT-001", profile.severity["EXPORT-001"], name, file, int(line) if line.isdigit() else None,
                    entry["name"], f"exported {kind} '{entry['name']}' is used by no analyzed module "
                                   "(called from a module outside this run, from outside the scope, or dead)",
                    element=f"export:{model['target']['id']}:{entry['name']}"))
    return findings


def finish_scan(repo: Path, profile, prep, results, all_models: dict[str, dict], out_dir: Path, fail_on: str | None,
                baseline: Baseline | None, design, design_path: Path | None, log: dict) -> int:
    """`all_models`: every in-build module (the graph and its rules); `results`: the modules written (EXPORT-001)."""
    from .pipeline import EXIT_OK, finding_counts, gate_code, top_findings

    written = {r.module: r.model for r in results}
    names = {m["id"]: m["name"] for model in all_models.values() for m in model["modules"]}
    graph = build_graph(all_models, profile.layers)
    scan_rules = (check_cycles(graph, names, profile) + check_layers(graph, names, profile)
                  + check_exports(written, profile)
                  # Repository facts even when nothing is in the build and no module view was written.
                  + check_build(prep.not_in_build, "", profile))
    merged: dict[str, dict] = {}
    for r in results:
        for f in r.findings:
            merged.setdefault(f["fingerprint"], f)
    for f in scan_rules:
        merged.setdefault(f["fingerprint"], f)
    findings = list(merged.values())
    stale = apply_baseline(findings, baseline, modules=(set(all_models) | {None}) if prep.complete else set())
    findings = sort_findings(findings)

    code = max([r.code for r in results] + [gate_code(findings, fail_on)], default=EXIT_OK)
    error = next((r.error for r in sorted(results, key=lambda r: -r.code) if r.error), None)

    out_dir.mkdir(parents=True, exist_ok=True)
    outputs = {
        "graph.json": write_json(out_dir / "graph.json", graph),
        "scan_findings.json": write_json(out_dir / "scan_findings.json",
                                         {"schema": "archcreator.findings/1", "findings": findings}),
    }
    first = results[0].manifest if results else None
    summary = {
        "schema": SCAN_SUMMARY_SCHEMA,
        "exit_code": code,
        "error": error,
        "target_commit": prep.commit,
        "target_dirty": prep.dirty,
        "scope": prep.scope_label,
        "analyzed_modules": sorted(r.module for r in results),
        "profile": {"name": profile.name, "origin": profile.origin, "file": profile.file_name},
        "baseline": {"file": baseline.file_name if baseline else None, "stale_entries": len(stale),
                     "stale_checked": prep.complete},
        "design": {"file": first["design_file"] if first else None},
        "parse_coverage": prep.coverage["ratio"],
        "modules": [{"name": r.module, "exit_code": r.code, "finding_counts": finding_counts(r.findings),
                     "accounting_ok": r.model["accounting"]["ok"]} for r in results],
        "not_in_build": [Path(s).stem for s in prep.not_in_build],
        "graph": {"nodes": len(graph["nodes"]), "edges": len(graph["edges"]),
                  "cycles": sum(1 for f in scan_rules if f["rule_id"] == "ARCH-001")},
        "finding_counts": finding_counts(findings),
        "top_findings": top_findings(findings, SCAN_TOP_FINDINGS),
        "top_findings_truncated": len(findings) > SCAN_TOP_FINDINGS,
        "outputs": {"graph": "graph.json", "findings": "scan_findings.json", "report": "scan_report.md",
                    "modules": {r.module: f"{r.module}/summary.json" for r in results}},
    }
    outputs["scan_summary.json"] = write_json(out_dir / "scan_summary.json", summary)
    write_text(out_dir / "scan_report.md", render_scan_report(summary, graph, findings, stale, results))
    log["scan_outputs"] = outputs
    return code


def render_scan_report(summary: dict, graph: dict, findings: list[dict], stale: list[dict], results) -> str:
    lines: list[str] = []
    w = lines.append
    w(f"# archcreator scan report: {', '.join(summary['scope'])}")
    w("")
    w("| Item | Value |")
    w("|---|---|")
    w(f"| Target commit | `{summary['target_commit']}`{' (working tree dirty)' if summary['target_dirty'] else ''} |")
    w(f"| Profile | `{summary['profile']['name']}` ({summary['profile']['origin']}) |")
    w(f"| Modules analyzed | {len(results)} ({', '.join(summary['analyzed_modules'])}); not in build: {', '.join(summary['not_in_build']) or 'none'} |")
    w("| Note | Callers, producers, cycles and unused exports are known among the analyzed modules only |")
    w(f"| Parse coverage | {summary['parse_coverage']:.0%} |")
    w(f"| Graph | {summary['graph']['nodes']} nodes, {summary['graph']['edges']} edges, {summary['graph']['cycles']} cycles |")
    counts = summary["finding_counts"]
    w(f"| Findings | {counts['total']} ({counts['by_status']['new']} new, {counts['by_status']['accepted']} accepted, "
      f"{counts['by_status']['regressed']} regressed); {len(stale)} stale baseline entries |")
    w(f"| Exit code | {summary['exit_code']}{(': ' + summary['error']) if summary['error'] else ''} |")
    w("")

    w("## Modules")
    w("")
    w("| Module | Exit | Findings | New | Accounting |")
    w("|---|---|---|---|---|")
    for r in results:
        c = finding_counts_of(r.findings)
        w(f"| `{r.module}` | {r.code} | {c['total']} | {c['by_status']['new']} | {'ok' if r.model['accounting']['ok'] else 'FAIL'} |")
    w("")

    scan_level = [f for f in findings if f["rule_id"] in ("ARCH-001", "LAYER-001", "EXPORT-001", "BUILD-001")]
    w("## Repository-level findings")
    w("")
    if not scan_level:
        w("None.")
    else:
        w("| Status | Severity | Rule | Module | Location | Symbol | Message | Fingerprint |")
        w("|---|---|---|---|---|---|---|---|")
        for f in scan_level:
            loc = f"`{f['file']}:{f['line']}`" if f["line"] else (f"`{f['file']}`" if f["file"] else "")
            w(f"| {f['status']} | {f['severity']} | {f['rule_id']} | `{f['module']}` | {loc} | {_esc(f['symbol'] or '')} "
              f"| {_esc(f['message'])} | `{f['fingerprint']}` |")
    w("")

    layered = [n for n in graph["nodes"] if n["layer"]]
    if layered:
        w("## Layers")
        w("")
        by_layer = defaultdict(list)
        for n in layered:
            by_layer[n["layer"]].append(n["name"])
        for layer, members in sorted(by_layer.items()):
            w(f"- **{layer}**: {', '.join('`' + m + '`' for m in sorted(members))}")
        w("")

    w("## Module graph (App modules)")
    w("")
    kinds = {n["id"]: n["kind"] for n in graph["nodes"]}
    names = {n["id"]: n["name"] for n in graph["nodes"]}
    app_edges = [e for e in graph["edges"] if kinds[e["from"]] == "c_module" and kinds[e["to"]] in ("c_module", "header_only")]
    if not app_edges:
        w("No edges between App modules.")
    else:
        w("| From | To | Kinds |")
        w("|---|---|---|")
        for e in app_edges:
            w(f"| `{names[e['from']]}` | `{names[e['to']]}` | {', '.join(f'{k} {n}' for k, n in e['kinds'].items())} |")
    w("")
    return "\n".join(lines)


def finding_counts_of(findings: list[dict]) -> dict:
    from .pipeline import finding_counts
    return finding_counts(findings)


def _esc(text) -> str:
    return str(text).replace("|", "\\|")


__all__ = ["GRAPH_SCHEMA", "SCAN_SUMMARY_SCHEMA", "build_graph", "check_cycles", "check_exports", "check_layers",
           "finish_scan", "render_scan_report", "Counter"]
