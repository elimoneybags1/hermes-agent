"""Worktrees, grading by code, landing.

Every subprocess goes through an injectable `runner` (default subprocess.run)
so tests never call the network.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any, Callable

from team.ledger import state_dir


class GradeError(RuntimeError):
    """Raised when a grading/landing operation cannot proceed."""


def worktree_path(target: str, slug: str) -> Path:
    """Return the canonical worktree path for a target/slug."""
    return state_dir() / "worktrees" / target / slug


def worktree_path_for_test(target: str, slug: str, base: Path) -> Path:
    """Return a test worktree path under a custom base directory."""
    return base / "worktrees" / target / slug


Runner = Callable[..., subprocess.CompletedProcess[Any]]


def _default_runner(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[Any]:
    return subprocess.run(cmd, **kwargs)


def create_worktree(
    repo: str | Path,
    target: str,
    slug: str,
    base: str = "main",
    runner: Runner = _default_runner,
    base_path: Path | None = None,
) -> tuple[Path, str]:
    """Create a git worktree for the given target/slug.

    Args:
        repo: Path to the main repository.
        target: Target name.
        slug: Short identifier for this worktree.
        base: Base branch to branch from (default "main").
        runner: Injectable subprocess runner for tests.
        base_path: Optional custom base path for worktrees (tests only).

    Returns:
        Tuple of (worktree path, branch name).

    Raises:
        GradeError: If the branch or path already exists.
    """
    if runner is ...:
        runner = _default_runner

    repo = Path(repo)
    if base_path is not None:
        path = worktree_path_for_test(target, slug, base_path)
    else:
        path = worktree_path(target, slug)
    branch = f"team/{slug}"

    # Check if branch already exists
    result = runner(
        ["git", "-C", str(repo), "rev-parse", "--verify", branch],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        raise GradeError(f"branch {branch!r} already exists")

    # Check if path already exists
    if path.exists():
        raise GradeError(f"path {path!r} already exists")

    path.parent.mkdir(parents=True, exist_ok=True)

    result = runner(
        ["git", "-C", str(repo), "worktree", "add", "-b", branch, str(path), base],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise GradeError(f"git worktree add failed: {result.stderr.strip()}")

    return path, branch


def run_check(
    worktree: str | Path,
    command: str,
    timeout_s: int = 900,
    runner: Runner = _default_runner,
) -> tuple[bool, str]:
    """Run a check command in the worktree.

    Args:
        worktree: Path to the worktree.
        command: Shell command to run.
        timeout_s: Timeout in seconds (default 900).
        runner: Injectable subprocess runner for tests.

    Returns:
        Tuple of (success, output_tail).
        On timeout, returns (False, "timeout after <n>s").
    """
    if runner is ...:
        runner = _default_runner

    worktree = Path(worktree)
    try:
        result = runner(
            ["/bin/sh", "-c", command],
            cwd=worktree,
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
        output = (result.stdout + result.stderr).strip()
        # Return last 60 lines
        lines = output.splitlines()
        tail = "\n".join(lines[-60:]) if lines else ""
        return result.returncode == 0, tail
    except subprocess.TimeoutExpired as e:
        return False, f"timeout after {timeout_s}s"


def has_commits(
    repo: str | Path,
    branch: str,
    base: str = "main",
    runner: Runner = _default_runner,
) -> bool:
    """Check if a branch has commits ahead of base.

    Args:
        repo: Path to the main repository.
        branch: Branch name to check.
        base: Base branch (default "main").
        runner: Injectable subprocess runner for tests.

    Returns:
        True if branch has commits not in base.
    """
    if runner is ...:
        runner = _default_runner

    repo = Path(repo)
    result = runner(
        ["git", "-C", str(repo), "rev-list", "--count", f"{base}..{branch}"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return False
    try:
        count = int(result.stdout.strip())
        return count > 0
    except ValueError:
        return False


def land(
    repo: str | Path,
    branch: str,
    message: str,
    runner: Runner = _default_runner,
) -> str:
    """Land a branch by merging it into main.

    Args:
        repo: Path to the main repository.
        branch: Branch name to merge.
        message: Merge commit message.
        runner: Injectable subprocess runner for tests.

    Returns:
        The merge commit SHA.

    Raises:
        GradeError: If the repo has uncommitted changes, is not on main,
                   or the merge conflicts.
    """
    if runner is ...:
        runner = _default_runner

    repo = Path(repo)

    # Check for uncommitted changes (excluding untracked files)
    result = runner(
        ["git", "-C", str(repo), "status", "--porcelain", "--untracked-files=no"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise GradeError(f"git status failed: {result.stderr.strip()}")
    if result.stdout.strip():
        raise GradeError("repository has uncommitted changes")

    # Check we're on main
    result = runner(
        ["git", "-C", str(repo), "branch", "--show-current"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 or result.stdout.strip() != "main":
        raise GradeError("not on main branch")

    # Merge
    result = runner(
        ["git", "-C", str(repo), "merge", "--no-ff", "-m", message, branch],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        # Try to abort
        runner(
            ["git", "-C", str(repo), "merge", "--abort"],
            capture_output=True,
            text=True,
        )
        raise GradeError(f"merge conflict: {result.stderr.strip()}")

    # Get merge commit SHA
    result = runner(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise GradeError(f"failed to get merge sha: {result.stderr.strip()}")
    return result.stdout.strip()


def remove_worktree(
    repo: str | Path,
    path: str | Path,
    branch: str,
    runner: Runner = _default_runner,
) -> None:
    """Remove a worktree and its branch.

    Args:
        repo: Path to the main repository.
        path: Worktree path.
        branch: Branch name to delete.
        runner: Injectable subprocess runner for tests.
    """
    if runner is ...:
        runner = _default_runner

    repo = Path(repo)
    path = Path(path)

    runner(
        ["git", "worktree", "remove", "--force", str(path)],
        capture_output=True,
        text=True,
    )
    runner(
        ["git", "-C", str(repo), "branch", "-D", branch],
        capture_output=True,
        text=True,
    )