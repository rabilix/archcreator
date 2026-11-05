"""Which EA models must never be written, and whether a target may be (plan section 5). Pure checks, no EA."""

import os
import sqlite3
import stat
from pathlib import Path

MODEL_SUFFIXES = (".qea", ".qeax")
PROTECTED_SUFFIXES = (".qea", ".qeax", ".eap", ".eapx")
EA_TABLES = ("t_object", "t_package", "t_connector", "t_diagram")
_SKIP_DIRS = {".git", ".venv", "node_modules", "__pycache__"}


class GuardError(Exception):
    """A target that must not be written; the message says why."""


def protected_models(repo: Path, design_model: str | None, design_arg: str | None, source: Path | None) -> list[Path]:
    """The profile's design model (relative to the repository), --design, --from, and every EA model in the repository."""
    found: list[Path] = []
    if design_model:
        found.append(repo / design_model)
    if design_arg:
        found.append(Path(design_arg) if Path(design_arg).is_absolute() else repo / design_arg)
    if source is not None:
        found.append(source)
    for root, dirs, files in os.walk(repo):
        dirs[:] = sorted(d for d in dirs if d not in _SKIP_DIRS)
        found += [Path(root) / f for f in sorted(files) if f.lower().endswith(PROTECTED_SUFFIXES)]
    unique: dict[str, Path] = {}
    for p in found:
        unique.setdefault(os.path.normcase(os.path.abspath(p)), Path(os.path.abspath(p)))
    return list(unique.values())


def _inside(path: Path, folder: Path) -> bool:
    try:
        Path(os.path.realpath(path)).relative_to(os.path.realpath(folder))
        return True
    except ValueError:
        return False


def is_ea_model(path: Path) -> bool:
    """An EA project file: an SQLite database with EA's core tables (opened read-only)."""
    try:
        con = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=1)
        try:
            names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        finally:
            con.close()
    except sqlite3.OperationalError as exc:
        if "locked" in str(exc) or "busy" in str(exc):
            raise GuardError(f"{path} is locked by another program: close the model in Enterprise Architect first") \
                from None
        return False
    except sqlite3.Error:
        return False
    return all(t in names for t in EA_TABLES)


def check_target(model: Path, repo: Path, protected: list[Path], creating: bool, out_dirs: tuple[Path, ...] = ()) -> None:
    """Raise GuardError unless `model` may be written (creating=True: it is about to be made by --from)."""
    if not model.name.lower().endswith(MODEL_SUFFIXES):
        raise GuardError(f"the copy must be a .qea or .qeax file: {model}")
    if _inside(model, repo):
        raise GuardError(f"the copy must be outside the repository (models there are protected): {model}")
    for folder in out_dirs:
        if _inside(model, folder):
            raise GuardError(f"the copy must not be inside archcreator's output folder {folder}: {model}")
    for p in protected:
        if os.path.normcase(os.path.abspath(p)) == os.path.normcase(os.path.abspath(model)):
            raise GuardError(f"{model} is a protected model ({p}); write a copy instead")
        if p.exists() and model.exists() and os.path.samefile(p, model):
            raise GuardError(f"{model} is the same file as the protected model {p} (a link); write a copy instead")
    if creating:
        if model.exists():
            raise GuardError(f"the copy already exists; choose a new file or leave out --from: {model}")
        return
    if not model.is_file():
        raise GuardError(f"the copy does not exist; make it with --from <model>: {model}")
    if not os.access(model, os.W_OK) or not (model.stat().st_mode & stat.S_IWRITE):
        raise GuardError(f"the copy is read-only: {model}")
    if not is_ea_model(model):
        raise GuardError(f"not an Enterprise Architect model (no {', '.join(EA_TABLES)}): {model}")
