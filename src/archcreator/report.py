"""Markdown review report. Deterministic: no timestamps, no absolute paths."""

from collections import Counter, defaultdict

BLIND_SPOTS = [
    "Type-only dependencies (typedef/struct use without a symbol reference) are not edges; they are visible only through includes.",
    "Code disabled by the active preprocessor configuration (profile defines) is not analyzed.",
    "Function-pointer calls are listed as unresolved; their possible targets are not inferred.",
    "Only the modules named for the run are parsed: callers, signal producers and consumers outside that set are unknown; modules outside the scope are EXTERNAL leaves.",
    "Global write detection is syntactic: an index expression inside the left side of an assignment counts as written.",
    "Complexity is measured by lizard on source as written (macros unexpanded); dependencies by libclang on preprocessed code.",
    "lizard counts each #if/#ifdef inside a function body as a decision point, so CCN includes build-variant branches.",
    "Signal accesses hidden in wrapper macros are found from the wrapper's body text; an accessor name assembled with ## token pasting is not seen.",
    "The design comparison covers provided and required interfaces, not state machines, types or sequences.",
]


def _loc(e: dict) -> str:
    fn = f" ({e['function']})" if e.get("function") else ""
    return f"`{e['file']}:{e['line']}`{fn}"


def _esc(text) -> str:
    return str(text).replace("|", "\\|")


def render(manifest: dict, ir: dict, model: dict, metrics: list[dict], coupling: dict, findings: list[dict],
           design: dict | None = None) -> str:
    t = model["target"]
    names = {m["id"]: m for m in model["modules"]}
    lines: list[str] = []
    w = lines.append

    w(f"# archcreator module report: {t['name']}")
    w("")
    w("| Item | Value |")
    w("|---|---|")
    w(f"| Module | `{t['name']}` ({t['kind']}) |")
    w(f"| Files | {', '.join(f'`{f}`' for f in t['files'])} |")
    w(f"| Scope | {', '.join('`' + s + '`' for s in model['scope'])} |")
    w(f"| Analyzed modules | {len(model['analyzed_modules'])}: {', '.join('`' + m + '`' for m in model['analyzed_modules'])} "
      "(callers, producers and consumers are known among these only) |")
    w(f"| Target commit | `{manifest['target_commit']}`{' (working tree dirty)' if manifest['target_dirty'] else ''} |")
    w(f"| Profile | `{manifest['profile_name']}` (sha256 `{manifest['profile_hash'][:12]}`) |")
    if manifest["baseline_file"]:
        accepted = sum(1 for f in findings if f["status"] == "accepted")
        regressed = sum(1 for f in findings if f["status"] == "regressed")
        stale = (f"{len(manifest['baseline_stale'])} stale entries" if manifest["baseline_stale_checked"]
                 else "stale entries not checked (partial context)")
        w(f"| Baseline | `{manifest['baseline_file']}` (sha256 `{manifest['baseline_hash'][:12]}`): "
          f"{accepted} accepted, {regressed} regressed, {stale} |")
    else:
        w("| Baseline | none: every finding counts as new |")
    if manifest["design_file"]:
        w(f"| Design model | `{manifest['design_file']}` (sha256 `{manifest['design_hash'][:12]}`): "
          f"{'module found' if manifest['design_found'] else 'module not found'} |")
    w(f"| Tool / libclang / lizard | {manifest['tool_version']} / {manifest['analyzer_versions']['libclang']} / {manifest['analyzer_versions']['lizard']} |")
    pc = manifest["parse_coverage"]
    w(f"| Parse coverage | {pc['clean_units']}/{pc['units']} units without errors in App files ({pc['ratio']:.0%}); {pc['units_with_external_errors']} with errors in external headers |")
    acc = model["accounting"]
    extra = f" - {acc['signal_wrapper_extra']} extra signal accesses of wrapper macros" if acc["signal_wrapper_extra"] else ""
    w(f"| Accounting check | {'PASS' if acc['ok'] else 'FAIL'}: {acc['target_references']} references = {acc['internal']} internal + {acc['outgoing_evidence']} outgoing + {acc['unresolved_calls']} unresolved{extra} |")
    w(f"| Coupling | fan-out {coupling['fan_out_modules']} modules, fan-in {coupling['fan_in_modules']} App modules, instability {coupling['instability']} |")
    w("")

    w("## Findings")
    w("")
    if not findings:
        w("No findings.")
    else:
        by_rule = Counter(f["rule_id"] for f in findings)
        w(", ".join(f"{r}: {n}" for r, n in sorted(by_rule.items())))
        w("")
        w("| Status | Severity | Rule | Location | Symbol | Message | Fingerprint |")
        w("|---|---|---|---|---|---|---|")
        for f in findings:
            loc = f"`{f['file']}:{f['line']}`" if f["line"] else (f"`{f['file']}`" if f["file"] else "")
            status = f["status"]
            if f.get("baseline"):
                status += f" ({_esc(f['baseline']['approved_by'])}, {f['baseline']['date']}: {_esc(f['baseline']['reason'])})"
            w(f"| {status} | {f['severity']} | {f['rule_id']} | {loc} | {_esc(f['symbol'] or '')} | {_esc(f['message'])} | `{f['fingerprint']}` |")
    if manifest["baseline_stale"]:
        w("")
        w(f"{len(manifest['baseline_stale'])} baseline entries match no finding any more (remove them):")
        w("")
        for e in manifest["baseline_stale"]:
            w(f"- `{e['fingerprint']}` {e['rule']} {_esc(e['symbol'] or '')} {('`' + e['file'] + '`') if e['file'] else ''}")
    w("")

    w("## Parse results")
    w("")
    app_bad = [u for u in ir["units"] if u["errors_app"] or not u["parsed"]]
    ext_bad = [u for u in ir["units"] if u["errors_external"] and u not in app_bad]
    if not app_bad and not ext_bad:
        w(f"All {len(ir['units'])} translation units parsed without errors.")
    if app_bad:
        w(f"**{len(app_bad)} unit(s) have errors inside App files.** Symbols there may be missing from the model; review these first.")
        w("")
        for u in app_bad:
            w(f"- `{u['file']}`: {u['errors_app']} App error(s), {u['errors_external']} external")
            for msg in u["error_messages"][:5]:
                w(f"  - {_esc(msg)}")
        w("")
    if ext_bad:
        w(f"{len(ext_bad)} unit(s) have errors only in external headers (not gating). Declarations after the error point in those headers may be missing.")
        w("")
        for u in ext_bad:
            w(f"- `{u['file']}`: {u['errors_external']} external error(s), e.g. {_esc(u['error_messages'][0])}")
    w("")

    pi = model["provided_interface"]
    w("## Provided interface")
    w("")
    w(f"{len(pi['functions'])} exported functions, {len(pi['globals'])} exported globals, {len(pi['macros'])} macros in own headers.")
    w("")
    if pi["functions"] or pi["globals"]:
        w("| Kind | Name | Defined at | Declared in |")
        w("|---|---|---|---|")
        for kind, entries in (("function", pi["functions"]), ("global", pi["globals"])):
            for e in entries:
                w(f"| {kind} | `{e['name']}` | {('`' + e['defined_at'] + '`') if e['defined_at'] else '-'} | {', '.join('`' + d + '`' for d in e['declared_in']) or '-'} |")
        w("")

    for title, edges, key in (("Outgoing dependencies", model["dependencies"]["outgoing"], "to"),
                              ("Incoming dependencies (analyzed modules only)", model["dependencies"]["incoming"], "from")):
        w(f"## {title}")
        w("")
        if not edges:
            w("None.")
            w("")
            continue
        grouped = defaultdict(list)
        for e in edges:
            grouped[e[key]].append(e)
        w(f"{len(edges)} edges with {len(grouped)} modules.")
        w("")
        for mid in sorted(grouped, key=lambda m: (names[m]["kind"], names[m]["name"].lower(), m)):
            m = names[mid]
            w(f"### `{m['name']}` ({m['kind']})")
            w("")
            w("| Kind | Symbol | Uses | Via macro | First location |")
            w("|---|---|---|---|---|")
            for e in grouped[mid]:
                via = sorted({(ev["via_macro"], ev["via_macro_file"]) for ev in e["evidence"] if ev["via_macro"]})
                via_text = "; ".join(f"`{_esc(m)}` ({f})" for m, f in via[:2]) + (" …" if len(via) > 2 else "")
                w(f"| {e['kind']} | `{_esc(e['symbol'])}` | {len(e['evidence'])} | {via_text or '-'} | {_loc(e['evidence'][0])} |")
            w("")

    if design is not None:
        w("## Design comparison")
        w("")
        if not design["found"]:
            w("The design model has no component for this module.")
        else:
            d = design["design"]
            w(f"Component `{d['path']}` ({d['stereotype'] or 'no stereotype'}): {len(d['provided'])} provided, "
              f"{len(d['required'])} required interfaces.")
            w("")
            w("| Design provides | Code exports | Match |")
            w("|---|---|---|")
            for row in design["provided"]:
                w(f"| `{_esc(row['design'])}` | {('`' + _esc(row['code']) + '`') if row['code'] else '-'} | {row['how']} |")
            for name in design["code_only_exports"]:
                w(f"| - | `{_esc(name)}` | code only |")
            w("")
            w("| Design requires | Kind | Used by the code |")
            w("|---|---|---|")
            for r in design["required_functions"]:
                w(f"| `{_esc(r['port'])}` | function | {', '.join('`' + _esc(u) + '`' for u in r['used']) or 'no'} |")
            for r in design["required_signals"]:
                w(f"| `{_esc(r['signal'])}` | signal | {', '.join(r['accessed']) or 'no'} |")
            w("")
            if design["code_only_signals"] or design["code_only_app_symbols"]:
                w("Used by the code but not required in the design: "
                  + ", ".join(f"signal `{_esc(s)}`" for s in design["code_only_signals"])
                  + (", " if design["code_only_signals"] and design["code_only_app_symbols"] else "")
                  + ", ".join(f"`{_esc(s)}`" for s in design["code_only_app_symbols"]) + ".")
        w("")

    w("## Signals")
    w("")
    if not model["signals"]:
        w("None (no signal pool in the profile, or the module uses no signal accessor).")
    else:
        w(f"{len(model['signals'])} signals touched. Writers and readers are App modules in the build; "
          "a signal without writer is produced elsewhere or stale.")
        w("")
        w("| Signal | This module | Writers | Readers |")
        w("|---|---|---|---|")
        for s in model["signals"]:
            writers = ", ".join(f"`{names[m]['name']}`" for m in s["writers"]) or "-"
            readers = ", ".join(f"`{names[m]['name']}`" for m in s["readers"]) or "-"
            w(f"| `{_esc(s['name'])}` | {', '.join(s['target_access'])} | {writers} | {readers} |")
    w("")

    w("## Unresolved calls (function pointers)")
    w("")
    if not model["unresolved_calls"]:
        w("None.")
    else:
        for u in model["unresolved_calls"]:
            w(f"- `{_esc(u['expression'])}` at {_loc(u)}")
    w("")

    w("## Function metrics")
    w("")
    if metrics:
        w("| Function | Location | CCN | Nesting | NLOC | Params |")
        w("|---|---|---|---|---|---|")
        for m in sorted(metrics, key=lambda m: (-m["ccn"], m["file"], m["start_line"])):
            w(f"| `{m['name']}` | `{m['file']}:{m['start_line']}` | {m['ccn']} | {m['nesting']} | {m['nloc']} | {m['params']} |")
    else:
        w("No functions.")
    w("")

    w("## Known blind spots")
    w("")
    for b in BLIND_SPOTS:
        w(f"- {b}")
    w("")
    return "\n".join(lines)
