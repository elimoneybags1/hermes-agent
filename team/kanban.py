"""Team's view of Hermes kanban.

Every call runs `hermes -p team-worker kanban --board <board> ...` with
`HERMES_KANBAN_HOME=<state_dir>/kanban` added to the environment.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

from team.ledger import state_dir


class KanbanError(RuntimeError):
    """Raised when a hermes kanban command fails."""

    def __init__(self, message: str, stderr: str):
        super().__init__(message)
        self.stderr = stderr


def _kanban_home() -> Path:
    return Path(state_dir()) / "kanban"


def _run_kanban(
    args: list[str],
    env: dict[str, str] | None = None,
    runner=subprocess.run,
) -> subprocess.CompletedProcess:
    """Run a hermes kanban command with the required environment."""
    base_env = os.environ.copy()
    base_env["HERMES_KANBAN_HOME"] = str(_kanban_home())
    if env:
        base_env.update(env)

    cmd = [
        "hermes",
        "-p",
        "team-worker",
        "kanban",
        *args,
    ]
    return runner(cmd, capture_output=True, text=True, env=base_env)


def _parse_json_output(result: subprocess.CompletedProcess, cmd_name: str) -> Any:
    """Parse JSON output from a hermes kanban command, raising KanbanError on failure."""
    if result.returncode != 0:
        stderr_tail = result.stderr[-500:] if result.stderr else "(no stderr)"
        raise KanbanError(f"hermes kanban {cmd_name} failed", stderr_tail)

    output = result.stdout.strip()
    if not output:
        return None
    return json.loads(output)


def kanban_home() -> Path:
    """Return the kanban home directory path."""
    return _kanban_home()


def create_task(
    board: str,
    title: str,
    body: str,
    *,
    assignee: str = "team-worker",
    workspace: str | None = None,
    branch: str | None = None,
    max_runtime: int | None = None,
    skills: tuple[str, ...] = (),
    runner=subprocess.run,
) -> dict:
    """Create a kanban task and return the parsed task dict."""
    args = [
        "--board",
        board,
        "create",
        title,
        "--body",
        body,
        "--assignee",
        assignee,
        "--json",
    ]
    if workspace:
        args.extend(["--workspace", workspace])
    if branch:
        args.extend(["--branch", branch])
    if max_runtime is not None:
        args.extend(["--max-runtime", str(max_runtime)])
    if skills:
        args.extend(["--skills", ",".join(skills)])

    result = _run_kanban(args, runner=runner)
    return _parse_json_output(result, "create")


def show(
    board: str,
    task_id: str,
    runner=subprocess.run,
) -> dict:
    """Show a kanban task and return the parsed dict."""
    args = ["--board", board, "show", task_id, "--json"]
    result = _run_kanban(args, runner=runner)
    parsed = _parse_json_output(result, "show")

    # Accept both {"task": {...}, "runs": [...]} and the task itself
    if isinstance(parsed, dict) and "task" in parsed:
        return parsed["task"]
    return parsed


def list_tasks(
    board: str,
    status: str | None = None,
    runner=subprocess.run,
) -> list[dict]:
    """List kanban tasks and return the parsed list."""
    args = ["--board", board, "list", "--json"]
    if status:
        args.extend(["--status", status])
    result = _run_kanban(args, runner=runner)
    parsed = _parse_json_output(result, "list")
    return parsed if isinstance(parsed, list) else []


def dispatch(
    board: str,
    runner=subprocess.run,
) -> dict:
    """Dispatch tasks and return the parsed result."""
    args = ["--board", board, "dispatch", "--json"]
    result = _run_kanban(args, runner=runner)
    return _parse_json_output(result, "dispatch")


def comment(
    board: str,
    task_id: str,
    text: str,
    runner=subprocess.run,
) -> None:
    """Add a comment to a kanban task."""
    args = [
        "--board",
        board,
        "comment",
        task_id,
        text,
        "--author",
        "team",
    ]
    result = _run_kanban(args, runner=runner)
    _parse_json_output(result, "comment")


def block(
    board: str,
    task_id: str,
    reason: str,
    kind: str = "needs_input",
    runner=subprocess.run,
) -> None:
    """Block a kanban task."""
    args = [
        "--board",
        board,
        "block",
        task_id,
        reason,
        "--kind",
        kind,
    ]
    result = _run_kanban(args, runner=runner)
    _parse_json_output(result, "block")


def session_for_task(
    task_id: str,
    runner=subprocess.run,
) -> dict | None:
    """Find the Hermes session for a task and return its metrics."""
    # First, list sessions to find the one matching the task
    list_cmd = [
        "hermes",
        "-p",
        "team-worker",
        "sessions",
        "list",
        "--format",
        "json",
    ]
    env = os.environ.copy()
    env["HERMES_KANBAN_HOME"] = str(_kanban_home())

    list_result = runner(list_cmd, capture_output=True, text=True, env=env)
    if list_result.returncode != 0:
        return None

    try:
        sessions = json.loads(list_result.stdout.strip() or "[]")
    except json.JSONDecodeError:
        return None

    task_prefix = f"work kanban task {task_id[:9]}"
    matching_session = None
    for session in sessions:
        if session.get("title", "").startswith(task_prefix):
            matching_session = session
            break

    if not matching_session:
        return None

    session_id = matching_session.get("id")
    if not session_id:
        return None

    # Export the session to get detailed metrics
    export_cmd = [
        "hermes",
        "-p",
        "team-worker",
        "sessions",
        "export",
        "/dev/stdout",
        "--session-id",
        session_id,
        "--format",
        "jsonl",
    ]

    export_result = runner(export_cmd, capture_output=True, text=True, env=env)
    if export_result.returncode != 0:
        return None

    try:
        # JSONL: each line is a JSON object, take the last one (summary)
        lines = export_result.stdout.strip().split("\n")
        if not lines:
            return None
        last_line = lines[-1]
        data = json.loads(last_line)
    except (json.JSONDecodeError, IndexError):
        return None

    return {
        "session_id": session_id,
        "model": data.get("model"),
        "billing_provider": data.get("billing_provider"),
        "input_tokens": data.get("input_tokens"),
        "output_tokens": data.get("output_tokens"),
        "estimated_cost_usd": data.get("estimated_cost_usd"),
        "api_call_count": data.get("api_call_count"),
    }