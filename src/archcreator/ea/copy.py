"""Make the copy of an EA model with SQLite's online backup (plan section 6)."""

import sqlite3
from pathlib import Path

from .guard import EA_TABLES, GuardError, is_ea_model

SIDECARS = ("-journal", "-wal", "-shm")


def make_copy(source: Path, target: Path) -> None:
    """Copy `source` into the new file `target`; refuse a source that is being written. Removes `target` on failure."""
    if not source.is_file():
        raise GuardError(f"the model to copy does not exist: {source}")
    busy = [s for s in SIDECARS if Path(f"{source}{s}").exists()]
    if busy:
        raise GuardError(f"{source} is open for writing ({', '.join(busy)} present): close the model in "
                         "Enterprise Architect first")
    if not is_ea_model(source):
        raise GuardError(f"not an Enterprise Architect model (no {', '.join(EA_TABLES)}): {source}")
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "xb"):  # exclusive: never overwrite a file that appeared meanwhile
        pass
    try:
        src = sqlite3.connect(f"{source.resolve().as_uri()}?mode=ro", uri=True, timeout=0)
        try:
            dst = sqlite3.connect(target)
            try:
                src.backup(dst)
                ok = dst.execute("PRAGMA integrity_check").fetchone()[0]
            finally:
                dst.close()
        finally:
            src.close()
        if ok != "ok":
            raise GuardError(f"the copy failed SQLite's integrity check ({ok}): {target}")
        if not is_ea_model(target):
            raise GuardError(f"the copy is not an EA model: {target}")
    except sqlite3.OperationalError as exc:
        target.unlink(missing_ok=True)
        raise GuardError(f"{source} could not be read ({exc}): close the model in Enterprise Architect first") from None
    except BaseException:
        target.unlink(missing_ok=True)
        raise
