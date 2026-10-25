"""Team's view of Hermes kanban.

Every call runs `hermes -p team-worker kanban --board <board> ...` with
`HERMES_KANBAN_HOME=<state_dir>/kanban` added to the environment.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
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
    model: str | None = None,
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
        args.extend(["--max-runtime", f"{max_runtime}s"])
    if skills:
        for skill in skills:
            args.extend(["--skill", skill])
    if model:
        args.extend(["--model", model])

    result = _run_kanban(args, runner=runner)
    return _parse_json_output(result, "create")


def show_full(
    board: str,
    task_id: str,
    runner=subprocess.run,
) -> dict:
    """Show a kanban task as {"task": {...}, "runs": [...], ...}.

    Accepts a bare task too, which then has no runs.
    """
    args = ["--board", board, "show", task_id, "--json"]
    result = _run_kanban(args, runner=runner)
    parsed = _parse_json_output(result, "show")
    if isinstance(parsed, dict) and "task" in parsed:
        return {**parsed, "runs": parsed.get("runs") or []}
    return {"task": parsed or {}, "runs": []}


def show(
    board: str,
    task_id: str,
    runner=subprocess.run,
) -> dict:
    """Show a kanban task and return the task dict."""
    return show_full(board, task_id, runner=runner)["task"]


def list_boards(runner=subprocess.run) -> list[dict]:
    """Every board in Team's kanban home: `boards list --json`."""
    result = _run_kanban(["boards", "list", "--json"], runner=runner)
    parsed = _parse_json_output(result, "boards list")
    return parsed if isinstance(parsed, list) else []


def create_board(name: str, runner=subprocess.run) -> None:
    """Create a board: `boards create <name>`."""
    result = _run_kanban(["boards", "create", name], runner=runner)
    if result.returncode != 0:
        stderr_tail = result.stderr[-500:] if result.stderr else "(no stderr)"
        raise KanbanError("hermes kanban boards create failed", stderr_tail)


def ensure_board(name: str, runner=subprocess.run) -> bool:
    """Create the board unless it exists; True when it was created."""
    if any(b.get("slug") == name for b in list_boards(runner=runner)):
        return False
    create_board(name, runner=runner)
    return True


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
    max_spawns: int | None = None,
    runner=subprocess.run,
) -> dict:
    """Dispatch tasks and return the parsed result.

    `max_spawns` caps how many workers kanban starts in this pass (`--max`).
    """
    args = ["--board", board, "dispatch", "--json"]
    if max_spawns is not None:
        args.extend(["--max", str(max_spawns)])
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
    if result.returncode != 0:
        stderr_tail = result.stderr[-500:] if result.stderr else "(no stderr)"
        raise KanbanError(f"hermes kanban comment failed", stderr_tail)


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
    if result.returncode != 0:
        stderr_tail = result.stderr[-500:] if result.stderr else "(no stderr)"
        raise KanbanError(f"hermes kanban block failed", stderr_tail)


def reopen_review(
    board: str,
    task_id: str,
    runner=subprocess.run,
) -> None:
    """Reopen a task from review back to ready for the implementer.

    Uses `hermes kanban reopen-review <id>`.
    """
    args = ["--board", board, "reopen-review", task_id]
    result = _run_kanban(args, runner=runner)
    if result.returncode != 0:
        stderr_tail = result.stderr[-500:] if result.stderr else "(no stderr)"
        raise KanbanError(f"hermes kanban reopen-review failed", stderr_tail)


def unblock(
    board: str,
    task_id: str,
    reason: str | None = None,
    runner=subprocess.run,
) -> None:
    """Move a blocked task back to ready: `unblock <id> [--reason R]`."""
    args = ["--board", board, "unblock", task_id]
    if reason:
        args.extend(["--reason", reason])
    result = _run_kanban(args, runner=runner)
    if result.returncode != 0:
        stderr_tail = result.stderr[-500:] if result.stderr else "(no stderr)"
        raise KanbanError("hermes kanban unblock failed", stderr_tail)


def log(
    board: str,
    task_id: str,
    tail: int | None = None,
    runner=subprocess.run,
) -> str:
    """The worker's log for a task: `log <id> [--tail <bytes>]`."""
    args = ["--board", board, "log", task_id]
    if tail is not None:
        args.extend(["--tail", str(tail)])
    result = _run_kanban(args, runner=runner)
    if result.returncode != 0:
        stderr_tail = result.stderr[-500:] if result.stderr else "(no stderr)"
        raise KanbanError("hermes kanban log failed", stderr_tail)
    return result.stdout or ""


def complete(
    board: str,
    task_id: str,
    result: str | None = None,
    runner=subprocess.run,
) -> None:
    """Complete a kanban task.

    Uses `hermes kanban complete <id>`.
    """
    args = ["--board", board, "complete", task_id]
    if result is not None:
        args.extend(["--result", result])
    result_obj = _run_kanban(args, runner=runner)
    if result_obj.returncode != 0:
        stderr_tail = result_obj.stderr[-500:] if result_obj.stderr else "(no stderr)"
        raise KanbanError(f"hermes kanban complete failed", stderr_tail)


def session_for_task(
    task_id: str,
    profile: str = "team-worker",
    runner=subprocess.run,
) -> dict | None:
    """Find the Hermes session for a task and return its metrics.

    Sessions live in the profile that ran the task (the task's assignee),
    not always team-worker.
    """
    # First, list sessions to find the one matching the task
    list_cmd = [
        "hermes",
        "-p",
        profile,
        "sessions",
        "list",
    ]
    env = os.environ.copy()
    env["HERMES_KANBAN_HOME"] = str(_kanban_home())

    list_result = runner(list_cmd, capture_output=True, text=True, env=env)
    if list_result.returncode != 0:
        return None

    # Real rows look like:
    # '{"title{ "title": "Work on kan   work kanban task t_6f7bd1e7   just now   20261024_205703_f92dde'
    # 'work kanban task t_4b0cf02b #2   work kanban task t_4b0cf02b   1h ago   20261024_195524_90c703'
    # The Preview column always holds 'work kanban task <id>'; the session id
    # is the last field.
    matches: list[str] = []
    for line in list_result.stdout.splitlines():
        fields = line.split()
        if task_id in fields:
            matches.append(fields[-1])

    if not matches:
        return None

    # Session ids sort by their YYYYMMDD_HHMMSS prefix: newest wins.
    matching_session_id = max(matches)

    # Export the session to a temp file (not /dev/stdout) and read its first line
    with tempfile.NamedTemporaryFile(mode="w+", delete=False, suffix=".jsonl") as tmp:
        tmp_path = tmp.name
    
    try:
        export_cmd = [
            "hermes",
            "-p",
            profile,
            "sessions",
            "export",
            tmp_path,
            "--session-id",
            matching_session_id,
            "--format",
            "jsonl",
        ]

        export_result = runner(export_cmd, capture_output=True, text=True, env=env)
        if export_result.returncode != 0:
            return None

        # Read the exported file's first valid JSON line
        try:
            with open(tmp_path, "r") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                        return {
                            "session_id": matching_session_id,
                            "model": data.get("model"),
                            "billing_provider": data.get("billing_provider"),
                            "input_tokens": data.get("input_tokens"),
                            "output_tokens": data.get("output_tokens"),
                            "estimated_cost_usd": data.get("estimated_cost_usd"),
                            "api_call_count": data.get("api_call_count"),
                        }
                    except json.JSONDecodeError:
                        continue
        except (json.JSONDecodeError, IndexError, OSError):
            return None
    finally:
        # Clean up temp file
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    return None
