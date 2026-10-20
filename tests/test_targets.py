import json
import os
import subprocess

import pytest

from team import cli, targets

GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
}


def git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True, capture_output=True, text=True, env=GIT_ENV,
    ).stdout.strip()


def commit(repo, msg="c"):
    git(repo, "commit", "--allow-empty", "-q", "-m", msg)
    return git(repo, "rev-parse", "HEAD")


def snapshot(repo):
    """Every path and mtime in the repo, .git included."""
    return {
        str(p.relative_to(repo)): p.stat().st_mtime_ns
        for p in sorted(repo.rglob("*"))
    }


@pytest.fixture
def state(monkeypatch, tmp_path):
    monkeypatch.setenv("TEAM_STATE", str(tmp_path / "state"))
    return tmp_path / "state"


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "MyRepo"
    path.mkdir()
    git(path, "init", "-q")
    commit(path)
    return path


def test_attach_writes_target_json(state, repo):
    target = targets.attach(repo)
    on_disk = json.loads((state / "targets" / "myrepo" / "target.json").read_text())
    assert on_disk == target
    assert target["name"] == "myrepo"
    assert target["path"] == str(repo.resolve())
    assert target["commit"] == git(repo, "rev-parse", "HEAD")
    assert target["attached_at"].endswith("+00:00")


def test_attach_with_name(state, repo):
    assert targets.attach(repo, name="other")["name"] == "other"
    assert (state / "targets" / "other" / "target.json").exists()


def test_attach_from_subdirectory_uses_repo_root(state, repo):
    (repo / "sub").mkdir()
    target = targets.attach(repo / "sub")
    assert target["path"] == str(repo.resolve()) and target["name"] == "myrepo"


def test_attach_refuses_non_repo(state, tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(targets.TargetError, match="not a git repo"):
        targets.attach(plain)
    assert not state.exists()


def test_attach_refuses_missing_path(state, tmp_path):
    with pytest.raises(targets.TargetError, match="not a directory"):
        targets.attach(tmp_path / "nope")


def test_attach_refuses_repo_without_commits(state, tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    git(empty, "init", "-q")
    with pytest.raises(targets.TargetError, match="no commits"):
        targets.attach(empty)


def test_attach_refuses_bad_name(state, repo):
    with pytest.raises(targets.TargetError, match="bad target name"):
        targets.attach(repo, name="../escape")


def test_attach_refuses_name_taken_by_other_path(state, repo, tmp_path):
    targets.attach(repo, name="dup")
    other = tmp_path / "other"
    other.mkdir()
    git(other, "init", "-q")
    commit(other)
    with pytest.raises(targets.TargetError, match="already attached"):
        targets.attach(other, name="dup")
    assert targets.get("dup")["path"] == str(repo.resolve())


def test_reattach_same_path_updates_commit(state, repo):
    first = targets.attach(repo)
    new = commit(repo, "second")
    second = targets.attach(repo)
    assert second["commit"] == new != first["commit"]
    assert second["attached_at"] == first["attached_at"]
    assert targets.get("myrepo") == second


def test_attach_never_writes_into_repo(state, repo):
    before = snapshot(repo)
    targets.attach(repo)
    targets.attach(repo)
    assert snapshot(repo) == before


def test_list_targets(state, repo, tmp_path):
    assert targets.list_targets() == []
    other = tmp_path / "Zed"
    other.mkdir()
    git(other, "init", "-q")
    commit(other)
    targets.attach(other)
    targets.attach(repo)
    assert [t["name"] for t in targets.list_targets()] == ["myrepo", "zed"]


def test_cli_attach_and_targets(state, repo, capsys):
    assert cli.main(["attach", str(repo), "--name", "mine"]) == 0
    assert "attached mine" in capsys.readouterr().out

    assert cli.main(["targets"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].split("\t") == list(cli.TARGET_COLUMNS)
    row = dict(zip(cli.TARGET_COLUMNS, lines[1].split("\t")))
    assert row["name"] == "mine" and row["path"] == str(repo.resolve())


def test_cli_attach_refusal_exits_nonzero(state, tmp_path, capsys):
    assert cli.main(["attach", str(tmp_path)]) == 1
    assert "not a git repo" in capsys.readouterr().err
