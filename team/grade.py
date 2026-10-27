"""Worktrees, grading by code, landing.

"Done" is decided here, never by a model: the target's check runs in the
worktree against current ``base`` and its exit code is the verdict. Landing is
a ``--no-ff`` merge that refuses staged changes, a wrong checked-out branch, or
a conflict.

Every subprocess goes through an injectable ``runner`` (default
``subprocess.run``) so tests can fake git; the tests here use real git in a
temporary repository instead, because the fakes hid real bugs before.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from team.ledger import state_dir

Runner = Callable[..., subprocess.CompletedProcess[Any]]


class GradeError(RuntimeError):
    """A grading or landing step that cannot proceed, with the reason."""


def _run(
    cmd: list[str], runner: Runner, **kwargs: Any
) -> subprocess.CompletedProcess[Any]:
    kwargs.setdefault("capture_output", True)
    kwargs.setdefault("text", True)
    return runner(cmd, **kwargs)


def _git(
    repo: Path, *args: str, runner: Runner = subprocess.run
) -> subprocess.CompletedProcess[Any]:
    return _run(["git", "-C", str(repo), *args], runner)


def _must(result: subprocess.CompletedProcess[Any], what: str) -> str:
    if result.returncode != 0:
        raise GradeError(f"{what}: {(result.stderr or result.stdout).strip()}")
    return (result.stdout or "").strip()


def worktree_path(target: str, slug: str) -> Path:
    """Where a goal's worktree lives: ``$TEAM_STATE/worktrees/<target>/<slug>``."""
    return state_dir() / "worktrees" / target / slug


def branch_exists(repo: Path, branch: str, runner: Runner = subprocess.run) -> bool:
    return (
        _git(
            repo,
            "rev-parse",
            "--verify",
            "--quiet",
            f"refs/heads/{branch}",
            runner=runner,
        ).returncode
        == 0
    )


def create_worktree(
    repo: str | Path,
    target: str,
    slug: str,
    base: str = "main",
    runner: Runner = subprocess.run,
) -> tuple[Path, str]:
    """Create branch ``team/<slug>`` from ``base`` in a new worktree.

    Returns (path, branch). Refuses when the branch or the path already exists.
    """
    repo = Path(repo)
    path = worktree_path(target, slug)
    branch = f"team/{slug}"
    if branch_exists(repo, branch, runner):
        raise GradeError(f"branch {branch!r} already exists")
    if path.exists():
        raise GradeError(f"path {path} already exists")
    path.parent.mkdir(parents=True, exist_ok=True)
    _must(
        _git(repo, "worktree", "add", "-b", branch, str(path), base, runner=runner),
        "git worktree add failed",
    )
    return path, branch


def ensure_worktree(
    repo: str | Path, path: str | Path, branch: str, runner: Runner = subprocess.run
) -> bool:
    """Re-create a worktree for an existing branch when it is gone.

    Kanban removes a clean worktree when its task completes; grading and
    retries work from the branch, so the worktree is re-added when missing.
    Returns True when it had to be re-added.
    """
    repo, path = Path(repo), Path(path)
    if path.is_dir() and (path / ".git").exists():
        return False
    if not branch_exists(repo, branch, runner):
        raise GradeError(f"branch {branch!r} does not exist")
    _git(repo, "worktree", "prune", runner=runner)
    path.parent.mkdir(parents=True, exist_ok=True)
    _must(
        _git(repo, "worktree", "add", str(path), branch, runner=runner),
        "git worktree add failed",
    )
    return True


def run_check(
    worktree: str | Path,
    command: str,
    timeout_s: int = 900,
    runner: Runner = subprocess.run,
    tail_lines: int = 60,
) -> tuple[bool, str]:
    """Run the check command in the worktree: (exit code == 0, last lines of output).

    A timeout is ``(False, "timeout after <n>s")``.
    """
    try:
        result = _run(
            ["/bin/sh", "-c", command], runner, cwd=str(worktree), timeout=timeout_s
        )
    except subprocess.TimeoutExpired:
        return False, f"timeout after {timeout_s}s"
    output = ((result.stdout or "") + (result.stderr or "")).strip()
    lines = output.splitlines()
    return result.returncode == 0, "\n".join(lines[-tail_lines:])


def has_commits(
    repo: str | Path, branch: str, base: str = "main", runner: Runner = subprocess.run
) -> bool:
    """True when ``branch`` has at least one commit that ``base`` does not."""
    result = _git(Path(repo), "rev-list", "--count", f"{base}..{branch}", runner=runner)
    if result.returncode != 0:
        return False
    try:
        return int(result.stdout.strip()) > 0
    except ValueError:
        return False


def is_clean(worktree: str | Path, runner: Runner = subprocess.run) -> bool:
    """No modified or staged tracked files (untracked files are allowed)."""
    return (
        _must(
            _git(
                Path(worktree),
                "status",
                "--porcelain",
                "--untracked-files=no",
                runner=runner,
            ),
            "git status failed",
        )
        == ""
    )


def merge_base_into(
    worktree: str | Path, base: str = "main", runner: Runner = subprocess.run
) -> tuple[bool, list[str]]:
    """Merge ``base`` into the worktree's branch so the check runs against current base.

    Returns (merged, conflicted files). A conflict is aborted and reported;
    nothing is left half-merged.
    """
    worktree = Path(worktree)
    result = _git(worktree, "merge", "--no-edit", base, runner=runner)
    if result.returncode == 0:
        return True, []
    conflicted = _git(
        worktree, "diff", "--name-only", "--diff-filter=U", runner=runner
    ).stdout.split()
    _git(worktree, "merge", "--abort", runner=runner)
    return False, sorted(conflicted)


def changed_files(
    repo: str | Path, branch: str, base: str = "main", runner: Runner = subprocess.run
) -> list[str]:
    """Paths the branch changes relative to base, both sides of a rename included."""
    out = _must(
        _git(
            Path(repo),
            "diff",
            "--name-only",
            "--no-renames",
            f"{base}...{branch}",
            runner=runner,
        ),
        "git diff failed",
    )
    return sorted(p for p in out.splitlines() if p)


def outside_owned(changed: list[str], owned: list[str]) -> list[str]:
    """Changed paths that no owned path covers (exact file or directory prefix)."""

    def covered(path: str) -> bool:
        for o in owned:
            o = o.rstrip("/")
            if path == o or path.startswith(o + "/"):
                return True
        return False

    return sorted(p for p in changed if not covered(p))


def land(
    repo: str | Path,
    branch: str,
    message: str,
    base: str = "main",
    runner: Runner = subprocess.run,
) -> str:
    """Merge ``branch`` into ``base`` with ``--no-ff``; returns the merge sha.

    Refuses staged changes (which could enter the merge commit), when ``base`` is
    not the checked-out branch, or when the merge conflicts (aborted cleanly).
    """
    repo = Path(repo)
    if _must(
        _git(repo, "diff", "--cached", "--name-only", runner=runner), "git diff failed"
    ):
        raise GradeError("repository has staged changes")
    current = _must(
        _git(repo, "branch", "--show-current", runner=runner), "git branch failed"
    )
    if current != base:
        raise GradeError(f"checked-out branch is {current!r}, not {base!r}")
    result = _git(
        repo,
        "-c",
        "merge.autostash=false",
        "merge",
        "--no-ff",
        "-m",
        message,
        branch,
        runner=runner,
    )
    if result.returncode != 0:
        _git(repo, "merge", "--abort", runner=runner)
        raise GradeError(f"merge conflict: {(result.stderr or result.stdout).strip()}")
    return _must(_git(repo, "rev-parse", "HEAD", runner=runner), "git rev-parse failed")


def remove_worktree(
    repo: str | Path, path: str | Path, branch: str, runner: Runner = subprocess.run
) -> None:
    """Remove a worktree and delete its branch; a missing worktree is fine."""
    repo = Path(repo)
    if Path(path).exists():
        _must(
            _git(repo, "worktree", "remove", "--force", str(path), runner=runner),
            "git worktree remove failed",
        )
    else:
        _git(repo, "worktree", "prune", runner=runner)
    _git(repo, "branch", "-D", branch, runner=runner)


def head(repo: str | Path, runner: Runner = subprocess.run) -> str:
    """The repo's HEAD commit sha."""
    return _must(
        _git(Path(repo), "rev-parse", "HEAD", runner=runner), "git rev-parse failed"
    )


def commit_paths(
    repo: str | Path, message: str, *paths: str | Path, runner: Runner = subprocess.run
) -> str:
    """Stage exactly these paths and commit them; returns the new sha.

    Nothing else that happens to be staged is swept in.
    """
    repo = Path(repo)
    rel = [
        str(Path(p).relative_to(repo)) if Path(p).is_absolute() else str(p)
        for p in paths
    ]
    _must(_git(repo, "add", "--", *rel, runner=runner), "git add failed")
    _must(
        _git(repo, "commit", "-q", "-m", message, "--", *rel, runner=runner),
        "git commit failed",
    )
    return head(repo, runner)
