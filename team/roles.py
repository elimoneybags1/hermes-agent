"""Roles: the roster of workers, as files under ``team/roles/<name>/``.

A role is a manifest (``role.toml``), its instructions (``SOUL.md``) and its
skills (``skills/<skill>/SKILL.md``). ``team roles sync`` copies them into
the role's Hermes profile; ``team run`` reads the manifest for budgets,
escalation and candidate models. No role exists until it has ``graded_by``,
and no role is dispatched unless its status is ``active``.
"""

from __future__ import annotations

import subprocess
import tomllib
from pathlib import Path

from team import catalog

# "claude": a role Team itself runs through Claude Code on the subscription
# (the lead); it has no Hermes profile and is never a kanban assignee.
HARNESSES = ("hermes", "hermes-subscription", "claude")
STATUSES = ("active", "planned")
# Hermes toolsets a worker may be given (platform_toolsets.cli).
TOOLSETS = (
    "terminal",
    "file",
    "code_execution",
    "todo",
    "skills",
    "web",
    "browser",
    "vision",
    "session_search",
    "clarify",
    "memory",
    "kanban",
    "team",
)
# Claude Code tool groups a "claude" role may name (team.claude_roles.TOOL_MAP).
CLAUDE_TOOLS = ("read-only", "file", "terminal", "todo", "skills")
REQUIRED = ("name", "title", "does", "graded_by", "status")


class RoleError(ValueError):
    """A manifest that is missing, malformed, or names things that do not exist."""


def roles_dir(root: Path) -> Path:
    return Path(root) / "team" / "roles"


def names(root: Path) -> list[str]:
    """Role names: directories under ``team/roles/`` with a role.toml, sorted."""
    d = roles_dir(root)
    if not d.is_dir():
        return []
    return sorted(
        p.name for p in d.iterdir() if p.is_dir() and (p / "role.toml").exists()
    )


def load(root: Path, name: str, cat: dict | None = None) -> dict:
    """Parse and validate one role; ``cat`` is the catalog for candidate checks."""
    path = roles_dir(root) / name / "role.toml"
    if not path.exists():
        raise RoleError(f"unknown role: {name}")
    with path.open("rb") as f:
        role = tomllib.load(f)
    for key in REQUIRED:
        if not role.get(key):
            raise RoleError(f"role {name}: role.toml needs {key!r}")
    if role["name"] != name:
        raise RoleError(f"role {name}: role.toml says name = {role['name']!r}")
    if role["status"] not in STATUSES:
        raise RoleError(f"role {name}: status must be one of {STATUSES}")
    harness = role.get("runs_on", {}).get("harness")
    if harness not in HARNESSES:
        raise RoleError(f"role {name}: runs_on.harness must be one of {HARNESSES}")
    known = CLAUDE_TOOLS if harness == "claude" else TOOLSETS
    for tool in role.get("tools", []):
        if tool not in known:
            raise RoleError(f"role {name}: unknown toolset {tool!r}")
    budget = role.setdefault("budget", {})
    budget.setdefault("max_turns", 40)
    budget.setdefault("run_budget_s", 900)
    for key in ("max_turns", "run_budget_s"):
        v = budget[key]
        if isinstance(v, bool) or not isinstance(v, int) or v < 1:
            raise RoleError(f"role {name}: budget.{key} must be a whole number above 0")
    esc = role.setdefault("escalation", {})
    esc.setdefault("after_failures", 0)
    if not isinstance(esc["after_failures"], int) or esc["after_failures"] < 0:
        raise RoleError(f"role {name}: escalation.after_failures must be 0 or more")
    cands = role.setdefault("models", {}).setdefault("candidates", [])
    if harness == "hermes" and role["status"] == "active" and not cands:
        raise RoleError(f"role {name}: an active hermes role needs models.candidates")
    if cat is not None:
        for spec in cands:
            try:
                catalog.resolve(spec, cat)
            except catalog.GateError as e:
                raise RoleError(f"role {name}: candidate {spec!r} refused: {e}") from e
    role["skills"] = skills(root, name)
    return role


def skill_summaries(root: Path, name: str) -> dict[str, str]:
    """Skill name -> the ``description`` line of its SKILL.md front matter (or "")."""
    out = {}
    for skill in skills(root, name):
        text = (roles_dir(root) / name / "skills" / skill / "SKILL.md").read_text()
        desc = ""
        if text.startswith("---"):
            for line in text.split("---", 2)[1].splitlines():
                if line.strip().startswith("description:"):
                    desc = line.split(":", 1)[1].strip().strip("'\"")
        out[skill] = desc
    return out


def load_all(root: Path, cat: dict | None = None) -> list[dict]:
    return [load(root, n, cat) for n in names(root)]


def skills(root: Path, name: str) -> list[str]:
    """Skill names under ``team/roles/<name>/skills/`` that have a SKILL.md."""
    d = roles_dir(root) / name / "skills"
    if not d.is_dir():
        return []
    return sorted(p.name for p in d.iterdir() if (p / "SKILL.md").exists())


def soul(root: Path, name: str) -> str | None:
    path = roles_dir(root) / name / "SOUL.md"
    return path.read_text() if path.exists() else None


def version(root: Path, name: str) -> str:
    """Git sha of the last commit touching this role's files, or "uncommitted"."""
    r = subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "log",
            "-1",
            "--format=%H",
            "--",
            f"team/roles/{name}",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    sha = r.stdout.strip() if r.returncode == 0 else ""
    return sha or "uncommitted"


def candidate(role: dict, attempt: int) -> str | None:
    """The model spec for this attempt: candidates in order, the last one repeating."""
    cands = role.get("models", {}).get("candidates") or []
    if not cands:
        return None
    pinned = role.get("models", {}).get("pinned")
    if pinned:
        return pinned
    return cands[min(attempt, len(cands)) - 1]
