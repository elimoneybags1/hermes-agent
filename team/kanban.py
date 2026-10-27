"""Team's view of Hermes kanban.

Every call runs ``team/bin/team-hermes kanban --board <board> ...``: the
wrapper puts the board under ``$TEAM_STATE/kanban`` and scrubs the
environment. Kanban owns boards, tasks, worktree workspaces, spawning,
heartbeats and the review column; Team decides what is "done".

A task's model is never a free-form string: ``create_task`` takes a
``ModelChoice`` from ``team.catalog``, so nothing outside the catalog can be
dispatched (``kanban create --model`` bypasses the profile pin otherwise).

Every subprocess goes through an injectable ``runner`` so tests never spawn
Hermes.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from team.catalog import ModelChoice
from team.ledger import state_dir

Runner = Callable[..., subprocess.CompletedProcess[Any]]


class KanbanError(RuntimeError):
    """A hermes kanban command failed; ``stderr`` holds the tail."""

    def __init__(self, message: str, stderr: str = ""):
        super().__init__(message)
        self.stderr = stderr


def wrapper() -> Path:
    return Path(__file__).resolve().parent / "bin" / "team-hermes"


def kanban_home() -> Path:
    return state_dir() / "kanban"


def _run(
    args: list[str], runner: Runner = subprocess.run
) -> subprocess.CompletedProcess[Any]:
    return runner([str(wrapper()), "kanban", *args], capture_output=True, text=True)


def _ok(result: subprocess.CompletedProcess[Any], what: str) -> str:
    if result.returncode != 0:
        tail = (result.stderr or result.stdout or "(no output)")[-800:]
        raise KanbanError(f"hermes kanban {what} failed", tail)
    return result.stdout or ""


def _json(result: subprocess.CompletedProcess[Any], what: str) -> Any:
    """Parse the JSON document in stdout, skipping setup chatter printed before it.

    The wrapper's PM sync prints coloured status lines first; the JSON starts
    on its own line with ``{`` or ``[``.
    """
    out = _ok(result, what).strip()
    if not out:
        return None
    lines = out.splitlines()
    for i, line in enumerate(lines):
        if line.lstrip()[:1] in ("{", "["):
            doc = "\n".join(lines[i:])
            try:
                return json.loads(doc)
            except ValueError as e:
                raise KanbanError(
                    f"hermes kanban {what} printed bad JSON: {e}", doc[-800:]
                ) from e
    raise KanbanError(f"hermes kanban {what} printed no JSON", out[-800:])


# --- boards -----------------------------------------------------------------


def list_boards(runner: Runner = subprocess.run) -> list[dict]:
    parsed = _json(_run(["boards", "list", "--json"], runner), "boards list")
    if isinstance(parsed, dict):
        parsed = parsed.get("boards", [])
    return parsed if isinstance(parsed, list) else []


def create_board(
    name: str, description: str | None = None, runner: Runner = subprocess.run
) -> None:
    args = ["boards", "create", name]
    if description:
        args += ["--description", description]
    _ok(_run(args, runner), "boards create")


def ensure_board(
    name: str, description: str | None = None, runner: Runner = subprocess.run
) -> bool:
    """Create the board unless it exists; True when it was created."""
    if any(b.get("slug") == name or b.get("name") == name for b in list_boards(runner)):
        return False
    create_board(name, description, runner)
    return True


# --- tasks ------------------------------------------------------------------


def create_task(
    board: str,
    title: str,
    body: str,
    *,
    assignee: str,
    model: ModelChoice | None = None,
    workspace: str | None = None,
    branch: str | None = None,
    max_runtime_s: int | None = None,
    max_retries: int | None = None,
    skills: tuple[str, ...] = (),
    parents: tuple[str, ...] = (),
    runner: Runner = subprocess.run,
) -> dict:
    """Create a task and return it (has ``id`` and ``status``)."""
    if not isinstance(model, ModelChoice) and model is not None:
        raise TypeError(
            "model must be a team.catalog.ModelChoice (never a free-form string)"
        )
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
    for parent in parents:
        args += ["--parent", parent]
    if workspace:
        args += ["--workspace", workspace]
    if branch:
        args += ["--branch", branch]
    if max_runtime_s is not None:
        args += ["--max-runtime", f"{int(max_runtime_s)}s"]
    if max_retries is not None:
        if max_retries < 1:
            raise ValueError("max_retries is a failure limit and must be at least 1")
        args += ["--max-retries", str(max_retries)]
    for skill in skills:
        args += ["--skill", skill]
    if model is not None:
        args += ["--provider", model.provider, "--model", model.model]
    task = _json(_run(args, runner), "create")
    if not isinstance(task, dict) or "id" not in task:
        raise KanbanError(
            "hermes kanban create returned no task", json.dumps(task)[-800:]
        )
    return task


def show_full(board: str, task_id: str, runner: Runner = subprocess.run) -> dict:
    """``{"task": {...}, "runs": [...]}``; a bare task answer gets an empty runs list."""
    parsed = _json(_run(["--board", board, "show", task_id, "--json"], runner), "show")
    if isinstance(parsed, dict) and "task" in parsed:
        return {**parsed, "runs": parsed.get("runs") or []}
    return {"task": parsed or {}, "runs": []}


def show(board: str, task_id: str, runner: Runner = subprocess.run) -> dict:
    return show_full(board, task_id, runner)["task"]


def runs(board: str, task_id: str, runner: Runner = subprocess.run) -> list[dict]:
    """The task's runs (worker pid, started_at, status), oldest first."""
    parsed = _json(_run(["--board", board, "runs", task_id, "--json"], runner), "runs")
    if isinstance(parsed, dict):
        parsed = parsed.get("runs", [])
    return parsed if isinstance(parsed, list) else []


def list_tasks(
    board: str, status: str | None = None, runner: Runner = subprocess.run
) -> list[dict]:
    args = ["--board", board, "list", "--json"]
    if status:
        args += ["--status", status]
    parsed = _json(_run(args, runner), "list")
    if isinstance(parsed, dict):
        parsed = parsed.get("tasks", [])
    return parsed if isinstance(parsed, list) else []


def dispatch(
    board: str, max_spawns: int | None = None, runner: Runner = subprocess.run
) -> dict:
    """One dispatch pass; ``max_spawns`` caps new workers this tick."""
    args = ["--board", board, "dispatch", "--json"]
    if max_spawns is not None:
        args += ["--max", str(max_spawns)]
    parsed = _json(_run(args, runner), "dispatch")
    return parsed if isinstance(parsed, dict) else {}


def comment(
    board: str,
    task_id: str,
    text: str,
    author: str = "team",
    runner: Runner = subprocess.run,
) -> None:
    _ok(
        _run(["--board", board, "comment", task_id, text, "--author", author], runner),
        "comment",
    )


def block(
    board: str,
    task_id: str,
    reason: str,
    kind: str = "needs_input",
    runner: Runner = subprocess.run,
) -> None:
    _ok(
        _run(["--board", board, "block", task_id, reason, "--kind", kind], runner),
        "block",
    )


def unblock(
    board: str, task_id: str, reason: str | None = None, runner: Runner = subprocess.run
) -> None:
    args = ["--board", board, "unblock", task_id]
    if reason:
        args += ["--reason", reason]
    _ok(_run(args, runner), "unblock")


def set_model(
    board: str, task_id: str, model: ModelChoice, runner: Runner = subprocess.run
) -> None:
    """Pin the next dispatch to the validated retry candidate."""
    if not isinstance(model, ModelChoice):
        raise TypeError("model must be a team.catalog.ModelChoice")
    _ok(
        _run(
            [
                "--board",
                board,
                "set-model",
                task_id,
                model.model,
                "--provider",
                model.provider,
            ],
            runner,
        ),
        "set-model",
    )


def reopen_review(
    board: str, task_id: str, reason: str | None = None, runner: Runner = subprocess.run
) -> None:
    """Send a task in ``review`` back to its implementer; comments stay in its context."""
    args = ["--board", board, "reopen-review", task_id]
    if reason:
        args += ["--reason", reason]
    _ok(_run(args, runner), "reopen-review")


def request_changes(
    board: str, task_id: str, reason: str, runner: Runner = subprocess.run
) -> None:
    _ok(
        _run(["--board", board, "request-changes", task_id, reason], runner),
        "request-changes",
    )


def complete(
    board: str, task_id: str, result: str | None = None, runner: Runner = subprocess.run
) -> None:
    args = ["--board", board, "complete", task_id]
    if result is not None:
        args += ["--result", result]
    _ok(_run(args, runner), "complete")


def reclaim(
    board: str, task_id: str, reason: str | None = None, runner: Runner = subprocess.run
) -> None:
    args = ["--board", board, "reclaim", task_id]
    if reason:
        args += ["--reason", reason]
    _ok(_run(args, runner), "reclaim")


def log(
    board: str, task_id: str, tail: int | None = None, runner: Runner = subprocess.run
) -> str:
    args = ["--board", board, "log", task_id]
    if tail is not None:
        args += ["--tail", str(tail)]
    return _ok(_run(args, runner), "log")
