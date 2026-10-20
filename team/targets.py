"""Targets: the repos Team works on.

Team manages every project from outside it (CLAUDE.md, Decision 1). Attaching
a repo writes $TEAM_STATE/targets/<name>/target.json and nothing else; the
target repo is only ever read, through `git rev-parse`.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from team.ledger import state_dir

NAME_RE = re.compile(r"[a-z0-9][a-z0-9._-]*")


class TargetError(Exception):
    """A repo that cannot be attached, with the reason why."""


def targets_dir() -> Path:
    return state_dir() / "targets"


def _git(repo: Path, *args: str) -> str:
    # GIT_OPTIONAL_LOCKS=0 so git never takes a lock or refreshes the index in
    # the target: attaching must leave it untouched.
    env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0"}
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, env=env,
    )
    if result.returncode != 0:
        raise TargetError(result.stderr.strip() or f"git {' '.join(args)} failed")
    return result.stdout.strip()


def _repo_root(path: Path) -> Path:
    if not path.is_dir():
        raise TargetError(f"{path} is not a directory")
    try:
        root = _git(path, "rev-parse", "--show-toplevel")
    except TargetError:
        raise TargetError(f"{path} is not a git repo") from None
    return Path(root).resolve()


def _head(repo: Path) -> str:
    try:
        return _git(repo, "rev-parse", "--verify", "HEAD^{commit}")
    except TargetError:
        raise TargetError(f"{repo} has no commits yet") from None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _write(target: dict) -> None:
    path = targets_dir() / target["name"] / "target.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(target, indent=2) + "\n")
    tmp.replace(path)


def get(name: str) -> dict | None:
    path = targets_dir() / name / "target.json"
    if not path.exists():
        return None
    return json.loads(path.read_text())


def attach(repo_path: Path | str, name: str | None = None) -> dict:
    """Register a git repo as a target and return its record.

    The name defaults to the repo directory name, lowercased. A name already
    attached to a different repo is refused; re-attaching the same repo
    updates its commit and keeps its original attached_at.
    """
    repo = _repo_root(Path(repo_path).expanduser().resolve())
    name = name if name is not None else repo.name.lower()
    if not NAME_RE.fullmatch(name):
        raise TargetError(
            f"bad target name {name!r}: use lowercase letters, digits, '.', '_', '-'"
        )
    commit = _head(repo)

    existing = get(name)
    if existing is not None and existing["path"] != str(repo):
        raise TargetError(f"target {name!r} is already attached to {existing['path']}")

    target = {
        "name": name,
        "path": str(repo),
        "commit": commit,
        "attached_at": existing["attached_at"] if existing else _now(),
    }
    _write(target)
    return target


def list_targets() -> list[dict]:
    """Every attached target, by name."""
    root = targets_dir()
    if not root.is_dir():
        return []
    return [
        json.loads(p.read_text())
        for p in sorted(root.glob("*/target.json"))
    ]
