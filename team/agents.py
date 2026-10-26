"""Agent management: read and write agent definitions with git commits."""

from __future__ import annotations

import hashlib
import re
import subprocess
import tomllib
import tomli_w
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from team.dash import _base_stats, _role_stats, _role_working_on
from team.free import is_free_model
from team import ledger
from team.ledger import sqlite3
from team.roles import load_roles


class AgentError(ValueError):
    """Raised when an agent operation is refused."""


TOOLSETS = [
    "terminal",
    "read-only",
    "orchestrator",
    "kanban",
    "file",
    "code_execution",
    "todo",
    "skills",
    "web",
    "browser",
    "vision",
    "session_search",
    "clarify",
]

SKILL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
VALID_STATUSES = {"active", "planned"}
VALID_HARNESSES = {"hermes", "claude-code", "crew", "team", "hermes-subscription"}

NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")


def create(
    root: Path,
    name: str,
    *,
    title: str,
    does: str,
    graded_by: str,
    model: str,
    tools: list[str],
    hands_to: str | None = None,
    instructions: str,
    source: str = "dashboard",
) -> str:
    """Create a new agent with role.toml and SOUL.md in one commit.
    
    Refuses:
    - existing name
    - name not matching ^[a-z][a-z0-9-]{0,31}$
    - non-free model
    - tools outside TOOLSETS
    - unknown hands_to
    
    Returns the commit SHA.
    """
    all_names = agent_names(root)
    if name in all_names:
        raise AgentError(f"agent already exists: {name}")
    
    if not NAME_RE.match(name):
        raise AgentError("name must match ^[a-z][a-z0-9-]{0,31}$")
    
    if not is_free_model(model):
        raise AgentError(f"model '{model}' is not free (must end with :free or be in FREE_EXACT)")
    
    for t in tools:
        if t not in TOOLSETS:
            raise AgentError(f"unknown tool: {t}")
    
    if hands_to is not None:
        if hands_to == name:
            raise AgentError("hands_to cannot point to itself")
        if hands_to not in all_names:
            raise AgentError(f"hands_to must be an existing agent name")
    
    # Create role directory
    role_dir = root / "roles" / name
    role_dir.mkdir(parents=True, exist_ok=True)
    
    # Build role.toml
    role_data = {
        "name": name,
        "title": title,
        "does": does,
        "graded_by": graded_by,
        "status": "planned",
        "tools": tools,
        "runs_on": {
            "harness": "hermes",
            "model": model,
        },
        "escalation": {
            "harness": "crew",
            "model": "claude",
            "after_failures": 2,
        },
        "budget": {
            "max_turns": 25,
            "run_budget_s": 600,
        },
    }
    if hands_to:
        role_data["hands_to"] = hands_to
    
    # Write role.toml
    role_toml_path = role_dir / "role.toml"
    with role_toml_path.open("wb") as f:
        tomli_w.dump(role_data, f)
    
    # Write SOUL.md
    soul_path = role_dir / "SOUL.md"
    soul_path.write_text(instructions)
    
    # Commit both files
    msg = f"{name}: created ({source})"
    return _git_commit(root, msg, role_toml_path, soul_path)


def agent_names(root: Path) -> list[str]:
    """Directories under roles/ with a role.toml, sorted."""
    roles_dir = root / "roles"
    if not roles_dir.is_dir():
        return []
    names = []
    for d in sorted(roles_dir.iterdir()):
        if d.is_dir() and (d / "role.toml").exists():
            names.append(d.name)
    return names


def pipeline(roles: list[dict]) -> list[str]:
    """Follow hands_to from 'boss' until a name repeats or is missing."""
    by_name = {r["name"]: r for r in roles}
    order: list[str] = []
    seen: set[str] = set()
    current = "boss"
    while current and current not in seen:
        if current not in by_name:
            break
        order.append(current)
        seen.add(current)
        current = by_name[current].get("hands_to", "")
    return order


def _read_role_toml(root: Path, name: str) -> dict:
    path = root / "roles" / name / "role.toml"
    with path.open("rb") as f:
        return tomllib.load(f)


def _write_role_toml(root: Path, name: str, data: dict) -> None:
    path = root / "roles" / name / "role.toml"
    with path.open("wb") as f:
        tomli_w.dump(data, f)


def _git_commit(root: Path, message: str, *paths: Path) -> str:
    """Commit the given paths and return the new commit sha."""
    rel_paths = [p.relative_to(root) for p in paths]
    subprocess.run(
        ["git", "-C", str(root), "add", *[str(p) for p in rel_paths]],
        check=True,
    )
    # Check if there's anything actually staged for these paths
    diff_result = subprocess.run(
        ["git", "-C", str(root), "diff", "--cached", "--quiet", "--", *[str(p) for p in rel_paths]],
        capture_output=True,
    )
    if diff_result.returncode == 0:
        # Nothing staged - no changes to commit
        raise AgentError("no change")
    subprocess.run(
        ["git", "-C", str(root), "commit", "-m", message, *[str(p) for p in rel_paths]],
        check=True,
    )
    result = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _git_rev_parse(root: Path, ref: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), "rev-parse", ref],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _git_diff_name_only(root: Path, sha: str) -> list[str]:
    """Return list of paths changed in the commit."""
    result = subprocess.run(
        ["git", "-C", str(root), "show", "--name-only", "--pretty=format:", sha],
        check=True,
        capture_output=True,
        text=True,
    )
    return [line for line in result.stdout.strip().split("\n") if line]


def _agent_paths(root: Path, name: str) -> set[str]:
    """All paths belonging to an agent (relative to root)."""
    paths = {f"roles/{name}/role.toml"}
    # Instructions
    soul = root / "roles" / name / "SOUL.md"
    if soul.exists():
        paths.add(f"roles/{name}/SOUL.md")
    # Skills
    skills_dir = root / "roles" / name / "skills"
    if skills_dir.is_dir():
        for skill_dir in skills_dir.iterdir():
            if skill_dir.is_dir():
                skill_file = skill_dir / "SKILL.md"
                if skill_file.exists():
                    paths.add(str(skill_file.relative_to(root)))
    # Hooks
    hooks = root / "roles" / name / "hooks.json"
    if hooks.exists():
        paths.add(f"roles/{name}/hooks.json")
    # Boss has special paths
    if name == "boss":
        boss_md = root / "plugin" / "agents" / "boss.md"
        if boss_md.exists():
            paths.add("plugin/agents/boss.md")
        boss_hooks = root / "plugin" / "hooks" / "hooks.json"
        if boss_hooks.exists():
            paths.add("plugin/hooks/hooks.json")
    return paths


def _agent_owns_path(root: Path, name: str, path: str) -> bool:
    """Check if an agent owns a given path (relative to root), per ownership rules.
    
    An agent owns:
    - Any path under roles/<name>/
    - plugin/agents/boss.md and plugin/hooks/hooks.json for boss
    """
    if name == "boss":
        if path in ("plugin/agents/boss.md", "plugin/hooks/hooks.json"):
            return True
    if path.startswith(f"roles/{name}/"):
        return True
    return False


def _get_instructions(root: Path, name: str) -> dict[str, Any]:
    """Read instructions file for an agent."""
    if name == "boss":
        path = root / "plugin" / "agents" / "boss.md"
    else:
        path = root / "roles" / name / "SOUL.md"
    text = path.read_text() if path.exists() else None
    return {"path": str(path.relative_to(root)), "text": text}


def _get_skills(root: Path, name: str) -> list[dict[str, Any]]:
    """List all skills for an agent."""
    skills = []
    skills_dir = root / "roles" / name / "skills"
    if skills_dir.is_dir():
        for skill_dir in sorted(skills_dir.iterdir()):
            if skill_dir.is_dir():
                skill_file = skill_dir / "SKILL.md"
                if skill_file.exists():
                    text = skill_file.read_text()
                    skills.append(
                        {
                            "name": skill_dir.name,
                            "path": str(skill_file.relative_to(root)),
                            "text": text,
                        }
                    )
    return skills


def _get_hooks(root: Path, name: str) -> list[dict[str, Any]]:
    """Get hooks for an agent (read-only)."""
    hooks = []
    if name == "boss":
        hooks_path = root / "plugin" / "hooks" / "hooks.json"
    else:
        hooks_path = root / "roles" / name / "hooks.json"
    if hooks_path.exists():
        import json
        with hooks_path.open() as f:
            data = json.load(f)
        for event, matchers in data.get("hooks", {}).items():
            for matcher in matchers:
                matcher_name = matcher.get("matcher", "")
                commands = matcher.get("hooks", [])
                for cmd in commands:
                    hooks.append(
                        {
                            "source": str(hooks_path.relative_to(root)),
                            "event": event,
                            "matcher": matcher_name,
                            "command": cmd.get("command", ""),
                        }
                    )
    return hooks


def _get_history(root: Path, name: str, limit: int = 30) -> list[dict[str, Any]]:
    """Last N commits touching this agent's files."""
    paths = _agent_paths(root, name)
    if not paths:
        return []
    result = subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "log",
            f"-{limit}",
            "--pretty=format:%H|%h|%ct|%an|%s",
            "--",
            *sorted(paths),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    history = []
    for line in result.stdout.strip().split("\n"):
        if not line:
            continue
        sha, short, date_str, author, subject = line.split("|", 4)
        # Convert unix timestamp to UTC ISO seconds
        dt = datetime.fromtimestamp(int(date_str), timezone.utc)
        history.append(
            {
                "sha": sha,
                "short": short,
                "date": dt.isoformat(timespec="seconds"),
                "author": author,
                "subject": subject,
            }
        )
    return history


def _get_jobs(conn: sqlite3.Connection | None, role: str, limit: int = 20) -> list[dict]:
    """Get ledger jobs for a role, newest first."""
    if conn is None:
        return []
    rows = conn.execute(
        "SELECT * FROM jobs WHERE role = ? ORDER BY id DESC LIMIT ?", (role, limit)
    ).fetchall()
    return [dict(r) for r in rows]


def detail(
    root: Path, conn: sqlite3.Connection | None, name: str
) -> dict[str, Any]:
    """Detailed agent info for the dashboard."""
    all_roles = load_roles(root)
    role = next((r for r in all_roles if r["name"] == name), None)
    if role is None:
        raise AgentError(f"unknown agent: {name}")

    role_toml = _read_role_toml(root, name)
    tools = role_toml.get("tools", [])

    return {
        "name": name,
        "role": role_toml,
        "instructions": _get_instructions(root, name),
        "skills": _get_skills(root, name),
        "tools": tools,
        "available_tools": TOOLSETS,
        "hooks": _get_hooks(root, name),
        "history": _get_history(root, name),
        "jobs": _get_jobs(conn, name),
        "working_on": _role_working_on(conn, name) if conn else [],
        "stats": _role_stats(conn, name) if conn else {"jobs": 0, "passed": 0, "pass_rate": None},
        "base": _base_stats(conn) if conn else {"jobs": 0, "passed": 0, "pass_rate": None},
    }




def set_field(
    root: Path, name: str, field: str, value: Any, source: str = "dashboard"
) -> str:
    """Set a field in role.toml and commit. Returns new commit sha."""
    all_names = agent_names(root)
    if name not in all_names:
        raise AgentError(f"unknown agent: {name}")

    # Validate field
    if field in ("title", "does", "graded_by"):
        if not isinstance(value, str) or not value.strip():
            raise AgentError(f"{field} must be a non-empty string")
    elif field == "status":
        if value not in VALID_STATUSES:
            raise AgentError(f"status must be one of {sorted(VALID_STATUSES)}")
    elif field == "runs_on.model":
        if not isinstance(value, str) or not value.strip():
            raise AgentError("runs_on.model must be a non-empty string")
        # Check free model if harness is hermes
        role_data = _read_role_toml(root, name)
        harness = role_data.get("runs_on", {}).get("harness")
        if harness == "hermes" and not is_free_model(value):
            raise AgentError(f"model '{value}' is not free (must end with :free or be in FREE_EXACT)")
    elif field == "budget.max_turns":
        if not isinstance(value, int) or value <= 0:
            raise AgentError("budget.max_turns must be an integer > 0")
    elif field == "budget.run_budget_s":
        if not isinstance(value, int) or value <= 0:
            raise AgentError("budget.run_budget_s must be an integer > 0")
    elif field == "escalation.after_failures":
        if not isinstance(value, int) or value <= 0:
            raise AgentError("escalation.after_failures must be an integer > 0")
    elif field == "tools":
        if not isinstance(value, list):
            raise AgentError("tools must be a list")
        for t in value:
            if t not in TOOLSETS:
                raise AgentError(f"unknown tool: {t}")
    elif field == "hands_to":
        if not isinstance(value, str) or not value.strip():
            raise AgentError("hands_to must be a non-empty string")
        if value not in all_names:
            raise AgentError(f"hands_to must be an existing agent name")
        if value == name:
            raise AgentError("hands_to cannot point to itself")
    else:
        raise AgentError(f"unknown field: {field}")

    # Read, modify, write
    role_data = _read_role_toml(root, name)

    # Navigate nested fields
    if "." in field:
        top, sub = field.split(".", 1)
        if top not in role_data:
            role_data[top] = {}
        if not isinstance(role_data[top], dict):
            role_data[top] = {}
        role_data[top][sub] = value
    else:
        role_data[field] = value

    _write_role_toml(root, name, role_data)

    # Commit
    path = root / "roles" / name / "role.toml"
    msg = f"{name}: set {field} ({source})"
    return _git_commit(root, msg, path)


def write_instructions(
    root: Path, name: str, text: str, source: str = "dashboard"
) -> str:
    """Write instructions (SOUL.md or boss.md) and commit."""
    all_names = agent_names(root)
    if name not in all_names:
        raise AgentError(f"unknown agent: {name}")

    if name == "boss":
        path = root / "plugin" / "agents" / "boss.md"
    else:
        path = root / "roles" / name / "SOUL.md"

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)

    msg = f"{name}: update instructions ({source})"
    return _git_commit(root, msg, path)


def write_skill(
    root: Path, name: str, skill: str, text: str, source: str = "dashboard"
) -> str:
    """Write a skill file and commit. Skill name must match ^[a-z0-9][a-z0-9-]{0,63}$."""
    all_names = agent_names(root)
    if name not in all_names:
        raise AgentError(f"unknown agent: {name}")

    if not SKILL_NAME_RE.match(skill):
        raise AgentError("skill name must match ^[a-z0-9][a-z0-9-]{0,63}$")

    skill_path = root / "roles" / name / "skills" / skill / "SKILL.md"
    skill_path.parent.mkdir(parents=True, exist_ok=True)
    skill_path.write_text(text)

    msg = f"{name}: add skill {skill} ({source})"
    return _git_commit(root, msg, skill_path)


def delete_skill(
    root: Path, name: str, skill: str, source: str = "dashboard"
) -> str:
    """Delete a skill directory and commit."""
    all_names = agent_names(root)
    if name not in all_names:
        raise AgentError(f"unknown agent: {name}")

    if not SKILL_NAME_RE.match(skill):
        raise AgentError("skill name must match ^[a-z0-9][a-z0-9-]{0,63}$")

    skill_path = root / "roles" / name / "skills" / skill
    if not skill_path.is_dir():
        raise AgentError(f"skill not found: {skill}")

    msg = f"{name}: delete skill {skill} ({source})"
    # Use git rm -r to stage and remove the directory
    rel_path = skill_path.relative_to(root)
    subprocess.run(
        ["git", "-C", str(root), "rm", "-r", str(rel_path)],
        check=True,
    )
    # Check if there's anything actually staged for this path
    diff_result = subprocess.run(
        ["git", "-C", str(root), "diff", "--cached", "--quiet", "--", str(rel_path)],
        capture_output=True,
    )
    if diff_result.returncode == 0:
        # Nothing staged - no changes to commit
        raise AgentError("no change")
    subprocess.run(
        ["git", "-C", str(root), "commit", "-m", msg, str(rel_path)],
        check=True,
    )
    result = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def revert(
    root: Path, name: str, sha: str, source: str = "dashboard"
) -> str:
    """Revert a commit. Refused unless every path the commit touched belongs to that agent."""
    all_names = agent_names(root)
    if name not in all_names:
        raise AgentError(f"unknown agent: {name}")

    # Get paths touched by the commit
    commit_paths = _git_diff_name_only(root, sha)
    if not commit_paths:
        raise AgentError("commit touches no files")

    for p in commit_paths:
        if not _agent_owns_path(root, name, p):
            raise AgentError(
                f"commit {sha[:8]} touches {p} which does not belong to {name}"
            )

    # Revert by restoring each file to its state before the commit, then commit
    for rel_path in commit_paths:
        abs_path = root / rel_path
        # Get the file content at the parent of the commit (before the commit)
        try:
            result = subprocess.run(
                ["git", "-C", str(root), "show", f"{sha}^:{rel_path}"],
                check=True,
                capture_output=True,
                text=True,
            )
            # Ensure parent directory exists
            abs_path.parent.mkdir(parents=True, exist_ok=True)
            abs_path.write_text(result.stdout)
        except subprocess.CalledProcessError:
            # File was created in this commit, so remove it
            if abs_path.exists():
                abs_path.unlink()

    msg = f"{name}: revert {sha[:8]} ({source})"
    return _git_commit(root, msg, *[root / p for p in commit_paths])


def fingerprint(root: Path, db: Path | sqlite3.Connection) -> str:
    """Hash of all agent-defining state: mtimes/sizes under roles/, plugin/, .git/HEAD, .git/refs/heads/, ledger file + -wal, orchestration.toml, and target.json files."""
    # Get the ledger file path from the connection if needed
    if isinstance(db, sqlite3.Connection):
        # Get the database file path from the connection
        cursor = db.execute("PRAGMA database_list")
        row = cursor.fetchone()
        db_path = Path(row["file"]) if row and row["file"] else ledger.db_path()
    else:
        db_path = db

    hasher = hashlib.sha256()

    # roles/
    roles_dir = root / "roles"
    if roles_dir.exists():
        for path in sorted(roles_dir.rglob("*")):
            if path.is_file():
                stat = path.stat()
                rel = path.relative_to(root)
                hasher.update(f"{rel}:{stat.st_mtime_ns}:{stat.st_size}".encode())

    # plugin/
    plugin_dir = root / "plugin"
    if plugin_dir.exists():
        for path in sorted(plugin_dir.rglob("*")):
            if path.is_file():
                stat = path.stat()
                rel = path.relative_to(root)
                hasher.update(f"{rel}:{stat.st_mtime_ns}:{stat.st_size}".encode())

    # .git/HEAD
    head = root / ".git" / "HEAD"
    if head.exists():
        stat = head.stat()
        hasher.update(f".git/HEAD:{stat.st_mtime_ns}:{stat.st_size}".encode())

    # .git/refs/heads/
    refs_heads = root / ".git" / "refs" / "heads"
    if refs_heads.exists():
        for path in sorted(refs_heads.rglob("*")):
            if path.is_file():
                stat = path.stat()
                rel = path.relative_to(root)
                hasher.update(f"{rel}:{stat.st_mtime_ns}:{stat.st_size}".encode())

    # orchestration.toml in repo root
    orchestration = root / "orchestration.toml"
    if orchestration.exists():
        stat = orchestration.stat()
        hasher.update(f"orchestration.toml:{stat.st_mtime_ns}:{stat.st_size}".encode())

    # target.json files in $TEAM_STATE/targets/*/
    targets_dir = ledger.state_dir() / "targets"
    if targets_dir.exists():
        for target_json in sorted(targets_dir.rglob("target.json")):
            if target_json.is_file():
                stat = target_json.stat()
                # Use a path relative to the state dir for consistency
                rel = target_json.relative_to(targets_dir.parent)
                hasher.update(f"{rel}:{stat.st_mtime_ns}:{stat.st_size}".encode())

    # ledger file and -wal
    for suffix in ("", "-wal", "-shm"):
        ledger_file = db_path if suffix == "" else Path(str(db_path) + suffix)
        if ledger_file.exists():
            stat = ledger_file.stat()
            hasher.update(f"{ledger_file}:{stat.st_mtime_ns}:{stat.st_size}".encode())

    return hasher.hexdigest()[:16]