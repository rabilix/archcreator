"""Central profiles: the profile is chosen by matching the target repository, never guessed."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from archrev.cli import main
from archrev.pipeline import EXIT_USAGE
from archrev.profile import ProfileError, find_profile, normalize_remote

FIXTURE = Path(__file__).parent / "fixtures" / "fx01"
# The fixture profile without its name, so each test can set one.
PROFILE_TEXT = (FIXTURE / "profile.yaml").read_text(encoding="utf-8").replace("name: fx01\n", "")


@pytest.mark.parametrize("url", [
    "git@bitbucket.org:bitbucket-flex/100554_mc_appl.git",
    "https://bitbucket.org/bitbucket-flex/100554_mc_appl.git",
    "https://user@Bitbucket.org/bitbucket-flex/100554_mc_appl/",
    "ssh://git@bitbucket.org:7999/bitbucket-flex/100554_mc_appl.git",
])
def test_remote_forms_normalize_alike(url):
    assert normalize_remote(url) == "bitbucket.org/bitbucket-flex/100554_mc_appl"


def _repo(tmp_path: Path, name: str = "work_copy", remote: str | None = None) -> Path:
    repo = tmp_path / name
    shutil.copytree(FIXTURE, repo)
    (repo / "profile.yaml").unlink()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    if remote:
        subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", remote], check=True)
    return repo


def _central(tmp_path: Path, profiles: dict[str, str]) -> Path:
    d = tmp_path / "central"
    d.mkdir()
    for name, match in profiles.items():
        (d / name).write_text(PROFILE_TEXT.replace("profile_version: 1\n", f"profile_version: 1\n{match}"),
                              encoding="utf-8")
    return d


def _analyze(repo: Path, out: Path, *extra: str) -> int:
    return main(["analyze", "--repo", str(repo), "--module", "alpha", "--out", str(out), "--context", "all", *extra])


def _summary(out: Path) -> dict:
    return json.loads((out / "summary.json").read_text(encoding="utf-8"))


def test_matched_by_git_remote_in_another_url_form(tmp_path):
    repo = _repo(tmp_path, remote="https://git.example.com/team/ecu_app.git")
    central = _central(tmp_path, {
        "ecu.yaml": 'name: "ecu"\nmatch: {git_remotes: ["git@git.example.com:team/ecu_app.git"]}\n',
        "other.yaml": 'name: "other"\nmatch: {repo_names: ["something_else"]}\n',
    })
    assert _analyze(repo, tmp_path / "out", "--profiles-dir", str(central)) == 0
    assert _summary(tmp_path / "out")["profile"] == {"name": "ecu", "origin": "central", "file": "ecu.yaml"}


def test_matched_by_repo_folder_name(tmp_path):
    repo = _repo(tmp_path, name="ECU_App_v2")
    central = _central(tmp_path, {"ecu.yaml": 'name: "ecu"\nmatch: {repo_names: ["ecu_app*"]}\n'})
    assert find_profile(repo, [central]).name == "ecu"


def test_baseline_next_to_central_profiles_is_not_a_profile(tmp_path):
    repo = _repo(tmp_path, name="ecu_app")
    central = _central(tmp_path, {"ecu.yaml": 'name: "ecu"\nmatch: {repo_names: ["ecu_app"]}\nbaseline: ecu.baseline.yaml\n'})
    (central / "ecu.baseline.yaml").write_text("baseline_version: 1\naccepted: []\n", encoding="utf-8")
    assert _analyze(repo, tmp_path / "out", "--profiles-dir", str(central)) == 0
    s = _summary(tmp_path / "out")
    assert s["profile"]["file"] == "ecu.yaml" and s["baseline"]["file"] == "ecu.baseline.yaml"


def test_no_match_uses_defaults_and_says_so(tmp_path):
    repo = _repo(tmp_path)
    central = _central(tmp_path, {"other.yaml": 'name: "other"\nmatch: {repo_names: ["nope"]}\n'})
    _analyze(repo, tmp_path / "out", "--profiles-dir", str(central))
    assert _summary(tmp_path / "out")["profile"] == {"name": "default", "origin": "none", "file": None}


def test_two_matches_are_a_usage_error(tmp_path):
    repo = _repo(tmp_path)
    central = _central(tmp_path, {"a.yaml": 'match: {repo_names: ["work_copy"]}\n',
                                  "b.yaml": 'match: {repo_names: ["work_*"]}\n'})
    assert _analyze(repo, tmp_path / "out", "--profiles-dir", str(central)) == EXIT_USAGE
    assert "a.yaml, b.yaml" in _summary(tmp_path / "out")["error"]


def test_broken_central_profile_is_named(tmp_path):
    repo = _repo(tmp_path)
    central = _central(tmp_path, {"bad.yaml": "match: {repo_name: [x]}\n"})
    with pytest.raises(ProfileError, match="bad.yaml.*repo_name"):
        find_profile(repo, [central])


def test_explicit_profile_wins(tmp_path):
    repo = _repo(tmp_path)
    central = _central(tmp_path, {"ecu.yaml": 'name: "ecu"\nmatch: {repo_names: ["work_copy"]}\n'})
    explicit = tmp_path / "mine.yaml"
    explicit.write_text(PROFILE_TEXT.replace("profile_version: 1\n", 'profile_version: 1\nname: "mine"\n'),
                        encoding="utf-8")
    _analyze(repo, tmp_path / "out", "--profiles-dir", str(central), "--profile", str(explicit))
    assert _summary(tmp_path / "out")["profile"] == {"name": "mine", "origin": "explicit", "file": "mine.yaml"}


def test_env_variable_names_the_central_folder(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    central = _central(tmp_path, {"ecu.yaml": 'name: "ecu"\nmatch: {repo_names: ["work_copy"]}\n'})
    monkeypatch.setenv("ARCHREV_PROFILES", str(central))
    _analyze(repo, tmp_path / "out")
    assert _summary(tmp_path / "out")["profile"]["origin"] == "central"


def test_shipped_100554_profile_matches_its_remote():
    from archrev.profile import load_profile
    p = load_profile(Path(__file__).parents[1] / "profiles" / "100554.yaml")
    assert normalize_remote(p.match_git_remotes[0]) == "bitbucket.org/bitbucket-flex/100554_mc_appl"
