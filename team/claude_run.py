"""Claude Code runner using the subscription (no API keys)."""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any

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


def run(
    worktree: Path | str,
    brief: str,
    *,
    max_turns: int = 60,
    timeout_s: int = 1800,
    runner: Any = subprocess.run,
) -> dict[str, Any]:
    """Run claude -p with the given brief in the worktree.

    Returns a dict with: ok, result, session_id, num_turns, duration_ms, usage.
    Missing keys are None. ok is False on error, non-zero exit, or non-JSON output.
    """
    worktree = Path(worktree)
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
    env = clean_env()
    start = time.monotonic()
    try:
        proc = runner(
            cmd,
            cwd=worktree,
            env=env,
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
        }

    is_error = data.get("is_error", False)
    return {
        "ok": not is_error,
        "result": data.get("result") if not is_error else data.get("result"),
        "session_id": data.get("session_id"),
        "num_turns": data.get("num_turns"),
        "duration_ms": duration_ms,
        "usage": data.get("usage"),
    }