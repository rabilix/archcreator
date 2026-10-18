"""Target profile: everything that is specific to one analyzed repository.

Profiles are validated strictly. Unknown keys or wrong types are rejected,
never silently repaired.
"""

import fnmatch
import math
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .canonical import sha256_text


class ProfileError(Exception):
    pass


SEVERITIES = ("info", "minor", "major", "critical")

DEFAULT_SEVERITY = {
    "NAMING-001": "major",
    "COMPLEXITY-001": "minor",
    "INTERFACE-001": "major",
    "BUILD-001": "info",
    "SIGNAL-001": "info",
    "SIGNAL-002": "minor",
    "DESIGN-000": "major",
    "DESIGN-001": "major",
    "DESIGN-002": "minor",
    "DESIGN-003": "minor",
    "DESIGN-004": "info",
    "DESIGN-005": "minor",
    "DESIGN-006": "info",
    "ARCH-001": "major",
    "LAYER-001": "major",
    "EXPORT-001": "info",
}

DEFAULT_COMPLEXITY = {"ccn_max": 15, "nesting_max": 5, "nloc_max": 150, "params_max": 6}


@dataclass
class BuildList:
    makefile: str
    variables: list[str]
    base_dir: str
    # Mirrors make's SPATH: the directory of every listed source is an include dir.
    add_source_dirs_to_includes: bool = False


SIGNAL_ACCESSES = ("read", "write", "test", "error_read", "error_write")
_SIGNAL_PATTERN = re.compile(r"^[A-Za-z0-9_]*\{signal\}[A-Za-z0-9_]*$")


@dataclass(frozen=True)
class SignalAccessor:
    """A macro name pattern with one `{signal}` placeholder, e.g. `SP_Get_{signal}`, and what a use of it means."""
    macro: str
    access: str

    def regex(self) -> re.Pattern:
        prefix, suffix = self.macro.split("{signal}")
        return re.compile(f"^{re.escape(prefix)}(?P<signal>[A-Za-z0-9_]+?){re.escape(suffix)}$")


@dataclass
class SignalPool:
    """Signals exchanged through accessor macros of one header (repo-relative, as the model labels it)."""
    header: str
    accessors: list[SignalAccessor]

    def __post_init__(self) -> None:
        self._compiled = [(a.regex(), a.access) for a in self.accessors]

    def match(self, macro_name: str) -> tuple[str, str] | None:
        """(signal, access) for an accessor macro name; the first listed pattern that matches wins."""
        for rx, access in self._compiled:
            m = rx.match(macro_name)
            if m:
                return m.group("signal"), access
        return None


@dataclass
class Layers:
    """`layers` of a profile: file globs assign modules to layers (first match wins); `allow` lists, per layer,
    the layers it may depend on (its own is always allowed); `edge_kinds` says which dependencies count."""
    assign: dict[str, list[str]]
    allow: dict[str, list[str]]
    edge_kinds: list[str]


@dataclass
class Profile:
    name: str = "default"
    # Folders (relative to the repository) whose .c files are the modules: the project's own code and whatever
    # delivered applications are built with it. The build list says which of them are built.
    scope: list[str] = field(default_factory=list)
    working_dir: str = "."
    include_dirs: list[str] = field(default_factory=list)
    system_include_dirs: list[str] = field(default_factory=list)
    defines: list[str] = field(default_factory=list)
    args: list[str] = field(default_factory=lambda: ["-std=c11"])
    build_list: BuildList | None = None
    prefix_aliases: dict[str, list[str]] = field(default_factory=dict)
    header_owner: dict[str, str] = field(default_factory=dict)
    severity: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_SEVERITY))
    complexity: dict[str, int] = field(default_factory=lambda: dict(DEFAULT_COMPLEXITY))
    min_parse_coverage: float = 0.0
    source_hash: str = "builtin-default"
    # Which repositories a central profile applies to (see find_profile).
    match_git_remotes: list[str] = field(default_factory=list)
    match_repo_names: list[str] = field(default_factory=list)
    # How the profile was chosen: "explicit" (--profile), "central" (matched) or "none" (built-in defaults).
    origin: str = "none"
    file_name: str | None = None
    # Accepted findings, resolved relative to the profile file (see baseline.py); None without a baseline.
    baseline_path: Path | None = None
    signal_pool: SignalPool | None = None
    design: "DesignSpec | None" = None
    layers: Layers | None = None
    export_exempt: list[str] = field(default_factory=list)


_TOP_KEYS = {"profile_version", "name", "match", "scope", "app_dir", "compile", "build_list", "modules", "rules",
             "gate", "baseline", "signal_pool", "design", "layers"}
_DESIGN_KEYS = {"model", "component_root", "signal_packages"}
_LAYER_KEYS = {"assign", "allow", "edge_kinds"}
_DEFAULT_LAYER_KINDS = ["call", "fn_address", "global_read", "global_write", "global_addr"]
_EDGE_KINDS = {"include", "call", "fn_address", "global_read", "global_write", "global_addr", "macro_use", "enum_use"}
_SIGNAL_KEYS = {"header", "accessors"}
_ACCESSOR_KEYS = {"macro", "access"}
_MATCH_KEYS = {"git_remotes", "repo_names"}
_COMPILE_KEYS = {"working_dir", "include_dirs", "system_include_dirs", "defines", "args"}
_BUILD_KEYS = {"makefile", "variables", "base_dir", "add_source_dirs_to_includes"}
_BUILD_REQUIRED = {"makefile", "variables", "base_dir"}
_MODULE_KEYS = {"prefix_aliases", "header_owner", "export_exempt"}
_RULE_KEYS = {"severity", "complexity"}
_GATE_KEYS = {"min_parse_coverage"}


def _check_keys(section: str, data: dict, allowed: set[str]) -> None:
    if not isinstance(data, dict):
        raise ProfileError(f"'{section}' must be a mapping")
    if not all(isinstance(k, str) for k in data):
        raise ProfileError(f"'{section}' has a key that is not a string")
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise ProfileError(f"unknown key(s) in '{section}': {', '.join(unknown)}")


def _str_list(section: str, value) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ProfileError(f"'{section}' must be a list of strings")
    return list(value)


def _str(section: str, value, optional: bool = False):
    if value is None and optional:
        return None
    if not isinstance(value, str):
        raise ProfileError(f"'{section}' must be a string")
    return value


def load_profile(path: Path) -> Profile:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ProfileError(f"cannot read profile {path}: {exc.strerror or exc}") from None
    except UnicodeDecodeError as exc:
        raise ProfileError(f"profile {path.name} is not UTF-8: {exc}") from None
    try:
        data = yaml.safe_load(text) or {}
    except (yaml.YAMLError, ValueError) as exc:  # ValueError: an unquoted date that does not exist
        raise ProfileError(f"profile {path.name} is not valid YAML: {exc}") from None
    _check_keys("<root>", data, _TOP_KEYS)
    if data.get("profile_version") != 1:
        raise ProfileError("profile_version must be 1")

    p = Profile(source_hash=sha256_text(text), origin="explicit", file_name=path.name)
    p.name = str(data.get("name", path.stem))
    if "scope" in data and "app_dir" in data:
        raise ProfileError("'scope' and its old name 'app_dir' cannot both be given")
    key = "scope" if "scope" in data else "app_dir"
    raw_scope = data.get(key)
    if raw_scope is None:
        p.scope = []
    elif isinstance(raw_scope, str):
        p.scope = [raw_scope]
    else:
        p.scope = _str_list(key, raw_scope)
    if len(set(p.scope)) != len(p.scope):
        raise ProfileError(f"'{key}' lists a folder twice")
    baseline = _str("baseline", data.get("baseline"), optional=True)
    p.baseline_path = path.parent / baseline if baseline else None

    match = data.get("match", {})
    _check_keys("match", match, _MATCH_KEYS)
    p.match_git_remotes = _str_list("match.git_remotes", match.get("git_remotes", []))
    p.match_repo_names = _str_list("match.repo_names", match.get("repo_names", []))

    comp = data.get("compile", {})
    _check_keys("compile", comp, _COMPILE_KEYS)
    p.working_dir = _str("compile.working_dir", comp.get("working_dir", "."))
    p.include_dirs = _str_list("compile.include_dirs", comp.get("include_dirs", []))
    p.system_include_dirs = _str_list("compile.system_include_dirs", comp.get("system_include_dirs", []))
    p.defines = _str_list("compile.defines", comp.get("defines", []))
    p.args = _str_list("compile.args", comp.get("args", ["-std=c11"]))

    if "build_list" in data:
        bl = data["build_list"]
        _check_keys("build_list", bl, _BUILD_KEYS)
        missing = sorted(_BUILD_REQUIRED - set(bl))
        if missing:
            raise ProfileError(f"build_list is missing: {', '.join(missing)}")
        add_dirs = bl.get("add_source_dirs_to_includes", False)
        if not isinstance(add_dirs, bool):
            raise ProfileError("build_list.add_source_dirs_to_includes must be true or false")
        p.build_list = BuildList(_str("build_list.makefile", bl["makefile"]),
                                 _str_list("build_list.variables", bl["variables"]),
                                 _str("build_list.base_dir", bl["base_dir"]), add_dirs)

    mods = data.get("modules", {})
    _check_keys("modules", mods, _MODULE_KEYS)
    aliases = mods.get("prefix_aliases", {})
    if not isinstance(aliases, dict) or not all(isinstance(k, str) for k in aliases):
        raise ProfileError("modules.prefix_aliases must be a mapping of module name -> list of prefixes")
    p.prefix_aliases = {k: _str_list(f"modules.prefix_aliases.{k}", v) for k, v in aliases.items()}
    owners = mods.get("header_owner", {})
    if not isinstance(owners, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in owners.items()):
        raise ProfileError("modules.header_owner must map header path -> module name")
    p.header_owner = dict(owners)
    p.export_exempt = _str_list("modules.export_exempt", mods.get("export_exempt", []))

    if "layers" in data:
        ly = data["layers"]
        _check_keys("layers", ly, _LAYER_KEYS)
        assign = ly.get("assign", {})
        _check_keys("layers.assign", assign, set(assign) if isinstance(assign, dict) else set())
        if not assign:
            raise ProfileError("layers.assign must map at least one layer to file globs")
        allow = ly.get("allow", {})
        _check_keys("layers.allow", allow, set(assign))
        kinds = _str_list("layers.edge_kinds", ly.get("edge_kinds", _DEFAULT_LAYER_KINDS))
        unknown = sorted(set(kinds) - _EDGE_KINDS)
        if unknown:
            raise ProfileError(f"layers.edge_kinds: unknown kind(s) {', '.join(unknown)}")
        p.layers = Layers({k: _str_list(f"layers.assign.{k}", v) for k, v in assign.items()},
                          {k: _str_list(f"layers.allow.{k}", v) for k, v in allow.items()}, kinds)
        unknown_targets = sorted({t for ts in p.layers.allow.values() for t in ts} - set(assign))
        if unknown_targets:
            raise ProfileError(f"layers.allow names layers that are not assigned: {', '.join(unknown_targets)}")

    rules = data.get("rules", {})
    _check_keys("rules", rules, _RULE_KEYS)
    severity = rules.get("severity", {})
    _check_keys("rules.severity", severity, set(DEFAULT_SEVERITY))
    for rule_id, level in severity.items():
        if level not in SEVERITIES:
            raise ProfileError(f"rules.severity.{rule_id}: '{level}' not in {SEVERITIES}")
        p.severity[rule_id] = level
    complexity = rules.get("complexity", {})
    _check_keys("rules.complexity", complexity, set(DEFAULT_COMPLEXITY))
    for key, value in complexity.items():
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ProfileError(f"rules.complexity.{key} must be a non-negative integer")
        p.complexity[key] = value

    if "signal_pool" in data:
        sp = data["signal_pool"]
        _check_keys("signal_pool", sp, _SIGNAL_KEYS)
        if "header" not in sp or "accessors" not in sp:
            raise ProfileError("signal_pool needs 'header' and 'accessors'")
        accessors = sp["accessors"]
        if not isinstance(accessors, list) or not accessors:
            raise ProfileError("signal_pool.accessors must be a non-empty list")
        parsed = []
        for i, acc in enumerate(accessors):
            _check_keys(f"signal_pool.accessors[{i}]", acc, _ACCESSOR_KEYS)
            macro = _str(f"signal_pool.accessors[{i}].macro", acc.get("macro"))
            if not _SIGNAL_PATTERN.match(macro):
                raise ProfileError(f"signal_pool.accessors[{i}].macro must be identifier characters around one "
                                   f"{{signal}} placeholder, got {macro!r}")
            access = acc.get("access")
            if access not in SIGNAL_ACCESSES:
                raise ProfileError(f"signal_pool.accessors[{i}].access must be one of {SIGNAL_ACCESSES}")
            parsed.append(SignalAccessor(macro, access))
        p.signal_pool = SignalPool(Path(_str("signal_pool.header", sp["header"])).as_posix(), parsed)

    if "design" in data:
        from .design import DEFAULT_SIGNAL_PACKAGES, DesignSpec
        ds = data["design"]
        _check_keys("design", ds, _DESIGN_KEYS)
        if "model" not in ds:
            raise ProfileError("design needs 'model' (path of the .qea relative to the repository)")
        packages = ds.get("signal_packages", list(DEFAULT_SIGNAL_PACKAGES))
        p.design = DesignSpec(Path(_str("design.model", ds["model"])).as_posix(),
                              _str("design.component_root", ds.get("component_root"), optional=True),
                              tuple(_str_list("design.signal_packages", packages)))

    gate = data.get("gate", {})
    _check_keys("gate", gate, _GATE_KEYS)
    cov = gate.get("min_parse_coverage", 0.0)
    if isinstance(cov, bool) or not isinstance(cov, (int, float)) or not math.isfinite(cov) or not 0 <= cov <= 1:
        raise ProfileError("gate.min_parse_coverage must be a number between 0 and 1")
    p.min_parse_coverage = float(cov)
    return p


# --- Central profiles -------------------------------------------------------------------------------------------

PROFILES_ENV = "ARCHREV_PROFILES"


def normalize_remote(url: str) -> str:
    """'git@host:org/repo.git', 'ssh://git@host/org/repo' and 'https://host/org/repo.git' -> 'host/org/repo'."""
    u = url.strip().lower().rstrip("/")
    u = re.sub(r"^[a-z+]+://", "", u)
    u = re.sub(r"^[^@/]+@", "", u)
    u = re.sub(r"^([^/:]+):(?!\d+/)", r"\1/", u)   # scp-like host:path
    u = re.sub(r"^([^/:]+):\d+/", r"\1/", u)       # drop a port
    return u[:-4] if u.endswith(".git") else u


def default_profile_dirs() -> list[Path]:
    """$ARCHREV_PROFILES (os.pathsep-separated), then profiles/ next to a frozen executable or the source checkout."""
    dirs = [Path(d) for d in os.environ.get(PROFILES_ENV, "").split(os.pathsep) if d.strip()]
    if getattr(sys, "frozen", False):
        dirs.append(Path(sys.executable).parent / "profiles")
    else:
        dirs.append(Path(__file__).resolve().parents[2] / "profiles")
    return dirs


def _git_remotes(repo: Path) -> list[str]:
    try:
        out = subprocess.run(["git", "-C", str(repo), "remote", "-v"], capture_output=True, text=True, check=True)
    except (OSError, subprocess.CalledProcessError):
        return []
    return sorted({normalize_remote(line.split()[1]) for line in out.stdout.splitlines() if len(line.split()) >= 2})


def _is_baseline(path: Path) -> bool:
    """Baselines live next to the profiles; they are recognised by their content, not their name."""
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError, ValueError):
        return False  # load_profile reports the real problem
    return isinstance(data, dict) and "baseline_version" in data and "profile_version" not in data


def find_profile(repo: Path, search_dirs: list[Path]) -> Profile | None:
    """The one central profile whose `match` names this repository, or None. Several matches are an error."""
    remotes = set(_git_remotes(repo))
    name = repo.resolve().name.lower()
    seen: dict[Path, Profile] = {}
    for d in search_dirs:
        if not d.is_dir():
            continue
        for f in sorted(d.glob("*.yaml")):
            real = f.resolve()
            if real in seen or _is_baseline(f):
                continue
            try:
                p = load_profile(f)
            except ProfileError as exc:
                raise ProfileError(f"central profile {f.name}: {exc}") from exc
            by_remote = remotes & {normalize_remote(r) for r in p.match_git_remotes}
            by_name = any(fnmatch.fnmatchcase(name, pat.lower()) for pat in p.match_repo_names)
            if by_remote or by_name:
                seen[real] = p
    if len(seen) > 1:
        names = ", ".join(sorted(p.file_name or "?" for p in seen.values()))
        raise ProfileError(f"several central profiles match this repository: {names}; pass --profile")
    if not seen:
        return None
    p = next(iter(seen.values()))
    p.origin = "central"
    return p
