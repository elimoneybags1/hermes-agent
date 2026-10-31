"""Grading and landing against real git in a temporary repository."""

import subprocess

import pytest

from team import grade


def git(repo, *args, check=True):
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=check
    )


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    git(r, "config", "user.name", "Test")
    git(r, "config", "user.email", "test@test")
    (r / "README.md").write_text("# Test\n")
    (r / "a.py").write_text("x = 1\n")
    git(r, "add", "-A")
    git(r, "commit", "-q", "-m", "initial")
    return r


def commit_in(wt, path, text, message="change"):
    (wt / path).write_text(text)
    git(wt, "add", "-A")
    git(wt, "commit", "-q", "-m", message)


def test_worktree_path_under_team_state(team_state):
    assert grade.worktree_path("team", "x") == team_state / "worktrees" / "team" / "x"


def test_create_worktree_and_refusals(repo, team_state):
    path, branch = grade.create_worktree(repo, "team", "slug")
    assert branch == "team/slug" and path == team_state / "worktrees" / "team" / "slug"
    assert (path / "README.md").exists()
    assert grade.branch_exists(repo, "team/slug")
    with pytest.raises(grade.GradeError, match="already exists"):
        grade.create_worktree(repo, "team", "slug")
    git(repo, "branch", "develop")
    path2, _ = grade.create_worktree(repo, "team", "other", base="develop")
    assert path2.exists()
    path3 = grade.worktree_path("team", "taken")
    path3.mkdir(parents=True)
    with pytest.raises(grade.GradeError, match="path .* already exists"):
        grade.create_worktree(repo, "team", "taken")


def test_ensure_worktree_readds_from_branch(repo, team_state):
    path, branch = grade.create_worktree(repo, "team", "slug")
    commit_in(path, "a.py", "x = 2\n")
    git(repo, "worktree", "remove", "--force", str(path))
    assert not path.exists()
    assert grade.ensure_worktree(repo, path, branch) is True
    assert (path / "a.py").read_text() == "x = 2\n"
    assert grade.ensure_worktree(repo, path, branch) is False
    with pytest.raises(grade.GradeError, match="does not exist"):
        grade.ensure_worktree(repo, team_state / "nope", "team/nope")


def test_run_check_pass_fail_tail_and_timeout(tmp_path):
    wt = tmp_path / "wt"
    wt.mkdir()
    ok, out = grade.run_check(wt, "echo hello; echo world")
    assert ok is True and out == "hello\nworld"
    ok, out = grade.run_check(wt, "echo oops >&2; exit 3")
    assert ok is False and out == "oops"
    ok, out = grade.run_check(wt, "seq 1 100", tail_lines=3)
    assert ok and out == "98\n99\n100"
    ok, out = grade.run_check(wt, "sleep 5", timeout_s=1)
    assert ok is False and out == "timeout after 1s"


def test_has_commits_and_changed_files(repo, team_state):
    path, branch = grade.create_worktree(repo, "team", "slug")
    assert grade.has_commits(repo, branch) is False
    commit_in(path, "a.py", "x = 2\n")
    (path / "new.txt").write_text("n\n")
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "add")
    assert grade.has_commits(repo, branch) is True
    assert grade.changed_files(repo, branch) == ["a.py", "new.txt"]
    assert grade.has_commits(repo, "team/none") is False


def test_outside_owned():
    changed = ["team/run.py", "team/sub/x.py", "tests/team/test_run.py", "README.md"]
    assert grade.outside_owned(changed, ["team/run.py", "tests/team/"]) == [
        "README.md",
        "team/sub/x.py",
    ]
    assert grade.outside_owned(changed, ["team", "tests", "README.md"]) == []
    assert grade.outside_owned(["teamster.py"], ["team"]) == ["teamster.py"]


def test_merge_base_into_clean_and_conflict(repo, team_state):
    path, _branch = grade.create_worktree(repo, "team", "slug")
    commit_in(path, "a.py", "x = 2\n")
    commit_in(repo, "README.md", "# Test\nmore\n", "main moves")
    merged, conflicts = grade.merge_base_into(path, "main")
    assert merged is True and conflicts == []
    assert (path / "README.md").read_text().endswith("more\n")

    commit_in(repo, "a.py", "x = 3\n", "main edits a.py")
    commit_in(path, "a.py", "x = 4\n", "branch edits a.py")
    merged, conflicts = grade.merge_base_into(path, "main")
    assert merged is False and conflicts == ["a.py"]
    assert grade.is_clean(path)
    assert (path / "a.py").read_text() == "x = 4\n"


def test_land_success_and_refusals(repo, team_state):
    path, branch = grade.create_worktree(repo, "team", "slug")
    commit_in(path, "a.py", "x = 2\n")
    sha = grade.land(repo, branch, "Merge team/slug (goal 1)")
    assert sha == grade.head(repo)
    log = git(repo, "log", "--oneline", "-1").stdout
    assert "Merge team/slug (goal 1)" in log
    assert (repo / "a.py").read_text() == "x = 2\n"
    assert len(git(repo, "rev-list", "--parents", "-1", "HEAD").stdout.split()) == 3

    (repo / "README.md").write_text("dirty\n")
    git(repo, "add", "README.md")
    with pytest.raises(grade.GradeError, match="staged"):
        grade.land(repo, branch, "again")
    git(repo, "reset", "HEAD", "README.md")
    git(repo, "checkout", "-q", "--", "README.md")

    git(repo, "checkout", "-q", "-b", "elsewhere")
    with pytest.raises(grade.GradeError, match="not 'main'"):
        grade.land(repo, branch, "again")
    git(repo, "checkout", "-q", "main")


def test_land_conflict_aborts_cleanly(repo, team_state):
    path, branch = grade.create_worktree(repo, "team", "slug")
    commit_in(path, "a.py", "x = 2\n")
    commit_in(repo, "a.py", "x = 9\n", "main edits a.py")
    with pytest.raises(grade.GradeError, match="conflict"):
        grade.land(repo, branch, "conflict")
    assert grade.is_clean(repo)
    assert (repo / "a.py").read_text() == "x = 9\n"
    assert not (repo / ".git" / "MERGE_HEAD").exists()


def test_land_into_another_base(repo, team_state):
    git(repo, "branch", "team/part-1")
    git(repo, "checkout", "-q", "team/part-1")
    path, branch = grade.create_worktree(repo, "team", "slug", base="team/part-1")
    commit_in(path, "a.py", "x = 2\n")
    grade.land(repo, branch, "into part", base="team/part-1")
    assert (repo / "a.py").read_text() == "x = 2\n"
    git(repo, "checkout", "-q", "main")
    assert (repo / "a.py").read_text() == "x = 1\n"


def test_remove_worktree(repo, team_state):
    path, branch = grade.create_worktree(repo, "team", "slug")
    grade.remove_worktree(repo, path, branch)
    assert not path.exists() and not grade.branch_exists(repo, branch)
    path2, branch2 = grade.create_worktree(repo, "team", "two")
    git(repo, "worktree", "remove", "--force", str(path2))
    grade.remove_worktree(repo, path2, branch2)
    assert not grade.branch_exists(repo, branch2)


def test_commit_paths_only_commits_named_paths(repo):
    (repo / "a.py").write_text("x = 5\n")
    (repo / "README.md").write_text("changed\n")
    git(repo, "add", "README.md")
    sha = grade.commit_paths(repo, "a only", repo / "a.py")
    assert sha == grade.head(repo)
    assert git(repo, "show", "--stat", "--format=", "HEAD").stdout.count("|") == 1
    assert "README.md" in git(repo, "status", "--porcelain").stdout


def test_land_preserves_unrelated_edits_and_refuses_overwriting(repo, team_state):
    path, branch = grade.create_worktree(repo, "team", "shared")
    commit_in(path, "a.py", "x = 2\n")
    (repo / "README.md").write_text("another session\n")
    (repo / "notes.txt").write_text("untracked\n")
    (repo / "a.py").write_text("in progress\n")
    with pytest.raises(grade.GradeError):
        grade.land(repo, branch, "must refuse overlapping edits")
    assert (repo / "a.py").read_text() == "in progress\n"
    git(repo, "restore", "a.py")
    grade.land(repo, branch, "land with unrelated edits")
    assert (repo / "a.py").read_text() == "x = 2\n"
    assert (repo / "README.md").read_text() == "another session\n"
    assert (repo / "notes.txt").read_text() == "untracked\n"
    assert git(repo, "show", "HEAD:README.md").stdout == "# Test\n"


def test_land_waits_while_the_checkout_edits_the_same_files(repo, team_state):
    path, branch = grade.create_worktree(repo, "team", "waits")
    commit_in(path, "a.py", "x = 2\n")
    (repo / "a.py").write_text("in progress\n")
    with pytest.raises(grade.LandLater) as e:
        grade.land(repo, branch, "must wait")
    assert e.value.files == ["a.py"] and "a.py" in str(e.value)
    assert isinstance(e.value, grade.GradeError)
    assert (repo / "a.py").read_text() == "in progress\n"
    git(repo, "restore", "a.py")
    (repo / "README.md").write_text("unrelated edit\n")
    assert grade.blocking_edits(repo, branch) == []
    # an untracked file the branch would create counts too
    commit_in(path, "new.py", "y = 1\n")
    (repo / "new.py").write_text("draft\n")
    assert grade.blocking_edits(repo, branch) == ["new.py"]
    (repo / "new.py").unlink()
    grade.land(repo, branch, "lands once the tree is clean")
    assert (repo / "a.py").read_text() == "x = 2\n"
    assert (repo / "README.md").read_text() == "unrelated edit\n"
