"""Orchestration settings: how `team run` behaves, editable from `team ui`.

Two places, by kind:

- `orchestration.toml` in this repo: the rules of the loop, the same for every
  target. Each change is a git commit, like an agent change.
- `target.json` in `$TEAM_STATE/targets/<name>/`: per-target settings
  (`check`, `enabled`). State, not code, so no commit (CLAUDE.md, Decision 1).

Retries before escalation are per role: `escalation.after_failures` in
`roles/<name>/role.toml`.
"""

from __future__ import annotations

import json
import os
import tomllib
from pathlib import Path
from typing import Any

import tomli_w

from team import targets
from team.agents import AgentError, _git_commit

FILE = "orchestration.toml"

# key: (type, default, help)
FIELDS: dict[str, tuple[type, Any, str]] = {
    "loop.interval_s": (int, 60, "Seconds between ticks of `team run`."),
    "loop.max_new_per_tick": (int, 3, "Most new kanban tasks Team creates in one tick."),
    "grading.default_check": (str, "uv run pytest -q", "The check that decides \"done\" when a target has none of its own."),
    "grading.check_timeout_s": (int, 900, "A check running longer than this fails."),
    "escalation.claude": (bool, True, "After a role's free attempts fail, try once on Claude Code (subscription)."),
    "escalation.claude_max_turns": (int, 60, "Turn limit for a Claude Code attempt."),
    "escalation.claude_timeout_s": (int, 1800, "Time limit for a Claude Code attempt."),
    "ask.block_after_claude_fails": (bool, True, "When the Claude attempt fails too, block the task and ask Eli."),
    "models.claude_cost": (float, 0.3, "How much pass-probability a Claude attempt must beat a free one by."),
    "models.minute_weight": (float, 0.005, "Penalty per median minute of a model's attempts."),
    "models.explore": (bool, True, "Off = always take the best mean, no Thompson sampling."),
}

TARGET_FIELDS: dict[str, tuple[type, Any, str]] = {
    "check": (str, None, "This target's \"done\" check. Empty uses grading.default_check."),
    "enabled": (bool, True, "Whether `team run` works on this target."),
}


def _read(root: Path) -> dict:
    path = root / FILE
    if not path.exists():
        return {}
    with path.open("rb") as f:
        return tomllib.load(f)


def _coerce(key: str, kind: type, value: Any) -> Any:
    """Validate one value; AgentError (a ValueError) on a bad one."""
    if kind is bool:
        if not isinstance(value, bool):
            raise AgentError(f"{key} must be true or false")
    elif kind is int:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise AgentError(f"{key} must be a whole number above 0")
    elif kind is float:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
            raise AgentError(f"{key} must be a number above 0")
        value = float(value)
    elif kind is str:
        if not isinstance(value, str) or not value.strip():
            raise AgentError(f"{key} must be a non-empty string")
        value = value.strip()
    return value


def load(root: Path) -> dict[str, Any]:
    """Every setting as a flat {"section.key": value}, defaults filled in."""
    data = _read(root)
    out = {}
    for key, (_, default, _) in FIELDS.items():
        section, name = key.split(".", 1)
        out[key] = data.get(section, {}).get(name, default)
    return out


def fields() -> list[dict[str, Any]]:
    """The settings the page can edit, with their types and help text."""
    return [
        {"key": k, "type": t.__name__, "default": d, "help": h}
        for k, (t, d, h) in FIELDS.items()
    ]


def set_value(root: Path, key: str, value: Any, source: str = "dashboard") -> str:
    """Change one setting in orchestration.toml and commit it; returns the sha."""
    if key not in FIELDS:
        raise AgentError(f"unknown setting: {key}")
    kind = FIELDS[key][0]
    value = _coerce(key, kind, value)
    data = _read(root)
    section, name = key.split(".", 1)
    data.setdefault(section, {})[name] = value
    path = root / FILE
    with path.open("wb") as f:
        tomli_w.dump(data, f)
    return _git_commit(root, f"settings: {key} = {value!r} ({source})", path)


def _target(name: str) -> dict:
    if not targets.NAME_RE.fullmatch(name) or (target := targets.get(name)) is None:
        raise AgentError(f"unknown target: {name}")
    return target


def target_settings(name: str) -> dict[str, Any]:
    """A target's settings with defaults, plus its name and path."""
    target = _target(name)
    out = {"name": target["name"], "path": target["path"]}
    for key, (_, default, _) in TARGET_FIELDS.items():
        out[key] = target.get(key, default)
    return out


def set_target(name: str, key: str, value: Any) -> dict[str, Any]:
    """Change one target setting in target.json; returns the target's settings."""
    if key not in TARGET_FIELDS:
        raise AgentError(f"unknown target setting: {key}")
    kind = TARGET_FIELDS[key][0]
    if key == "check" and (value is None or value == ""):
        value = None
    else:
        value = _coerce(key, kind, value)
    target = _target(name)
    if value is None:
        target.pop(key, None)
    else:
        target[key] = value
    path = targets.targets_dir() / name / "target.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(target, indent=2) + "\n")
    os.replace(tmp, path)
    return target_settings(name)


def check_for(root: Path, name: str) -> str:
    """The check command that decides "done" for this target."""
    return target_settings(name)["check"] or load(root)["grading.default_check"]
