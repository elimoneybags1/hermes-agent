"""Tests for team/grade.py."""

import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

from team import grade


class MockRunner:
    """Mock runner that records calls and returns canned results."""

    def __init__(self, responses: dict | None = None):
        self.calls = []
        self.responses = responses or {}

    def __call__(self, cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
        self.calls.append({"cmd": cmd, "kwargs": kwargs})
        # Match by command tuple
        key = tuple(cmd)
        if key in self.responses:
            return self.responses[key]
        # Default success
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")


class TestWorktreePath:
    def test_worktree_path_format(self, monkeypatch, tmp_path):
        monkeypatch.setenv("TEAM_STATE", str(tmp_path / "state"))
        path = grade.worktree_path("my-target", "my-slug")
        assert path == tmp_path / "state" / "worktrees" / "my-target" / "my-slug"

    def test_worktree_path_default(self, monkeypatch, tmp_path):
        monkeypatch.delenv("TEAM_STATE", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))
        path = grade.worktree_path("my-target", "my-slug")
        assert path == tmp_path / "state" / "team" / "worktrees" / "my-target" / "my-slug"


class TestCreateWorktree:
    def _init_repo(self, repo: Path):
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "test@test"], cwd=repo, check=True)
        (repo / "README.md").write_text("# Test\n")
        subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=repo, check=True)

    def test_create_worktree_success(self, monkeypatch, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        self._init_repo(repo)

        # Set TEAM_STATE so worktree_path lands in tmp_path
        monkeypatch.setenv("TEAM_STATE", str(tmp_path / "state"))

        # Mock runner that says branch doesn't exist (returncode=128 for rev-parse --verify)
        runner = MockRunner({
            ("git", "-C", str(repo), "rev-parse", "--verify", "team/slug"):
                subprocess.CompletedProcess([], 128, stdout="", stderr=""),
            ("git", "-C", str(repo), "worktree", "add", "-b", "team/slug",
             str(grade.worktree_path("target", "slug")), "main"):
                subprocess.CompletedProcess([], 0, stdout="", stderr=""),
        })

        path, branch = grade.create_worktree(repo, "target", "slug", runner=runner)

        assert branch == "team/slug"
        assert path == grade.worktree_path("target", "slug")

    def test_create_worktree_real_git(self, monkeypatch, tmp_path):
        """Integration test with real git."""
        repo = tmp_path / "repo"
        repo.mkdir()
        self._init_repo(repo)

        # Set TEAM_STATE so worktree_path lands in tmp_path
        monkeypatch.setenv("TEAM_STATE", str(tmp_path / "state"))

        path, branch = grade.create_worktree(repo, "target", "slug")

        assert branch == "team/slug"
        assert path.exists()
        assert (path / "README.md").exists()

        # Verify branch exists
        result = subprocess.run(
            ["git", "branch", "--list", branch], cwd=repo, capture_output=True, text=True
        )
        assert branch in result.stdout

    def test_create_worktree_branch_exists_raises(self, monkeypatch, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        self._init_repo(repo)

        monkeypatch.setenv("TEAM_STATE", str(tmp_path / "state"))

        # Create the branch first
        subprocess.run(["git", "branch", "team/slug"], cwd=repo, check=True)

        with pytest.raises(grade.GradeError, match="already exists"):
            grade.create_worktree(repo, "target", "slug")

    def test_create_worktree_path_exists_raises(self, monkeypatch, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        self._init_repo(repo)

        monkeypatch.setenv("TEAM_STATE", str(tmp_path / "state"))

        # Create the path using the correct worktree_path
        path = grade.worktree_path("target", "slug")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.mkdir()

        with pytest.raises(grade.GradeError, match="already exists"):
            grade.create_worktree(repo, "target", "slug")

    def test_create_worktree_custom_base(self, monkeypatch, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        self._init_repo(repo)

        monkeypatch.setenv("TEAM_STATE", str(tmp_path / "state"))

        # Create a develop branch
        subprocess.run(["git", "branch", "develop"], cwd=repo, check=True)

        path, branch = grade.create_worktree(repo, "target", "slug", base="develop")

        assert branch == "team/slug"
        assert path.exists()

    def test_create_worktree_uses_injected_runner(self, monkeypatch, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        self._init_repo(repo)

        monkeypatch.setenv("TEAM_STATE", str(tmp_path / "state"))

        runner = MockRunner({
            ("git", "-C", str(repo), "rev-parse", "--verify", "team/slug"):
                subprocess.CompletedProcess([], 128, stdout="", stderr=""),
            ("git", "-C", str(repo), "worktree", "add", "-b", "team/slug",
             str(grade.worktree_path("target", "slug")), "main"):
                subprocess.CompletedProcess([], 0, stdout="", stderr=""),
        })
        grade.create_worktree(repo, "target", "slug", runner=runner)

        assert len(runner.calls) >= 2  # rev-parse + worktree add
        assert ["git", "-C", str(repo), "rev-parse", "--verify", "team/slug"] == runner.calls[0]["cmd"]


class TestRunCheck:
    def test_run_check_success(self, tmp_path):
        wt = tmp_path / "wt"
        wt.mkdir()
        (wt / "pass.sh").write_text("echo hello\n")
        (wt / "pass.sh").chmod(0o755)

        ok, out = grade.run_check(wt, "./pass.sh", runner=MockRunner())

        assert ok is True

    def test_run_check_failure(self, tmp_path):
        wt = tmp_path / "wt"
        wt.mkdir()
        (wt / "fail.sh").write_text("exit 1\n")
        (wt / "fail.sh").chmod(0o755)

        def fail_runner(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="error")

        ok, out = grade.run_check(wt, "./fail.sh", runner=fail_runner)

        assert ok is False

    def test_run_check_timeout(self, tmp_path):
        wt = tmp_path / "wt"
        wt.mkdir()

        def timeout_runner(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd, 1)

        ok, out = grade.run_check(wt, "sleep 10", timeout_s=1, runner=timeout_runner)

        assert ok is False
        assert out == "timeout after 1s"

    def test_run_check_captures_output_tail(self, tmp_path):
        wt = tmp_path / "wt"
        wt.mkdir()

        def output_runner(cmd, **kwargs):
            lines = "\n".join(f"line {i}" for i in range(70))
            return subprocess.CompletedProcess(cmd, 0, stdout=lines, stderr="")

        ok, out = grade.run_check(wt, "cmd", runner=output_runner)

        assert ok is True
        tail_lines = out.splitlines()
        assert len(tail_lines) == 60
        assert tail_lines[0] == "line 10"
        assert tail_lines[-1] == "line 69"

    def test_run_check_stderr_included(self, tmp_path):
        wt = tmp_path / "wt"
        wt.mkdir()

        def mixed_runner(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, 0, stdout="out", stderr="err")

        ok, out = grade.run_check(wt, "cmd", runner=mixed_runner)

        assert ok is True
        assert "out" in out
        assert "err" in out


class TestHasCommits:
    def _init_repo(self, repo: Path):
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "test@test"], cwd=repo, check=True)
        (repo / "README.md").write_text("# Test\n")
        subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=repo, check=True)

    def test_has_commits_true(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        self._init_repo(repo)

        # Create a branch with commits
        subprocess.run(["git", "checkout", "-b", "feature"], cwd=repo, check=True)
        (repo / "new.txt").write_text("new\n")
        subprocess.run(["git", "add", "new.txt"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "add new"], cwd=repo, check=True)
        subprocess.run(["git", "checkout", "main"], cwd=repo, check=True)

        assert grade.has_commits(repo, "feature") is True

    def test_has_commits_false(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        self._init_repo(repo)

        # Branch with no new commits
        subprocess.run(["git", "branch", "feature"], cwd=repo, check=True)

        assert grade.has_commits(repo, "feature") is False

    def test_has_commits_nonexistent_branch(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        self._init_repo(repo)

        assert grade.has_commits(repo, "nonexistent") is False

    def test_has_commits_custom_base(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        self._init_repo(repo)

        # Create develop branch with a commit
        subprocess.run(["git", "checkout", "-b", "develop"], cwd=repo, check=True)
        (repo / "develop.txt").write_text("dev\n")
        subprocess.run(["git", "add", "develop.txt"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "develop commit"], cwd=repo, check=True)
        subprocess.run(["git", "checkout", "main"], cwd=repo, check=True)

        # Feature based on develop
        subprocess.run(["git", "checkout", "-b", "feature", "develop"], cwd=repo, check=True)
        (repo / "feature.txt").write_text("feat\n")
        subprocess.run(["git", "add", "feature.txt"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "feature commit"], cwd=repo, check=True)
        subprocess.run(["git", "checkout", "main"], cwd=repo, check=True)

        # Should be true when base is develop (feature is 1 ahead of develop)
        assert grade.has_commits(repo, "feature", base="develop") is True
        # And true when base is main (feature is 2 ahead of main)
        assert grade.has_commits(repo, "feature", base="main") is True


class TestLand:
    def _init_repo(self, repo: Path):
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "test@test"], cwd=repo, check=True)
        (repo / "README.md").write_text("# Test\n")
        subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=repo, check=True)

    def test_land_success(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        self._init_repo(repo)

        # Create feature branch
        subprocess.run(["git", "checkout", "-b", "feature"], cwd=repo, check=True)
        (repo / "feature.txt").write_text("feature\n")
        subprocess.run(["git", "add", "feature.txt"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "add feature"], cwd=repo, check=True)
        subprocess.run(["git", "checkout", "main"], cwd=repo, check=True)

        sha = grade.land(repo, "feature", "Merge feature")

        assert len(sha) == 40  # Full SHA
        # Verify merge commit exists
        result = subprocess.run(
            ["git", "log", "--oneline", "-1"], cwd=repo, capture_output=True, text=True
        )
        assert "Merge feature" in result.stdout

    def test_land_refuses_dirty_tree(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        self._init_repo(repo)

        # Create feature branch
        subprocess.run(["git", "checkout", "-b", "feature"], cwd=repo, check=True)
        (repo / "feature.txt").write_text("feature\n")
        subprocess.run(["git", "add", "feature.txt"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "add feature"], cwd=repo, check=True)
        subprocess.run(["git", "checkout", "main"], cwd=repo, check=True)

        # Make main dirty
        (repo / "dirty.txt").write_text("dirty\n")
        subprocess.run(["git", "add", "dirty.txt"], cwd=repo, check=True)

        with pytest.raises(grade.GradeError, match="uncommitted changes"):
            grade.land(repo, "feature", "Merge feature")

    def test_land_refuses_not_on_main(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        self._init_repo(repo)

        # Create feature branch
        subprocess.run(["git", "checkout", "-b", "feature"], cwd=repo, check=True)
        (repo / "feature.txt").write_text("feature\n")
        subprocess.run(["git", "add", "feature.txt"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "add feature"], cwd=repo, check=True)
        # Stay on feature branch

        with pytest.raises(grade.GradeError, match="not on main"):
            grade.land(repo, "feature", "Merge feature")

    def test_land_conflict_aborts(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        self._init_repo(repo)

        # Create feature branch that conflicts with main
        subprocess.run(["git", "checkout", "-b", "feature"], cwd=repo, check=True)
        (repo / "README.md").write_text("# Changed\n")
        subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "change readme"], cwd=repo, check=True)
        subprocess.run(["git", "checkout", "main"], cwd=repo, check=True)

        # Change main too
        (repo / "README.md").write_text("# Also changed\n")
        subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "change readme on main"], cwd=repo, check=True)

        with pytest.raises(grade.GradeError, match="conflict"):
            grade.land(repo, "feature", "Merge feature")

        # Verify merge was aborted (should be back on main)
        result = subprocess.run(
            ["git", "branch", "--show-current"], cwd=repo, capture_output=True, text=True
        )
        assert result.stdout.strip() == "main"

    def test_land_uses_injected_runner(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        self._init_repo(repo)

        subprocess.run(["git", "checkout", "-b", "feature"], cwd=repo, check=True)
        (repo / "feature.txt").write_text("feature\n")
        subprocess.run(["git", "add", "feature.txt"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "add feature"], cwd=repo, check=True)
        subprocess.run(["git", "checkout", "main"], cwd=repo, check=True)

        runner = MockRunner({
            ("git", "-C", str(repo), "status", "--porcelain", "--untracked-files=no"):
                subprocess.CompletedProcess([], 0, stdout="", stderr=""),
            ("git", "-C", str(repo), "branch", "--show-current"):
                subprocess.CompletedProcess([], 0, stdout="main\n", stderr=""),
            ("git", "-C", str(repo), "merge", "--no-ff", "-m", "Merge feature", "feature"):
                subprocess.CompletedProcess([], 0, stdout="", stderr=""),
            ("git", "-C", str(repo), "rev-parse", "HEAD"):
                subprocess.CompletedProcess([], 0, stdout="abc123\n", stderr=""),
        })
        grade.land(repo, "feature", "Merge feature", runner=runner)

        # Should have called status, branch --show-current, merge, rev-parse
        assert len(runner.calls) >= 4
        assert ["git", "-C", str(repo), "status", "--porcelain", "--untracked-files=no"] == runner.calls[0]["cmd"]
        assert ["git", "-C", str(repo), "branch", "--show-current"] == runner.calls[1]["cmd"]


class TestRemoveWorktree:
    def _init_repo(self, repo: Path):
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "test@test"], cwd=repo, check=True)
        (repo / "README.md").write_text("# Test\n")
        subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=repo, check=True)

    def test_remove_worktree_mocked(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        self._init_repo(repo)

        path = tmp_path / "wt"
        # Create a branch to use for the worktree
        subprocess.run(["git", "branch", "worktree-branch"], cwd=repo, check=True)
        subprocess.run(["git", "worktree", "add", str(path), "worktree-branch"], cwd=repo, check=True)
        subprocess.run(["git", "branch", "team/slug"], cwd=repo, check=True)

        runner = MockRunner()
        grade.remove_worktree(repo, path, "team/slug", runner=runner)

        assert len(runner.calls) == 2
        assert runner.calls[0]["cmd"] == ["git", "-C", str(repo), "worktree", "remove", "--force", str(path)]
        assert runner.calls[1]["cmd"] == ["git", "-C", str(repo), "branch", "-D", "team/slug"]

    def test_remove_worktree_real_git(self, monkeypatch, tmp_path):
        """Integration test with real git - verifies worktree and branch removal."""
        repo = tmp_path / "repo"
        repo.mkdir()
        self._init_repo(repo)

        monkeypatch.setenv("TEAM_STATE", str(tmp_path / "state"))

        # Create worktree via create_worktree
        path, branch = grade.create_worktree(repo, "target", "slug")
        assert path.exists()
        
        # Verify branch exists
        result = subprocess.run(
            ["git", "branch", "--list", branch], cwd=repo, capture_output=True, text=True
        )
        assert branch in result.stdout

        # Now remove it
        grade.remove_worktree(repo, path, branch)

        # Path should be gone
        assert not path.exists()
        # Branch should be gone
        result = subprocess.run(
            ["git", "branch", "--list", branch], cwd=repo, capture_output=True, text=True
        )
        assert branch not in result.stdout

    def test_remove_worktree_raises_on_failure(self, monkeypatch, tmp_path):
        """Test that remove_worktree raises GradeError when worktree removal fails."""
        repo = tmp_path / "repo"
        repo.mkdir()
        self._init_repo(repo)

        monkeypatch.setenv("TEAM_STATE", str(tmp_path / "state"))

        # Try to remove a non-existent worktree
        path = tmp_path / "nonexistent"
        
        with pytest.raises(grade.GradeError, match="git worktree remove failed"):
            grade.remove_worktree(repo, path, "team/slug")