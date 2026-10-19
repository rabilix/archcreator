"""Thin CLI adapter. All logic lives in the library; a later GUI can call the same functions."""

import argparse
import json
import re
import sys
import traceback
from pathlib import Path

from . import __version__
from .baseline import Baseline, BaselineError, draft, load_baseline
from .canonical import dumps, write_json, write_text
from .design import DesignError, DesignSpec
from .discovery import DiscoveryError
from .pipeline import (EXIT_INTERNAL, EXIT_USAGE, MODULES_SCHEMA, SUMMARY_SCHEMA, error_summary, export_xmi,
                       list_modules, list_modules_data, run_analysis, run_scan)
from .scan import GRAPH_SCHEMA, SCAN_SUMMARY_SCHEMA
from .profile import PROFILES_ENV, Profile, ProfileError, default_profile_dirs, find_profile, load_profile
from .rules import SEVERITY_ORDER
from .xmi import XMI_TARGET

_MODULE_NAME = re.compile(r"[A-Za-z0-9_]+")


def _profile(args, repo: Path) -> Profile:
    """--profile wins; else the central profile that matches the repository; else built-in defaults."""
    if args.profile:
        return load_profile(Path(args.profile))
    dirs = [Path(d) for d in args.profiles_dir] if args.profiles_dir else default_profile_dirs()
    return find_profile(repo, dirs) or Profile()


def _profile_args(sub) -> None:
    sub.add_argument("--profile", help="target profile YAML (include paths, defines, build list, rules); "
                                       "default: the central profile whose 'match' names the repository")
    sub.add_argument("--profiles-dir", action="append",
                     help=f"central profile folder (repeatable; default: ${PROFILES_ENV}, then the bundled profiles/)")


def _out_dir(args) -> Path | None:
    if args.out:
        return Path(args.out)
    if args.command == "scan":
        return Path("archcreator-out") / "scan"
    # The default folder is derived from the module name; never from a name that could leave it.
    return Path("archcreator-out") / args.module if _MODULE_NAME.fullmatch(args.module) else None


def version_info() -> dict:
    return {"tool": "archcreator", "version": __version__,
            "schemas": {"summary": SUMMARY_SCHEMA, "modules": MODULES_SCHEMA, "scan_summary": SCAN_SUMMARY_SCHEMA,
                        "graph": GRAPH_SCHEMA},
            "xmi_target": XMI_TARGET}


class _UsageError(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # type: ignore[override]
        # Raised instead of exiting so that the error summary is written like for every other usage error.
        raise _UsageError(f"{message}\n{self.format_usage()}".rstrip())


def _argv_option(argv: list[str], name: str) -> str | None:
    """Value of `name` in a raw argument list, for the error summary when parsing itself failed.

    Like argparse: the last occurrence wins, and another option is not a value.
    """
    value = None
    for i, token in enumerate(argv):
        if token == name and i + 1 < len(argv) and not argv[i + 1].startswith("-"):
            value = argv[i + 1]
        elif token.startswith(name + "="):
            value = token[len(name) + 1:]
    return value


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="archcreator", description="Deterministic architecture analysis of one C module.")
    sub = parser.add_subparsers(dest="command", required=True)

    def run_args(sub_parser, out_help: str) -> None:
        sub_parser.add_argument("--repo", required=True, help="root of the target repository")
        _profile_args(sub_parser)
        sub_parser.add_argument("--scope", action="append",
                                help="folder (relative to --repo) whose .c files are modules; repeatable; "
                                     "default: 'scope' of the profile, else the single App* folder")
        sub_parser.add_argument("--out", help=out_help)
        sub_parser.add_argument("--fail-on", choices=sorted(SEVERITY_ORDER, key=SEVERITY_ORDER.get),
                                help="exit 1 if a finding nobody accepted has at least this severity")
        sub_parser.add_argument("--xmi", action="store_true",
                                help="also write model.xmi (XMI 2.1 for Enterprise Architect 16)")
        sub_parser.add_argument("--baseline",
                                help="accepted findings YAML (default: 'baseline' of the profile; none otherwise)")
        sub_parser.add_argument("--design", help="Enterprise Architect .qea to compare against (default: "
                                                 "'design.model' of the profile, relative to the repository)")
        sub_parser.add_argument("--no-design", action="store_true",
                                help="skip the design comparison even if the profile names one")

    a = sub.add_parser("analyze", help="analyze one module and write model, findings, summary and report")
    a.add_argument("--module", required=True, help="module name = C file name without .c (e.g. DCDC_FSM)")
    a.add_argument("--context", help="comma-separated in-build modules to parse alongside, or 'all': callers, "
                                     "signal producers and consumers are known among these only (default: none)")
    run_args(a, "output directory (default: archcreator-out/<module>)")

    s = sub.add_parser("scan", help="analyze the named modules from one parse, plus the module graph and the "
                                    "rules over them (cycles, layers, unused exports)")
    group = s.add_mutually_exclusive_group(required=True)
    group.add_argument("--modules", help="comma-separated in-build module names to analyze")
    group.add_argument("--all", action="store_true", help="every in-build module of the scope")
    run_args(s, "output directory (default: archcreator-out/scan; one folder per module inside)")

    x = sub.add_parser("export-xmi", help="write the XMI 2.1 for Enterprise Architect 16 of an analyze output folder")
    x.add_argument("--in", dest="in_dir", required=True, help="output folder of analyze (model.json, manifest.json)")
    x.add_argument("--out", help="XMI file to write (default: <--in>/model.xmi)")

    bd = sub.add_parser("baseline-draft", help="write a baseline draft with every finding of an analyze output folder; "
                                               "a person must fill in reason, approved_by and date")
    bd.add_argument("--in", dest="in_dir", required=True, help="output folder of analyze (findings.json)")
    bd.add_argument("--out", help="YAML file to write (default: print)")

    lm = sub.add_parser("list-modules", help="list the scope and the modules (C files) in it")
    lm.add_argument("--repo", required=True)
    _profile_args(lm)
    lm.add_argument("--scope", action="append")
    lm.add_argument("--json", action="store_true", help="print JSON (schema archcreator.modules/1)")

    m = sub.add_parser("mcp", help="serve the analysis as an MCP server on stdin/stdout (for MCP clients)")
    m.add_argument("--repo", required=True, help="root of the target repository (fixed for the whole session)")
    _profile_args(m)
    m.add_argument("--scope", action="append", help="folder whose .c files are modules; repeatable (default: profile)")
    m.add_argument("--out-root", default="archcreator-out",
                   help="folder for the results, one subfolder per module (default: ./archcreator-out)")

    v = sub.add_parser("version", help="print the tool version and output schema versions")
    v.add_argument("--json", action="store_true")
    return parser


def _run(args) -> int:
    if args.command == "version":
        info = version_info()
        print(dumps(info), end="") if args.json else print(f"archcreator {info['version']}")
        return 0
    if args.command == "export-xmi":
        print(f"archcreator: wrote {export_xmi(Path(args.in_dir), Path(args.out) if args.out else None)}")
        return 0
    if args.command == "baseline-draft":
        findings_file = Path(args.in_dir) / "findings.json"
        try:
            findings = json.loads(findings_file.read_text(encoding="utf-8"))["findings"]
        except FileNotFoundError:
            raise DiscoveryError(f"not an archcreator output folder (run analyze first): {findings_file}") from None
        text = draft(findings)
        if args.out:
            write_text(Path(args.out), text)
            print(f"archcreator: wrote {args.out} ({len(findings)} entries to fill in)")
        else:
            print(text, end="")
        return 0
    repo = Path(args.repo)
    if not repo.is_dir():
        raise DiscoveryError(f"repo does not exist: {repo}")
    if args.command == "mcp":
        from .mcp import run_stdio
        _profile(args, repo)  # a broken profile stops the server at start, not at the first tool call
        return run_stdio(repo, Path(args.out_root), args.profile, args.profiles_dir, args.scope)
    profile = _profile(args, repo)
    if args.command == "list-modules":
        if args.json:
            print(dumps(list_modules_data(repo, profile, args.scope)), end="")
        else:
            print("\n".join(list_modules(repo, profile, args.scope)))
        return 0
    out = _out_dir(args)
    baseline = _baseline(args, profile)
    design, design_path = _design(args, profile)
    if args.command == "scan":
        modules = _names(args.modules, "--modules") if args.modules is not None else ["all"]
        code = run_scan(repo, profile, args.scope, out, args.fail_on, xmi=args.xmi, baseline=baseline,
                        design=design, design_path=design_path, modules=modules)
        print(f"archcreator: wrote {out} (exit {code})")
        return code
    if out is None:
        raise DiscoveryError(f"invalid module name: {args.module!r} (letters, digits and _ only)")
    context = _names(args.context, "--context") if args.context is not None else None
    code = run_analysis(repo, args.module, profile, args.scope, out, args.fail_on, xmi=args.xmi, baseline=baseline,
                        design=design, design_path=design_path, context=context)
    print(f"archcreator: wrote {out} (exit {code})")
    return code


def _names(text: str, option: str) -> list[str]:
    """A comma-separated module list; 'all' alone stands for every in-build module."""
    names = [m.strip() for m in text.split(",") if m.strip()]
    if names == ["all"]:
        return names
    bad = [m for m in names if not _MODULE_NAME.fullmatch(m) or m == "all"]
    if bad or not names:
        raise DiscoveryError(f"{option}: invalid module name(s): {', '.join(bad) or '(empty)'} "
                             "(letters, digits and _ only; 'all' stands alone)")
    return names


def _design(args, profile: Profile) -> tuple[DesignSpec | None, Path | None]:
    """--no-design wins; else --design (with the profile's component root); else the profile's design; else none."""
    if args.no_design:
        return None, None
    if args.design:
        spec = profile.design or DesignSpec(model=args.design)
        return spec, Path(args.design)
    return profile.design, None


def _baseline(args, profile: Profile) -> Baseline | None:
    """--baseline wins; else the profile's baseline; else none (every finding is new)."""
    path = Path(args.baseline) if args.baseline else profile.baseline_path
    return load_baseline(path) if path is not None else None


def _fail(args, code: int, message: str, label: str = "error") -> int:
    print(f"archcreator: {label}: {message}", file=sys.stderr)
    # Front ends read the exit code and the error from the summary; write it whenever an output folder is known.
    if args.command in ("analyze", "scan") and (out := _out_dir(args)) is not None:
        name = "scan_summary.json" if args.command == "scan" else "summary.json"
        summary = error_summary(getattr(args, "module", "") or "", code, message)
        if args.command == "scan":
            summary = {**summary, "schema": SCAN_SUMMARY_SCHEMA}
            summary.pop("module")
        try:
            out.mkdir(parents=True, exist_ok=True)
            write_json(out / name, summary)
        except OSError as exc:
            print(f"archcreator: could not write {out / name}: {exc}", file=sys.stderr)
    return code


def main(argv: list[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    try:
        args = _parser().parse_args(raw)
    except _UsageError as exc:
        # Reconstruct what the summary needs from the raw arguments: the parse did not get that far.
        module, out = _argv_option(raw, "--module") or "", _argv_option(raw, "--out")
        command = raw[0] if raw[:1] and raw[0] in ("analyze", "scan") else None
        args = argparse.Namespace(command=command, module=module, out=out)
        return _fail(args, EXIT_USAGE, str(exc))
    try:
        return _run(args)
    except (ProfileError, BaselineError, DesignError, DiscoveryError) as exc:
        return _fail(args, EXIT_USAGE, str(exc))
    except Exception as exc:  # noqa: BLE001 - anything else is a bug or a broken installation, never a finding
        traceback.print_exc()
        return _fail(args, EXIT_INTERNAL, f"{type(exc).__name__}: {exc}", "internal error")


if __name__ == "__main__":
    sys.exit(main())
