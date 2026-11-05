"""ea-apply's guard (plan section 5) and copy (section 6): the design model and every model in the repository are
never written, and the copy is a consistent new file."""

import os
import sqlite3
import stat
from pathlib import Path

import pytest

from archcreator.ea.copy import make_copy
from archcreator.ea.guard import GuardError, check_target, protected_models

from ea_model import make_model


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    make_model(root / "arch" / "design.qea")
    make_model(root / "other" / "old.eapx")
    return root


def _protected(repo, source=None):
    return protected_models(repo, "arch/design.qea", None, source)


def test_protected_models_are_the_design_from_and_every_model_in_the_repository(repo, tmp_path):
    source = make_model(tmp_path / "elsewhere" / "src.qea")
    names = sorted(p.name for p in _protected(repo, source))
    assert names == ["design.qea", "old.eapx", "src.qea"]


def test_refuses_a_copy_inside_the_repository(repo):
    with pytest.raises(GuardError, match="outside the repository"):
        check_target(repo / "copy.qea", repo, _protected(repo), creating=True)


def test_refuses_the_design_model_by_path_and_by_other_case(repo, tmp_path):
    design = repo / "arch" / "design.qea"
    with pytest.raises(GuardError):
        check_target(design, repo, _protected(repo), creating=False)
    if os.path.normcase("A") == os.path.normcase("a"):  # case-insensitive file systems only
        with pytest.raises(GuardError):
            check_target(repo / "arch" / "DESIGN.qea", repo, _protected(repo), creating=False)


@pytest.mark.skipif(os.name == "nt" and not hasattr(os, "symlink"), reason="no symlinks")
def test_refuses_a_link_to_a_protected_model(repo, tmp_path):
    design = repo / "arch" / "design.qea"
    hard = tmp_path / "out" / "hard.qea"
    hard.parent.mkdir()
    os.link(design, hard)
    with pytest.raises(GuardError, match="same file"):
        check_target(hard, repo, _protected(repo), creating=False)
    soft = tmp_path / "out" / "soft.qea"
    try:
        soft.symlink_to(design)
    except OSError:
        pytest.skip("creating symlinks is not allowed here")
    with pytest.raises(GuardError):
        check_target(soft, repo, _protected(repo), creating=False)


def test_with_from_the_copy_must_be_new_and_without_it_must_exist(repo, tmp_path):
    existing = make_model(tmp_path / "out" / "copy.qea")
    with pytest.raises(GuardError, match="already exists"):
        check_target(existing, repo, _protected(repo), creating=True)
    with pytest.raises(GuardError, match="does not exist"):
        check_target(tmp_path / "out" / "new.qea", repo, _protected(repo), creating=False)
    check_target(tmp_path / "out" / "new.qea", repo, _protected(repo), creating=True)
    check_target(existing, repo, _protected(repo), creating=False)


def test_refuses_wrong_suffix_read_only_non_ea_and_output_folder(repo, tmp_path):
    with pytest.raises(GuardError, match=".qea or .qeax"):
        check_target(tmp_path / "copy.db", repo, _protected(repo), creating=True)
    ro = make_model(tmp_path / "out" / "ro.qea")
    ro.chmod(stat.S_IREAD)
    try:
        with pytest.raises(GuardError, match="read-only"):
            check_target(ro, repo, _protected(repo), creating=False)
    finally:
        ro.chmod(stat.S_IREAD | stat.S_IWRITE)
    plain = tmp_path / "out" / "plain.qea"
    sqlite3.connect(plain).execute("CREATE TABLE x (a)").connection.close()
    with pytest.raises(GuardError, match="not an Enterprise Architect model"):
        check_target(plain, repo, _protected(repo), creating=False)
    with pytest.raises(GuardError, match="output folder"):
        check_target(tmp_path / "results" / "c.qea", repo, _protected(repo), creating=True,
                     out_dirs=(tmp_path / "results",))


def _rows(path: Path) -> dict:
    con = sqlite3.connect(path)
    try:
        tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        return {t: con.execute(f"SELECT * FROM {t} ORDER BY 1").fetchall() for t in tables}
    finally:
        con.close()


def test_copy_equals_the_source_table_by_table(repo, tmp_path):
    design = repo / "arch" / "design.qea"
    target = tmp_path / "out" / "copy.qea"
    make_copy(design, target)
    assert _rows(target) == _rows(design)


def test_copy_refused_while_the_source_is_being_written(repo, tmp_path):
    design = repo / "arch" / "design.qea"
    Path(f"{design}-journal").write_bytes(b"")
    with pytest.raises(GuardError, match="open for writing"):
        make_copy(design, tmp_path / "out" / "a.qea")
    Path(f"{design}-journal").unlink()
    holder = sqlite3.connect(design, isolation_level=None)
    holder.execute("BEGIN EXCLUSIVE")
    try:
        with pytest.raises(GuardError, match="locked|close the model"):
            make_copy(design, tmp_path / "out" / "b.qea")
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    assert not (tmp_path / "out" / "a.qea").exists() and not (tmp_path / "out" / "b.qea").exists()


def test_copy_never_overwrites(repo, tmp_path):
    target = make_model(tmp_path / "out" / "copy.qea")
    before = target.read_bytes()
    with pytest.raises(FileExistsError):
        make_copy(repo / "arch" / "design.qea", target)
    assert target.read_bytes() == before
