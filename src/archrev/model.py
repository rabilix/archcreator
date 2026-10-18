"""Builds the as-built model of one target module from the IR.

The model is a projection of the IR: every IR reference and include whose
site lies in a target file ends up exactly once as internal, as evidence of
an outgoing dependency, or as an unresolved call. The accounting check proves
this per run.
"""

from collections import defaultdict
from pathlib import PurePosixPath

from .canonical import guid

SYMBOL_KINDS = {"call", "fn_address", "global_read", "global_write", "global_addr", "macro_use", "enum_use"}
# A signal-pool accessor macro use becomes an edge of kind "signal_<access>" to a "signal:<name>" node.
SIGNAL_PREFIX = "signal:"
SIGNAL_READS = ("read", "test", "error_read")
# A symbol whose owner this run could not see (declared only in the using source; definition not parsed).
UNKNOWN_PREFIX = "unknown:"


class ModuleIndex:
    """Resolves files and symbols to module ids."""

    def __init__(self, file_owner: dict[str, str], sources_in_build: set[str], sources_all: list[str],
                 parsed: set[str] | None = None):
        self.file_owner = file_owner
        self.in_build = sources_in_build
        self.sources_all = sources_all
        # Sources parsed in this run; an in-build module outside it is known by its headers only.
        self.parsed = parsed if parsed is not None else set(sources_in_build)

    def module_of_file(self, label: str) -> str:
        owned = self.file_owner.get(label)
        return owned if owned is not None else f"external:{label}"

    def kind_of(self, module_id: str) -> str:
        if module_id.startswith("module:"):
            src = module_id[len("module:"):]
            if src not in self.in_build:
                return "not_in_build"
            return "c_module" if src in self.parsed else "c_module_unparsed"
        if module_id.startswith("header:"):
            return "header_only"
        if module_id.startswith(SIGNAL_PREFIX):
            return "signal"
        if module_id.startswith(UNKNOWN_PREFIX):
            return "unknown"
        return "external"


def module_name(module_id: str) -> str:
    path = module_id.split(":", 1)[1]
    name = PurePosixPath(path).name
    if module_id.startswith("module:"):
        return PurePosixPath(path).stem
    return name


def build_model(ir: dict, target_id: str, index: ModuleIndex, scope_label: str, signal_pool=None) -> dict:
    target_files = sorted(f for f, m in index.file_owner.items() if m == target_id)
    target_set = set(target_files)

    app_defs: dict[str, list[str]] = defaultdict(list)
    for d in ir["declarations"]:
        if d["is_definition"] and d["kind"] in ("function", "variable"):
            app_defs[d["usr"]].append(d["file"])
    app_decl_files: dict[str, set[str]] = defaultdict(set)
    for d in ir["declarations"]:
        app_decl_files[d["usr"]].add(d["file"])

    def owner_of(ref: dict) -> str:
        """The module that owns a referenced symbol.

        Its definition, when parsed; else the header declaring it; else a declaration in another module's source.
        A prototype inside the using source itself ("extern int f(void);" in the caller) names no owner: the
        definition lives in a module this run did not parse, so the use goes to an `unknown:` node.
        """
        defs = app_defs.get(ref["usr"])
        if defs:
            return index.module_of_file(sorted(defs)[0])
        decl_files = sorted(app_decl_files.get(ref["usr"], ()))
        headers = [f for f in decl_files if f.lower().endswith(".h")]
        if headers:
            return index.module_of_file(headers[0])
        site_module = index.module_of_file(ref["site_file"])
        others = [f for f in decl_files if index.module_of_file(f) != site_module]
        if others:
            return index.module_of_file(others[0])
        if ref["decl_file"] in app_decl_files.get(ref["usr"], ()) or ref["decl_file"] == ref["site_file"]:
            return f"{UNKNOWN_PREFIX}{ref['name']}"
        return index.module_of_file(ref["decl_file"])

    outgoing: dict[tuple, list] = defaultdict(list)
    incoming: dict[tuple, list] = defaultdict(list)
    internal_count = 0
    unresolved = []
    target_ref_count = 0
    # signal -> access -> App modules in the build using it that way (the producer/consumer view), and the
    # target's own accesses (it may be outside the build).
    signal_users: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    target_signal_access: dict[str, set[str]] = defaultdict(set)
    wrapper_extra = 0

    macro_bodies = {(m["file"], m["name"]): m["identifiers"] for m in ir.get("macro_bodies", [])}
    defined_in: dict[str, set[str]] = defaultdict(set)
    for d in ir.get("body_macro_definitions", []):
        defined_in[d["name"]].add(d["file"])
    wrapper_cache: dict[tuple[str, str], tuple[tuple[str, str], ...]] = {}

    def definition_for(ident: str, from_file: str) -> str | None:
        """The file whose definition of `ident` a macro body in `from_file` sees: its own file, else the one header
        defining it. A `.c`-local macro of another file is invisible; several headers are ambiguous: none."""
        files = defined_in.get(ident, set())
        if from_file in files:
            return from_file
        headers = sorted(f for f in files if f.lower().endswith(".h"))
        return headers[0] if len(headers) == 1 else None

    def wrapper_accesses(file: str, name: str, depth: int = 0) -> tuple[tuple[str, str], ...]:
        """Signal accesses an App macro performs through its body, directly or through further App wrappers.

        libclang records no instantiation for an accessor expanded from another macro's body, so the body text is
        the only trace: an identifier that is an accessor macro of the signal header counts, and one that is another
        App macro is followed, up to four wrappers deep.
        """
        key = (file, name)
        if key in wrapper_cache:
            return wrapper_cache[key]
        found: list[tuple[str, str]] = []
        if depth < 4:
            for ident in macro_bodies.get(key, ()):
                source = definition_for(ident, file)
                if source == signal_pool.header:
                    matched = signal_pool.match(ident)
                    if matched is not None:
                        found.append(matched)
                elif source is not None and (source, ident) in macro_bodies:
                    found.extend(wrapper_accesses(source, ident, depth + 1))
        wrapper_cache[key] = tuple(sorted(set(found)))
        return wrapper_cache[key]

    def signal_accesses(ref: dict) -> tuple[tuple[str, str], ...]:
        """(signal, access) pairs a macro use stands for: the accessor itself, or the accessors a wrapper's body uses."""
        if signal_pool is None or ref["kind"] != "macro_use":
            return ()
        if ref["decl_file"] == signal_pool.header:
            matched = signal_pool.match(ref["name"])
            return (matched,) if matched else ()
        return wrapper_accesses(ref["decl_file"], ref["name"])

    for ref in ir["references"]:
        site_module = index.module_of_file(ref["site_file"])
        evidence = {"file": ref["site_file"], "line": ref["line"], "function": ref["in_function"],
                    "via_macro": ref["via_macro"] or None, "via_macro_file": ref["via_macro_file"] or None}
        if ref["kind"] == "fn_pointer_call":
            if site_module == target_id:
                target_ref_count += 1
                unresolved.append({**evidence, "expression": ref["name"]})
            continue
        accesses = signal_accesses(ref)
        if accesses:
            if ref["decl_file"] != signal_pool.header:
                # The wrapper is what the code names; keep it as the evidence's macro.
                evidence = {**evidence, "via_macro": ref["name"], "via_macro_file": ref["decl_file"]}
            for signal, access in accesses:
                if index.kind_of(site_module) == "c_module":
                    signal_users[signal][access].add(site_module)
                if site_module == target_id:
                    target_signal_access[signal].add(access)
                    outgoing[(SIGNAL_PREFIX + signal, f"signal_{access}", signal, "")].append(evidence)
            if site_module == target_id:
                target_ref_count += 1
                wrapper_extra += len(accesses) - 1  # one reference, several signal accesses
            continue
        owner = owner_of(ref)
        if site_module == target_id:
            target_ref_count += 1
            if owner == target_id:
                internal_count += 1
            else:
                outgoing[(owner, ref["kind"], ref["name"], ref["usr"])].append(evidence)
        elif owner == target_id:
            incoming[(site_module, ref["kind"], ref["name"], ref["usr"])].append(evidence)

    for inc in ir["includes"]:
        site_module = index.module_of_file(inc["site_file"])
        included_module = index.module_of_file(inc["included_file"])
        evidence = {"file": inc["site_file"], "line": inc["line"], "function": None,
                    "via_macro": None, "via_macro_file": None}
        if site_module == target_id:
            target_ref_count += 1
            if included_module == target_id:
                internal_count += 1
            else:
                outgoing[(included_module, "include", inc["included_file"], "")].append(evidence)
        elif included_module == target_id:
            incoming[(site_module, "include", inc["included_file"], "")].append(evidence)

    modules: dict[str, dict] = {}

    def touch(module_id: str) -> None:
        if module_id not in modules:
            files = sorted(f for f, m in index.file_owner.items() if m == module_id)
            if not files and module_id.startswith("external:"):
                files = [module_id.split(":", 1)[1]]
            if module_id.startswith(UNKNOWN_PREFIX):
                files = []
            modules[module_id] = {"id": module_id, "guid": guid(module_id), "name": module_name(module_id),
                                  "kind": index.kind_of(module_id), "files": files}

    touch(target_id)

    def edges(table: dict, direction: str) -> list[dict]:
        result = []
        for (other, kind, symbol, usr), ev in table.items():
            touch(other)
            src, dst = (target_id, other) if direction == "out" else (other, target_id)
            dep_id = f"dep:{src}->{dst}:{kind}:{symbol}:{usr}"
            result.append({"id": dep_id, "guid": guid(dep_id), "from": src, "to": dst, "kind": kind,
                           "symbol": symbol, "usr": usr, "declared_in": sorted(app_decl_files.get(usr, ())),
                           "evidence": sorted(ev, key=lambda e: (e["file"], e["line"], e["function"] or "",
                                                                 e["via_macro"] or ""))})
        return sorted(result, key=lambda e: (e["to"] if direction == "out" else e["from"], e["kind"], e["symbol"], e["usr"]))

    out_edges = edges(outgoing, "out")
    in_edges = edges(incoming, "in")

    interface = _provided_interface(ir, target_set)

    signals = []
    for signal in sorted(target_signal_access):
        users = signal_users.get(signal, {})
        for ms in users.values():
            for m in ms:
                touch(m)  # producers and consumers belong to the module's view even without a direct edge
        signals.append({
            "name": signal, "id": SIGNAL_PREFIX + signal, "guid": guid(SIGNAL_PREFIX + signal),
            "target_access": sorted(target_signal_access[signal]),
            "by_access": {a: sorted(ms) for a, ms in sorted(users.items())},
            "writers": sorted(users.get("write", ())),
            "readers": sorted({m for a in SIGNAL_READS for m in users.get(a, ())}),
        })

    accounted = internal_count + len(unresolved) + sum(len(e["evidence"]) for e in out_edges) - wrapper_extra
    return {
        "schema": "archrev.module_model/1",
        "scope": scope_label,
        "analyzed_modules": sorted(module_name(f"module:{s}") for s in index.parsed),
        "target": {**modules[target_id], "files": target_files},
        "modules": sorted(modules.values(), key=lambda m: m["id"]),
        "provided_interface": interface,
        "dependencies": {"outgoing": out_edges, "incoming": in_edges},
        "signals": signals,
        "unresolved_calls": sorted(unresolved, key=lambda u: (u["file"], u["line"], u["expression"])),
        "accounting": {
            "target_references": target_ref_count,
            "internal": internal_count,
            "outgoing_evidence": sum(len(e["evidence"]) for e in out_edges),
            "unresolved_calls": len(unresolved),
            "signal_wrapper_extra": wrapper_extra,
            "ok": accounted == target_ref_count,
        },
    }


def _provided_interface(ir: dict, target_set: set[str]) -> dict:
    funcs: dict[str, dict] = {}
    globs: dict[str, dict] = {}
    macros = []
    for d in ir["declarations"]:
        if d["file"] not in target_set:
            continue
        if d["kind"] == "macro":
            if d["file"].lower().endswith(".h"):
                macros.append({"name": d["name"], "file": d["file"], "line": d["line"]})
            continue
        if d["kind"] not in ("function", "variable") or d["linkage"] != "EXTERNAL":
            continue
        is_source = d["file"].lower().endswith(".c")
        if is_source and not d["is_definition"]:
            # An extern declaration inside a .c file is a use of another module, not a provided symbol.
            continue
        table = funcs if d["kind"] == "function" else globs
        entry = table.setdefault(d["usr"], {"name": d["name"], "usr": d["usr"], "defined_at": None, "declared_in": []})
        if d["is_definition"] and d["file"].lower().endswith(".c"):
            entry["defined_at"] = f'{d["file"]}:{d["line"]}'
        elif d["file"].lower().endswith(".h"):
            entry["declared_in"].append(f'{d["file"]}:{d["line"]}')
    for table in (funcs, globs):
        for e in table.values():
            e["declared_in"] = sorted(set(e["declared_in"]))
    return {
        "functions": sorted(funcs.values(), key=lambda e: (e["name"], e["usr"])),
        "globals": sorted(globs.values(), key=lambda e: (e["name"], e["usr"])),
        "macros": sorted(macros, key=lambda m: (m["name"], m["file"], m["line"])),
    }
