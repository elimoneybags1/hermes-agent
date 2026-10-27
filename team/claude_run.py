"""Claude Code on Eli's subscription, never on an API key.

``run`` launches ``claude -p`` in a worktree with every Anthropic variable
removed from the environment, in build mode (the role's plugin and agent,
Team's settings file, the stop and edit gates). The result is parsed from
Claude's JSON output. Every subprocess goes through an injectable ``runner``.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any

from team import claude_roles

SCRUB = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
)


def clean_env(env: dict[str, str] | None = None) -> dict[str, str]:
    """A copy of the environment without any way to bill the API."""
    env = dict(os.environ) if env is None else dict(env)
    return {k: v for k, v in env.items() if k not in SCRUB}


def subscription_ready(runner: Any = subprocess.run) -> tuple[bool, str]:
    """True when the Claude CLI is logged in through claude.ai, with the reason otherwise."""
    try:
        proc = runner(
            ["claude", "auth", "status"],
            capture_output=True,
            text=True,
            env=clean_env(),
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, f"claude auth status failed: {e}"
    try:
        data = json.loads(proc.stdout)
    except ValueError:
        return (
            False,
            f"claude auth status printed no JSON: {(proc.stderr or proc.stdout).strip()[:200]}",
        )
    if not data.get("loggedIn"):
        return False, "claude is not logged in"
    if data.get("authMethod") != "claude.ai":
        return False, f"claude auth method is {data.get('authMethod')!r}, not claude.ai"
    return True, "claude.ai"


def build_cmd(
    brief: str,
    *,
    max_turns: int = 60,
    tools: list[str] | None = None,
    model: str | None = None,
    json_schema: dict | None = None,
    plugin: Path | None = None,
    agent: str | None = None,
    settings: Path | None = None,
) -> list[str]:
    """The ``claude -p`` command line. With ``settings`` (build mode) Eli's user
    settings, plugins and MCP servers are left out."""
    cmd = [
        "claude",
        "-p",
        brief,
        "--output-format",
        "json",
        "--max-turns",
        str(max_turns),
        "--dangerously-skip-permissions",
    ]
    if settings:
        cmd += [
            "--setting-sources",
            "project,local",
            "--settings",
            str(settings),
            "--strict-mcp-config",
        ]
    if plugin:
        cmd += ["--plugin-dir", str(plugin)]
    if agent:
        cmd += ["--agent", agent]
    if tools is not None:
        cmd += ["--allowedTools", ",".join(tools)]
    if model:
        cmd += ["--model", model]
    if json_schema:
        cmd += ["--json-schema", json.dumps(json_schema)]
    return cmd


def _result(ok: bool, result: Any, duration_ms: int, **more: Any) -> dict[str, Any]:
    out = {
        "ok": ok,
        "result": result,
        "session_id": None,
        "num_turns": None,
        "duration_ms": duration_ms,
        "usage": None,
        "structured": None,
    }
    out.update(more)
    return out


def run(
    worktree: Path | str,
    brief: str,
    *,
    max_turns: int = 60,
    timeout_s: int = 1800,
    runner: Any = subprocess.run,
    role: str | None = None,
    root: Path | None = None,
    tools: list[str] | None = None,
    model: str | None = None,
    json_schema: dict | None = None,
    allowed_files: list[str] | None = None,
    check: str | None = None,
    decision_file: Path | str | None = None,
    state_dir: Path | None = None,
) -> dict[str, Any]:
    """Run ``claude -p`` with the brief in the worktree.

    With ``role`` (and Team's ``root``), Claude runs in build mode: the role's
    plugin and agent, its tools unless ``tools`` is given, Team's settings,
    and the gates set by ``check`` and ``allowed_files``.

    Returns ``{ok, result, structured, session_id, num_turns, duration_ms,
    usage}``; ``ok`` is False on a timeout, a non-zero exit, non-JSON output
    or ``is_error``.
    """
    worktree = Path(worktree)
    plugin = agent = settings = None
    if role:
        root = Path(root) if root else Path(__file__).resolve().parent.parent
        plugin = claude_roles.generate_plugin(root, role, state_dir)
        agent = claude_roles.agent_name(role)
        settings = claude_roles.settings_file(state_dir)
        if tools is None:
            tools = claude_roles.role_tools(root, role)
    cmd = build_cmd(
        brief,
        max_turns=max_turns,
        tools=tools,
        model=model,
        json_schema=json_schema,
        plugin=plugin,
        agent=agent,
        settings=settings,
    )
    env = clean_env()
    if check:
        env["TEAM_CHECK"] = check
    if allowed_files:
        env["TEAM_ALLOWED_FILES"] = ":".join(allowed_files)
    if decision_file:
        env["TEAM_DECISION_FILE"] = str(decision_file)
    start = time.monotonic()
    try:
        proc = runner(
            cmd,
            cwd=worktree,
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired:
        return _result(
            False, f"timeout after {timeout_s}s", int((time.monotonic() - start) * 1000)
        )
    duration_ms = int((time.monotonic() - start) * 1000)
    if proc.returncode != 0:
        return _result(
            False,
            (proc.stderr or "").strip() or (proc.stdout or "").strip(),
            duration_ms,
        )
    try:
        data = json.loads(proc.stdout)
    except ValueError:
        return _result(False, "output is not valid JSON", duration_ms)
    return _result(
        not data.get("is_error", False),
        data.get("result"),
        duration_ms,
        session_id=data.get("session_id"),
        num_turns=data.get("num_turns"),
        usage=data.get("usage"),
        structured=data.get("structured_output"),
    )
