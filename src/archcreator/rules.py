"""MVP rule families: NAMING-001, COMPLEXITY-001, INTERFACE-001, BUILD-001.

Rules only evaluate. They never alter the model.
"""

from pathlib import PurePosixPath

from .canonical import fingerprint, guid

INTERFACE_KINDS = {"call", "fn_address", "global_read", "global_write", "global_addr"}

RULE_TEXT = {
    "NAMING-001": "Exported symbols carry the module prefix or a declared alias",
    "COMPLEXITY-001": "Function complexity stays within profile thresholds",
    "INTERFACE-001": "Cross-module use only through symbols declared in the owning module's own headers",
    "BUILD-001": "Source files in the scope are part of the build",
    "SIGNAL-001": "Every signal a module reads is written by an App module in the build",
    "SIGNAL-002": "A signal has one producer",
    "DESIGN-000": "The module exists in the design model",
    "DESIGN-001": "Every provided function of the design is exported by the code",
    "DESIGN-002": "Every exported function of the code is provided in the design",
    "DESIGN-003": "Every required function interface of the design is used by the code",
    "DESIGN-004": "The code uses only signals and App symbols the design requires",
    "DESIGN-005": "Every required signal of the design is accessed by the code",
    "DESIGN-006": "Provided functions carry the same name in design and code",
    "ARCH-001": "No dependency cycles between App modules",
    "LAYER-001": "Modules depend only on the layers the profile allows",
    "EXPORT-001": "Every exported symbol is used by some App module",
}


def _finding(rule_id, severity, module, file, line, symbol, message, measured=None, threshold=None,
             evidence=None, element=None, discriminator=""):
    fp = fingerprint(rule_id, module, file or "", symbol or "", discriminator)
    return {
        "fingerprint": fp,
        "rule_id": rule_id,
        "severity": severity,
        "module": module,
        "file": file,
        "line": line,
        "symbol": symbol,
        "message": message,
        "measured_value": measured,
        "threshold": threshold,
        "evidence": evidence or [],
        "element_guid": guid(element) if element else None,
        "status": "new",
    }


def check_naming(model: dict, profile) -> list[dict]:
    target = model["target"]
    name = target["name"]
    prefixes = [f"{name}_"] + list(profile.prefix_aliases.get(name, []))
    sev = profile.severity["NAMING-001"]
    findings = []
    for kind, entries in (("function", model["provided_interface"]["functions"]),
                          ("global", model["provided_interface"]["globals"])):
        for e in entries:
            if any(e["name"].startswith(p) for p in prefixes):
                continue
            where = e["defined_at"] or (e["declared_in"][0] if e["declared_in"] else "")
            file, _, line = where.rpartition(":")
            findings.append(_finding(
                "NAMING-001", sev, name, file or None, int(line) if line.isdigit() else None, e["name"],
                f"exported {kind} '{e['name']}' does not start with any of {prefixes}",
                measured=e["name"], threshold=prefixes, element=target["id"]))
    return findings


def check_complexity(metrics: list[dict], model: dict, profile) -> list[dict]:
    limits = profile.complexity
    sev = profile.severity["COMPLEXITY-001"]
    checks = (("ccn", "ccn_max", "cyclomatic complexity"),
              ("nesting", "nesting_max", "nesting depth"),
              ("nloc", "nloc_max", "lines of code"),
              ("params", "params_max", "parameter count"))
    findings = []
    for m in metrics:
        for key, limit_key, label in checks:
            limit = limits[limit_key]
            if m[key] > limit:
                findings.append(_finding(
                    "COMPLEXITY-001", sev, model["target"]["name"], m["file"], m["start_line"], m["name"],
                    f"{label} of '{m['name']}' is {m[key]} (limit {limit})",
                    measured=m[key], threshold=limit, element=model["target"]["id"], discriminator=key))
    return findings


def _declared_by_owner(edge: dict, owner_files: list[str]) -> bool:
    owner_headers = {f for f in owner_files if f.lower().endswith(".h")}
    return any(f in owner_headers for f in edge["declared_in"])


def _mediated(ev: dict, owner_id: str, file_owner: dict[str, str]) -> bool:
    """A use expanded from a macro defined outside the scope (e.g. generated RTE)
    or in the owner's own files goes through an interface the owner side provides."""
    via_file = ev.get("via_macro_file")
    if not via_file:
        return False
    via_module = file_owner.get(via_file)
    return via_module is None or via_module == owner_id


def _bypass_evidence(edge: dict, owner_id: str, file_owner: dict[str, str]) -> list[dict]:
    return [ev for ev in edge["evidence"] if not _mediated(ev, owner_id, file_owner)]


def check_interface(model: dict, file_owner: dict[str, str], profile) -> list[dict]:
    sev = profile.severity["INTERFACE-001"]
    target = model["target"]
    files_of: dict[str, list[str]] = {}
    for f, m in file_owner.items():
        files_of.setdefault(m, []).append(f)
    kinds = {m["id"]: m["kind"] for m in model["modules"]}
    findings = []

    for e in model["dependencies"]["outgoing"]:
        if e["kind"] not in INTERFACE_KINDS or kinds.get(e["to"]) not in ("c_module", "c_module_unparsed",
                                                                            "not_in_build", "unknown"):
            continue
        if _declared_by_owner(e, files_of.get(e["to"], [])):
            continue
        direct = _bypass_evidence(e, e["to"], file_owner)
        if not direct:
            continue
        if kinds.get(e["to"]) == "unknown":
            # Declared only in the using source: no header of any module provides it, whoever defines it.
            message = (f"{target['name']} uses '{e['symbol']}' through its own declaration; no header in the scope "
                       f"declares it (defined in a module this run did not parse)")
        else:
            owner = PurePosixPath(e["to"].split(":", 1)[1]).stem
            message = (f"{target['name']} uses '{e['symbol']}' of module {owner}, which is not declared in {owner}'s "
                       f"own headers (declared in: {', '.join(e['declared_in']) or 'nowhere in the scope'})")
        findings.append(_finding(
            "INTERFACE-001", sev, target["name"], direct[0]["file"], direct[0]["line"], e["symbol"], message,
            evidence=direct, element=e["id"], discriminator=f"out:{e['to']}:{e['kind']}"))

    own_files = files_of.get(target["id"], [])
    for e in model["dependencies"]["incoming"]:
        if e["kind"] not in INTERFACE_KINDS:
            continue
        if _declared_by_owner(e, own_files):
            continue
        direct = _bypass_evidence(e, target["id"], file_owner)
        if not direct:
            continue
        user = PurePosixPath(e["from"].split(":", 1)[1]).stem
        findings.append(_finding(
            "INTERFACE-001", sev, target["name"], direct[0]["file"], direct[0]["line"], e["symbol"],
            f"module {user} uses '{e['symbol']}' of {target['name']}, which is not declared in {target['name']}'s own headers"
            f" (declared in: {', '.join(e['declared_in']) or 'nowhere in the scope'})",
            evidence=direct, element=e["id"], discriminator=f"in:{e['from']}:{e['kind']}"))
    return findings


def check_build(not_in_build: list[str], target_file: str, profile) -> list[dict]:
    """A repository fact, attributed to the file's own module: the same fingerprint whichever module is analyzed."""
    sev = profile.severity["BUILD-001"]
    findings = []
    for src in not_in_build:
        why = "parsed only because it is the analyzed module" if src == target_file else "it was not parsed"
        findings.append(_finding(
            "BUILD-001", sev, PurePosixPath(src).stem, src, None, None,
            f"{src} is in the scope but not in the build list; {why}",
            element=f"module:{src}"))
    return findings


def check_signals(model: dict, profile) -> list[dict]:
    """Producer/consumer consistency of the signals the target touches (needs a signal_pool in the profile)."""
    target = model["target"]
    names = {m["id"]: m["name"] for m in model["modules"]}
    reads = {"read", "test", "error_read"}
    findings = []
    for s in model["signals"]:
        edges = [e for e in model["dependencies"]["outgoing"] if e["to"] == s["id"]]
        first = min((e["evidence"][0] for e in edges), key=lambda ev: (ev["file"], ev["line"]))
        if reads & set(s["target_access"]) and not s["writers"]:
            findings.append(_finding(
                "SIGNAL-001", profile.severity["SIGNAL-001"], target["name"], first["file"], first["line"], s["name"],
                f"{target['name']} reads signal '{s['name']}' but no analyzed module writes it "
                "(written by a module outside this run, by the signal pool defaults, or stale)",
                evidence=[first], element=s["id"]))
        if "write" in s["target_access"] and len(s["writers"]) > 1:
            others = sorted(names.get(m, m) for m in s["writers"] if m != target["id"])
            findings.append(_finding(
                "SIGNAL-002", profile.severity["SIGNAL-002"], target["name"], first["file"], first["line"], s["name"],
                f"signal '{s['name']}' is written by {len(s['writers'])} modules: {target['name']} and "
                f"{', '.join(others)}",
                measured=len(s["writers"]), threshold=1, evidence=[first], element=s["id"]))
    return findings


SEVERITY_ORDER = {"info": 0, "minor": 1, "major": 2, "critical": 3}


def sort_findings(findings: list[dict]) -> list[dict]:
    """Accepted findings last, so the capped summary shows what a person has not decided on yet."""
    return sorted(findings, key=lambda f: (f.get("status") == "accepted", -SEVERITY_ORDER[f["severity"]],
                                           f["rule_id"], f["file"] or "", f["line"] or 0, f["symbol"] or "",
                                           f["fingerprint"]))
