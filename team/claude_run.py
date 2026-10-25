"""Claude Code runner using the subscription (no API keys)."""

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
    """Return a copy of env without the SCRUB variables."""
    if env is None:
        env = dict(os.environ)
    return {k: v for k, v in env.items() if k not in SCRUB}


class ClaudeRunError(RuntimeError):
    """Raised when claude command fails."""


def _build_cmd(
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
    """Build the claude -p command line.

    With `settings` (build mode) Eli's user settings, plugins and MCP servers
    are left out; that file carries what must stay (docs/RUN.md).
    """
    cmd = [
        "claude", "-p", brief,
        "--output-format", "json",
        "--max-turns", str(max_turns),
        "--dangerously-skip-permissions",
    ]
    if settings:
        cmd += ["--setting-sources", "project,local", "--settings", str(settings),
                "--strict-mcp-config"]
    if plugin:
        cmd += ["--plugin-dir", str(plugin)]
    if agent:
        cmd += ["--agent", agent]
    if tools is not None:
        cmd += ["--tools", ",".join(tools)]
    if model:
        cmd += ["--model", model]
    if json_schema:
        cmd += ["--json-schema", json.dumps(json_schema)]
    return cmd


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
) -> dict[str, Any]:
    """Run claude -p with the given brief in the worktree.

    With `role` (and Team's `root`), Claude runs in build mode: the role's
    plugin and agent, its tools unless `tools` is given, Team's settings, and
    the gates set by `check` and `allowed_files`.

    Returns a dict with: ok, result, structured, session_id, num_turns,
    duration_ms, usage. Missing keys are None. ok is False on error, non-zero
    exit, or non-JSON output.
    """
    worktree = Path(worktree)
    plugin = agent = settings = None
    if role:
        root = Path(root) if root else Path(__file__).resolve().parent.parent
        plugin = claude_roles.generate_plugin(root, role)
        agent = claude_roles.agent_name(role)
        settings = claude_roles.settings_file()
        if tools is None:
            tools = claude_roles.role_tools(root, role)
    cmd = _build_cmd(
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
        duration_ms = int((time.monotonic() - start) * 1000)
        return {
            "ok": False,
            "result": f"timeout after {timeout_s}s",
            "session_id": None,
            "num_turns": None,
            "duration_ms": duration_ms,
            "usage": None,
            "structured": None,
        }

    duration_ms = int((time.monotonic() - start) * 1000)

    if proc.returncode != 0:
        return {
            "ok": False,
            "result": proc.stderr.strip() or proc.stdout.strip(),
            "session_id": None,
            "num_turns": None,
            "duration_ms": duration_ms,
            "usage": None,
            "structured": None,
        }

    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {
            "ok": False,
            "result": "output is not valid JSON",
            "session_id": None,
            "num_turns": None,
            "duration_ms": duration_ms,
            "usage": None,
            "structured": None,
        }

    is_error = data.get("is_error", False)
    return {
        "ok": not is_error,
        "result": data.get("result"),
        "session_id": data.get("session_id"),
        "num_turns": data.get("num_turns"),
        "duration_ms": duration_ms,
        "usage": data.get("usage"),
        "structured": data.get("structured_output"),
    }