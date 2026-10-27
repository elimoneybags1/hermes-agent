"""Targets: the repos Team works on.

Team manages every project from outside it. Attaching a repo writes
``$TEAM_STATE/targets/<name>/target.json`` and nothing else; the target repo
is only ever read, through ``git rev-parse``. The record holds the repo path,
the commit at attach time, the board name (same as the target), the check
command that decides "done" (None means the orchestration default), and
whether ``team run`` works on it.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from team.ledger import state_dir

NAME_RE = re.compile(r"[a-z0-9][a-z0-9._-]*")

# key: (type, default, help)
FIELDS: dict[str, tuple[type, object, str]] = {
    "check": (
        str,
        None,
        'This target\'s "done" check. Empty uses grading.default_check.',
    ),
    "enabled": (bool, True, "Whether `team run` works on this target."),
}


class TargetError(ValueError):
    """A repo that cannot be attached, or a setting that cannot be set."""


def targets_dir() -> Path:
    return state_dir() / "targets"


def _git(repo: Path, *args: str) -> str:
    # GIT_OPTIONAL_LOCKS=0: never take a lock or refresh the index in the target.
    env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0"}
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        env=env,
        check=False,
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
    return datetime.now(UTC).isoformat(timespec="seconds")


def _path(name: str) -> Path:
    return targets_dir() / name / "target.json"


def _write(target: dict) -> None:
    path = _path(target["name"])
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(target, indent=2) + "\n")
    tmp.replace(path)


def get(name: str) -> dict | None:
    if not NAME_RE.fullmatch(name or ""):
        return None
    path = _path(name)
    if not path.exists():
        return None
    return json.loads(path.read_text())


def require(name: str) -> dict:
    target = get(name)
    if target is None:
        raise TargetError(f"unknown target: {name}")
    return target


def attach(
    repo_path: Path | str, name: str | None = None, check: str | None = None
) -> dict:
    """Register a git repo as a target and return its record.

    The name defaults to the repo directory name, lowercased. A name already
    attached to a different repo is refused; re-attaching the same repo
    refreshes its commit and keeps its original attached_at and settings.
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
        "board": name,
        "check": (existing or {}).get("check"),
        "enabled": (existing or {}).get("enabled", True),
    }
    if check is not None:
        target["check"] = check.strip() or None
    _write(target)
    return target


def list_targets() -> list[dict]:
    """Every attached target, by name."""
    root = targets_dir()
    if not root.is_dir():
        return []
    return [json.loads(p.read_text()) for p in sorted(root.glob("*/target.json"))]


def set_field(name: str, key: str, value: object) -> dict:
    """Change ``check`` or ``enabled`` on a target; returns the record."""
    if key not in FIELDS:
        raise TargetError(f"unknown target setting: {key}")
    target = require(name)
    kind = FIELDS[key][0]
    if key == "check":
        if value is not None and not isinstance(value, str):
            raise TargetError("check must be a string")
        value = (value or "").strip() or None
    elif kind is bool and not isinstance(value, bool):
        raise TargetError(f"{key} must be true or false")
    target[key] = value
    _write(target)
    return target


def repo_path(name: str) -> Path:
    return Path(require(name)["path"])
