"""Locating the App folder, the target module, and module membership of files.

Paths handed to later stages are repo-relative POSIX strings. Real paths are
resolved first, so drive substitutions (F: -> C:\\F_Laufwerk) and case
differences in #include spellings do not create duplicate identities.
"""

import os
import re
from dataclasses import dataclass
from pathlib import Path

SKIP_DIRS = {".git", ".venv", "node_modules", "__pycache__", ".svn"}
SOURCE_EXT = (".c",)
HEADER_EXT = (".h",)


class DiscoveryError(Exception):
    pass


class PathMapper:
    """Maps any filesystem path to a stable, machine-independent label."""

    def __init__(self, repo: Path, system_dirs: list[Path]):
        self.repo_real = os.path.realpath(repo)
        self._repo_key = os.path.normcase(self.repo_real)
        self._system = [
            (os.path.normcase(os.path.realpath(d)), f"<system{i}>") for i, d in enumerate(system_dirs)
        ]
        self._cache: dict[str, str] = {}

    def label(self, path: str | None) -> str:
        if not path:
            return "<builtin>"
        cached = self._cache.get(path)
        if cached is not None:
            return cached
        real = os.path.realpath(path)
        key = os.path.normcase(real)
        result = None
        if key == self._repo_key or key.startswith(self._repo_key + os.sep):
            result = Path(os.path.relpath(real, self.repo_real)).as_posix()
        else:
            for sys_key, tag in self._system:
                if key.startswith(sys_key + os.sep):
                    result = f"{tag}/{Path(os.path.relpath(key, sys_key)).as_posix()}"
                    break
        if result is None:
            result = f"<outside>/{Path(real).name}"
        self._cache[path] = result
        return result


def _walk(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        yield Path(dirpath), dirnames, sorted(filenames)


def find_app_dir_candidates(repo: Path) -> list[Path]:
    found = []
    for dirpath, dirnames, _ in _walk(repo):
        for d in dirnames:
            if d.lower().startswith("app"):
                found.append(dirpath / d)
    return sorted(found)


def _find_c_file(app_dir: Path, module: str) -> list[Path]:
    target = f"{module}.c".lower()
    hits = []
    for dirpath, _, filenames in _walk(app_dir):
        for f in filenames:
            if f.lower() == target:
                hits.append(dirpath / f)
    return hits


@dataclass
class Resolution:
    app_dir: Path
    module_file: Path


def scope_dirs(repo: Path, scope: list[str]) -> list[Path]:
    """The scope folders, deduplicated and in a fixed order: the order of the list carries no meaning."""
    dirs = {}
    for s in scope:
        path = repo / s
        if not path.is_dir():
            raise DiscoveryError(f"scope folder does not exist: {s}")
        dirs[os.path.normcase(os.path.realpath(path))] = path
    return [dirs[k] for k in sorted(dirs)]


def resolve_module(repo: Path, module: str, scope: list[str] | None) -> Resolution:
    if scope:
        hits = []
        seen: set[str] = set()
        for base in scope_dirs(repo, scope):
            for h in _find_c_file(base, module):
                real = os.path.normcase(os.path.realpath(h))
                if real not in seen:  # overlapping scope folders list a file twice
                    seen.add(real)
                    hits.append((base, h))
        if not hits:
            raise DiscoveryError(f"module '{module}': no {module}.c under {', '.join(scope)}")
        if len(hits) > 1:
            listing = ", ".join(Path(os.path.relpath(h, repo)).as_posix() for _, h in hits)
            raise DiscoveryError(f"module '{module}' is ambiguous in the scope: {listing}")
        return Resolution(hits[0][0], hits[0][1])

    by_file: dict[str, list[Path]] = {}
    for cand in find_app_dir_candidates(repo):
        for hit in _find_c_file(cand, module):
            by_file.setdefault(os.path.normcase(os.path.realpath(hit)), []).append(cand)
    if not by_file:
        raise DiscoveryError(f"module '{module}': no {module}.c found under any App* folder; set a scope")
    if len(by_file) > 1:
        listing = ", ".join(sorted(Path(os.path.relpath(k, os.path.realpath(repo))).as_posix() for k in by_file))
        raise DiscoveryError(f"module '{module}' is ambiguous: {listing}; set a scope")
    (real_file, app_dirs), = by_file.items()
    innermost = max(app_dirs, key=lambda p: len(p.parts))
    return Resolution(innermost, Path(real_file))


def list_app_files(dirs: Path | list[Path], mapper: PathMapper) -> tuple[list[str], list[str]]:
    """Sources and headers below the scope folders (nested folders are listed once)."""
    sources, headers = set(), set()
    for d in ([dirs] if isinstance(dirs, Path) else dirs):
        for dirpath, _, filenames in _walk(d):
            for f in filenames:
                low = f.lower()
                if low.endswith(SOURCE_EXT):
                    sources.add(mapper.label(str(dirpath / f)))
                elif low.endswith(HEADER_EXT):
                    headers.add(mapper.label(str(dirpath / f)))
    return sorted(sources), sorted(headers)


def _stem(rel: str) -> str:
    return Path(rel).stem


def module_id_for_source(source_label: str) -> str:
    return f"module:{source_label}"


def assign_modules(sources: list[str], headers: list[str], header_owner: dict[str, str]) -> dict[str, str]:
    """Return file label -> module id for every App file.

    A .c file is its own module ("module:<path>"). A header belongs to the
    same-directory .c whose stem equals the header stem, or is a prefix of it
    followed by '_'; the longest stem wins. Profile overrides (header path ->
    module name) take precedence. Any other header is its own header-only
    module ("header:<path>").
    """
    owner: dict[str, str] = {}
    by_dir: dict[str, list[tuple[str, str]]] = {}
    by_stem: dict[str, list[str]] = {}
    for s in sources:
        owner[s] = module_id_for_source(s)
        by_dir.setdefault(str(Path(s).parent), []).append((_stem(s), s))
        by_stem.setdefault(_stem(s).lower(), []).append(s)

    overrides = {Path(k).as_posix(): v for k, v in header_owner.items()}
    stale = sorted(set(overrides) - set(headers))
    if stale:
        raise DiscoveryError(f"profile header_owner lists headers that are not in the scope: {', '.join(stale)}")

    for h in headers:
        if h in overrides:
            cands = by_stem.get(overrides[h].lower(), [])
            if len(cands) != 1:
                raise DiscoveryError(f"header_owner: module '{overrides[h]}' for {h} matches {len(cands)} source files")
            owner[h] = module_id_for_source(cands[0])
            continue
        hs = _stem(h).lower()
        best: tuple[str, str] | None = None
        for stem, src in by_dir.get(str(Path(h).parent), []):
            cl = stem.lower()
            if hs == cl or hs.startswith(cl + "_"):
                if best is None or len(stem) > len(best[0]) or (len(stem) == len(best[0]) and stem < best[0]):
                    best = (stem, src)
        owner[h] = module_id_for_source(best[1]) if best else f"header:{h}"
    return owner


_VAR_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*(:=|\+=|=)\s*(.*)$")
_REF_RE = re.compile(r"\$\(([A-Za-z_][A-Za-z0-9_]*)\)")


def _simple_vars(lines: list[str]) -> dict[str, str]:
    """Single-line literal assignments (NAME := value) usable for $(NAME) substitution."""
    result: dict[str, str] = {}
    for raw in lines:
        m = _VAR_RE.match(raw.split("#", 1)[0])
        if m and m.group(2) in (":=", "=") and not m.group(3).rstrip().endswith("\\"):
            result[m.group(1)] = m.group(3).strip()
    return result


def _expand(token: str, simple: dict[str, str]) -> str:
    for _ in range(8):
        new = _REF_RE.sub(lambda m: simple.get(m.group(1), m.group(0)), token)
        if new == token:
            break
        token = new
    return token


def read_make_list(makefile: Path, variables: list[str]) -> list[str]:
    """Collect the tokens assigned to the given make variables (:=, =, +=).

    $(NAME) references are expanded from single-line literal assignments in the
    same makefile. Tokens that still contain '$(' cannot be resolved and are
    reported by the caller.
    """
    lines = makefile.read_text(encoding="latin-1").splitlines()
    simple = _simple_vars(lines)
    values: dict[str, list[str]] = {v: [] for v in variables}
    i = 0
    while i < len(lines):
        m = _VAR_RE.match(lines[i].split("#", 1)[0])
        if m and m.group(1) in values:
            collected: list[str] = []
            body = m.group(3)
            while body.rstrip().endswith("\\") and i + 1 < len(lines):
                collected.extend(body.rstrip()[:-1].split())
                i += 1
                body = lines[i].split("#", 1)[0]
            collected.extend(body.split())
            if m.group(2) == "+=":
                values[m.group(1)].extend(collected)
            else:
                values[m.group(1)] = collected
        i += 1
    return [_expand(t, simple) for v in variables for t in values[v]]
