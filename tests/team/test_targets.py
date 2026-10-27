import json
import subprocess

import pytest

from team import targets


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "MyRepo"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    git(r, "config", "user.name", "T")
    git(r, "config", "user.email", "t@t")
    (r / "f").write_text("x")
    git(r, "add", "f")
    git(r, "commit", "-q", "-m", "one")
    return r


def test_attach_writes_only_the_state_record(repo, team_state):
    before = sorted(p.name for p in repo.iterdir())
    target = targets.attach(repo)
    assert target["name"] == "myrepo" and target["path"] == str(repo.resolve())
    assert len(target["commit"]) == 40 and target["board"] == "myrepo"
    assert target["check"] is None and target["enabled"] is True
    path = team_state / "targets" / "myrepo" / "target.json"
    assert json.loads(path.read_text()) == target
    assert sorted(p.name for p in repo.iterdir()) == before
    assert targets.list_targets() == [target]
    assert targets.repo_path("myrepo") == repo.resolve()


def test_attach_from_subdirectory_with_name_and_check(repo, team_state):
    sub = repo / "sub"
    sub.mkdir()
    target = targets.attach(sub, name="team", check="pytest -q")
    assert target["path"] == str(repo.resolve()) and target["check"] == "pytest -q"
    again = targets.attach(repo, name="team")
    assert (
        again["attached_at"] == target["attached_at"] and again["check"] == "pytest -q"
    )


def test_attach_refusals(repo, tmp_path, team_state):
    with pytest.raises(targets.TargetError, match="not a directory"):
        targets.attach(tmp_path / "nope")
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(targets.TargetError, match="not a git repo"):
        targets.attach(plain)
    empty = tmp_path / "empty"
    empty.mkdir()
    git(empty, "init", "-q")
    with pytest.raises(targets.TargetError, match="no commits"):
        targets.attach(empty)
    with pytest.raises(targets.TargetError, match="bad target name"):
        targets.attach(repo, name="Bad Name")
    targets.attach(repo, name="one")
    other = tmp_path / "other"
    other.mkdir()
    git(other, "init", "-q", "-b", "main")
    git(other, "config", "user.name", "T")
    git(other, "config", "user.email", "t@t")
    (other / "g").write_text("y")
    git(other, "add", "g")
    git(other, "commit", "-q", "-m", "one")
    with pytest.raises(targets.TargetError, match="already attached"):
        targets.attach(other, name="one")


def test_set_field_and_require(repo, team_state):
    targets.attach(repo, name="team")
    assert (
        targets.set_field("team", "check", "  pytest tests/team -q ")["check"]
        == "pytest tests/team -q"
    )
    assert targets.set_field("team", "check", "")["check"] is None
    assert targets.set_field("team", "enabled", False)["enabled"] is False
    assert targets.get("team")["enabled"] is False
    with pytest.raises(targets.TargetError, match="unknown target setting"):
        targets.set_field("team", "colour", "blue")
    with pytest.raises(targets.TargetError, match="true or false"):
        targets.set_field("team", "enabled", "yes")
    with pytest.raises(targets.TargetError, match="unknown target"):
        targets.require("nope")
    assert targets.get("../etc") is None
    assert targets.list_targets()[0]["name"] == "team"


def test_list_targets_empty_without_state(team_state):
    assert targets.list_targets() == []
