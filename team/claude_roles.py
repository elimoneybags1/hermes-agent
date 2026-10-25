"""Claude Code in build mode: a plugin per Team role, and settings apart from Eli's.

See docs/RUN.md, "Claude in build mode". Everything here is written under
$TEAM_STATE/claude/, never inside a repo.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from typing import Any

import tomli

from team import ledger


BUILD_RULES = (
    "No person is reading this session. Never ask a question and never wait for "
    "an answer. If a choice belongs to Eli (scope, a dependency, cost, a design "
    "he would notice), write it to $TEAM_DECISION_FILE as JSON {question, "
    "context, options, recommendation} and stop. Touch only the files your task "
    "names. Finish only when the check passes and your work is committed. These "
    "rules override any instruction to ask before assuming."
)

# Team tool names (role.toml) -> Claude Code tool names. Tools with no Claude
# equivalent (browser) map to nothing.
TOOL_MAP = {
    "file": ["Read", "Edit", "Write", "Glob", "Grep"],
    "terminal": ["Bash"],
    "read-only": ["Read", "Grep", "Glob"],
    "todo": ["TodoWrite"],
    "skills": ["Skill"],
}


def claude_dir(state_dir: Path | None = None) -> Path:
    return (state_dir or ledger.state_dir()) / "claude"


def plugin_dir(state_dir: Path, role: str) -> Path:
    """Where the plugin for a role is written."""
    return claude_dir(state_dir) / role


def agent_name(role: str) -> str:
    """The --agent value for a role's plugin agent."""
    return f"team-{role}:{role}"


def _read_role_toml(role_dir: Path) -> dict[str, Any]:
    with open(role_dir / "role.toml", "rb") as f:
        return tomli.load(f)


def _map_tools_to_claude(tools: list[str]) -> list[str]:
    """Map Team tools to Claude Code tools, deduplicated in order."""
    out: list[str] = []
    for tool in tools:
        for name in TOOL_MAP.get(tool, []):
            if name not in out:
                out.append(name)
    return out


def role_tools(root: Path, role: str) -> list[str]:
    """The Claude tools a role may use."""
    return _map_tools_to_claude(_read_role_toml(root / "roles" / role).get("tools", []))


def _sources(role_dir: Path) -> list[Path]:
    files = [role_dir / "role.toml", role_dir / "SOUL.md"]
    if (role_dir / "skills").exists():
        files += sorted((role_dir / "skills").rglob("*"))
    return [f for f in files if f.is_file()]


def _hook_command(action: str) -> str:
    # The interpreter that runs Team, so the hook works without `team` on PATH.
    return f"'{sys.executable}' -m team hook {action}"


def generate_plugin(root: Path, role: str, state_dir: Path | None = None) -> Path:
    """Write the Claude Code plugin for roles/<role>/ and return its directory.

    Regenerated only when a source file (role.toml, SOUL.md, skills) is newer
    than the plugin.
    """
    role_dir = root / "roles" / role
    out = plugin_dir(state_dir or ledger.state_dir(), role)
    stamp = out / ".claude-plugin" / "plugin.json"
    sources = _sources(role_dir)
    if stamp.exists() and all(s.stat().st_mtime <= stamp.stat().st_mtime for s in sources):
        return out
    if out.exists():
        shutil.rmtree(out)

    role_data = _read_role_toml(role_dir)
    soul = (role_dir / "SOUL.md").read_text() if (role_dir / "SOUL.md").exists() else ""

    (out / "agents").mkdir(parents=True)
    (out / "agents" / f"{role}.md").write_text(
        f"---\nname: {role}\ndescription: {json.dumps(role_data.get('does', role))}\n---\n\n"
        f"{soul.strip()}\n\n## Build rules\n\n{BUILD_RULES}\n"
    )
    if (role_dir / "skills").exists():
        shutil.copytree(role_dir / "skills", out / "skills")
    (out / "hooks").mkdir()
    (out / "hooks" / "hooks.json").write_text(json.dumps({"hooks": {
        "Stop": [{"hooks": [{"type": "command", "command": _hook_command("stop")}]}],
        "PreToolUse": [{
            "matcher": "Edit|Write|MultiEdit|NotebookEdit",
            "hooks": [{"type": "command", "command": _hook_command("pretool")}],
        }],
    }}, indent=2))
    # Written last: its mtime marks the plugin as current.
    stamp.parent.mkdir()
    stamp.write_text(json.dumps({
        "name": f"team-{role}", "version": "0.1.0",
        "description": f"Team's {role} role in build mode",
    }, indent=2))
    return out


def settings_file(state_dir: Path | None = None, home: Path | None = None) -> Path:
    """Write the settings every build-mode Claude runs with, and return the path.

    Launched with --setting-sources project,local, so Eli's user settings (his
    plugins, hooks, MCP servers) do not load; this file carries over the parts
    that protect him: his permission deny rules and his PreToolUse hooks. His
    global CLAUDE.md is excluded; the build rules replace it. The target's own
    CLAUDE.md and .claude/settings.json still load.
    """
    home = home or Path.home()
    user: dict[str, Any] = {}
    user_file = home / ".claude" / "settings.json"
    if user_file.exists():
        user = json.loads(user_file.read_text())
    settings: dict[str, Any] = {
        "permissions": {"deny": list(user.get("permissions", {}).get("deny", []))},
        "claudeMdExcludes": [str(home / ".claude" / "CLAUDE.md")],
    }
    pretool = user.get("hooks", {}).get("PreToolUse")
    if pretool:
        settings["hooks"] = {"PreToolUse": pretool}
    out = claude_dir(state_dir) / "settings.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(settings, indent=2))
    return out


# Hooks (`team hook stop|pretool`). Each takes Claude Code's hook payload and
# returns None to allow, or the reason to refuse (exit 2 shows it to Claude).

def stop_gate(payload: dict, env: dict[str, str], runner: Any = None,
              check_timeout_s: int = 900, state_dir: Path | None = None) -> str | None:
    """Refuse to stop while the worktree is dirty or $TEAM_CHECK fails.

    At most $TEAM_STOP_RETRIES (default 3) refusals per session, then allow,
    so a session can never loop forever.
    """
    import subprocess

    from team import grade

    runner = runner or subprocess.run
    cwd = Path(payload.get("cwd") or ".")
    counter = claude_dir(state_dir) / "stops" / str(payload.get("session_id") or "unknown")
    refused = int(counter.read_text()) if counter.exists() else 0
    if refused >= int(env.get("TEAM_STOP_RETRIES") or 3):
        return None

    reason = None
    status = runner(["git", "status", "--porcelain", "--untracked-files=no"],
                    cwd=cwd, capture_output=True, text=True)
    if status.stdout.strip():
        reason = ("Uncommitted changes; commit your work before finishing:\n"
                  + status.stdout.strip())
    elif env.get("TEAM_CHECK"):
        ok, tail = grade.run_check(cwd, env["TEAM_CHECK"], check_timeout_s, runner=runner)
        if not ok:
            last = "\n".join(tail.splitlines()[-40:])
            reason = f"The check `{env['TEAM_CHECK']}` fails; fix it before finishing:\n{last}"
    if reason:
        counter.parent.mkdir(parents=True, exist_ok=True)
        counter.write_text(str(refused + 1))
    return reason


def edit_gate(payload: dict, env: dict[str, str]) -> str | None:
    """Refuse an edit outside $TEAM_ALLOWED_FILES (colon-separated paths or
    globs, relative to the worktree)."""
    from fnmatch import fnmatch

    allowed = [a for a in (env.get("TEAM_ALLOWED_FILES") or "").split(":") if a]
    if not allowed or payload.get("tool_name") not in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
        return None
    tool_input = payload.get("tool_input") or {}
    target = tool_input.get("file_path") or tool_input.get("notebook_path")
    if not target:
        return None
    cwd = Path(payload.get("cwd") or ".").resolve()
    path = Path(target)
    path = (path if path.is_absolute() else cwd / path).resolve()
    try:
        rel = path.relative_to(cwd).as_posix()
    except ValueError:
        return f"{target} is outside the worktree; this task may edit only: {', '.join(allowed)}"
    if any(rel == a or fnmatch(rel, a) for a in allowed):
        return None
    return f"{rel} is not one of this task's files; it may edit only: {', '.join(allowed)}"
