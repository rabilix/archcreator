"""Per-function complexity metrics with lizard, measured on source as written."""

from importlib.metadata import version
from pathlib import Path

import lizard


def lizard_version() -> str:
    return version("lizard")


def function_metrics(repo: Path, files: list[str]) -> list[dict]:
    analyzer = lizard.FileAnalyzer(lizard.get_extensions(["ns"]))
    result = []
    for rel in files:
        # Sources contain Windows-1252 comments; latin-1 decodes any byte deterministically.
        text = (repo / rel).read_bytes().decode("latin-1")
        info = analyzer.analyze_source_code(rel, text)
        for f in info.function_list:
            result.append({
                "name": f.name,
                "file": rel,
                "start_line": f.start_line,
                "end_line": f.end_line,
                "ccn": f.cyclomatic_complexity,
                "nloc": f.nloc,
                "params": f.parameter_count,
                "nesting": getattr(f, "max_nested_structures", 0),
            })
    return sorted(result, key=lambda m: (m["file"], m["start_line"], m["name"]))


def coupling(model: dict) -> dict:
    # Signals are data, not modules: they do not count towards fan-out.
    out_modules = sorted({e["to"] for e in model["dependencies"]["outgoing"] if not e["to"].startswith("signal:")})
    in_modules = sorted({e["from"] for e in model["dependencies"]["incoming"]})
    ce, ca = len(out_modules), len(in_modules)
    return {
        "fan_out_modules": ce,
        "fan_in_modules": ca,
        "instability": round(ce / (ca + ce), 4) if (ca + ce) else None,
    }
