"""Decisions: questions that block work until Eli answers them.

Tables created by ledger.init. Answers also append to
$TEAM_STATE/targets/<name>/decisions.md.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from team.ledger import connect, state_dir


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def log_path(target: str) -> Path:
    """Path to the decisions.md log for a target."""
    return state_dir() / "targets" / target / "decisions.md"


def create(
    conn,
    target: str,
    question: str,
    context: str,
    options: list[str],
    recommendation: str,
    goal_id: int | None = None,
    source: str = "overseer",
    task_id: str | None = None,
) -> int:
    """Create a new decision and return its id."""
    with conn:
        cur = conn.execute(
            """
            INSERT INTO decisions (
                target, created, question, context, options, recommendation,
                goal_id, source, status, task_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open', ?)
            """,
            (
                target,
                _now(),
                question,
                context,
                json.dumps(options),
                recommendation,
                goal_id,
                source,
                task_id,
            ),
        )
    return cur.lastrowid


def list_open(conn, target: str | None = None) -> list[dict]:
    """All open decisions, newest first, optionally filtered by target."""
    sql = "SELECT * FROM decisions WHERE status = 'open'"
    args = []
    if target is not None:
        sql += " AND target = ?"
        args.append(target)
    sql += " ORDER BY id DESC"
    return [dict(row) for row in conn.execute(sql, args)]


def answer(conn, decision_id: int, text: str) -> None:
    """Answer a decision, updating its status and appending to decisions.md."""
    # Fetch the decision first
    row = conn.execute("SELECT * FROM decisions WHERE id = ?", (decision_id,)).fetchone()
    if row is None:
        raise LookupError(f"decision {decision_id} not found")
    if row["status"] != "open":
        raise ValueError(f"decision {decision_id} is not open (status={row['status']})")

    target = row["target"]
    question = row["question"]

    with conn:
        conn.execute(
            "UPDATE decisions SET status = 'answered', answer = ?, answered_at = ? WHERE id = ?",
            (text, _now(), decision_id),
        )

    # Append to decisions.md
    path = log_path(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    header = f"## {_now()} — {question}\n"
    entry = f"{header}{text}\n\n"
    # Create the file on first answer, never an empty one
    if path.exists():
        with path.open("a") as f:
            f.write(entry)
    else:
        path.write_text(entry)


def dismiss(conn, decision_id: int) -> None:
    """Dismiss a decision without answering."""
    row = conn.execute("SELECT status FROM decisions WHERE id = ?", (decision_id,)).fetchone()
    if row is None:
        raise LookupError(f"decision {decision_id} not found")
    if row["status"] != "open":
        raise ValueError(f"decision {decision_id} is not open (status={row['status']})")

    with conn:
        conn.execute(
            "UPDATE decisions SET status = 'dismissed', answer = '', answered_at = ? WHERE id = ?",
            (_now(), decision_id),
        )


def paused_goals(conn, target: str) -> set[int]:
    """Goal ids that have an open decision for this target."""
    rows = conn.execute(
        "SELECT goal_id FROM decisions WHERE target = ? AND status = 'open' AND goal_id IS NOT NULL",
        (target,),
    ).fetchall()
    return {r["goal_id"] for r in rows}