"""Outcomes ledger: every job Team runs, and every model call inside it.

The schema is the one agreed in CLAUDE.md. A job's outcome is written by its
grading check, never by a model. There is no cost column: the free gate
refuses any paid model, so every call costs nothing.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

OUTCOMES = ("pass", "fail", "error", "timeout")

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id            INTEGER PRIMARY KEY,
    target        TEXT NOT NULL,
    target_commit TEXT NOT NULL,
    role          TEXT NOT NULL,
    role_version  TEXT NOT NULL,
    attempt       INTEGER NOT NULL DEFAULT 1,
    started       TEXT NOT NULL,
    ended         TEXT,
    outcome       TEXT CHECK (outcome IN ('pass', 'fail', 'error', 'timeout')),
    grader        TEXT,
    grader_detail TEXT,
    task_ref      TEXT
);

CREATE TABLE IF NOT EXISTS calls (
    id          INTEGER PRIMARY KEY,
    job_id      INTEGER NOT NULL REFERENCES jobs(id),
    provider    TEXT NOT NULL,
    model       TEXT NOT NULL,
    task_class  TEXT NOT NULL,
    tokens_in   INTEGER,
    tokens_out  INTEGER,
    latency_ms  INTEGER,
    ok          INTEGER NOT NULL CHECK (ok IN (0, 1)),
    error       TEXT,
    ts          TEXT
);

CREATE TABLE IF NOT EXISTS decisions (
    id              INTEGER PRIMARY KEY,
    target          TEXT NOT NULL,
    created         TEXT NOT NULL,
    question        TEXT NOT NULL,
    context         TEXT NOT NULL,
    options         TEXT NOT NULL,
    recommendation  TEXT NOT NULL,
    goal_id         INTEGER,
    source          TEXT NOT NULL,
    status          TEXT NOT NULL CHECK (status IN ('open', 'answered', 'dismissed')),
    answer          TEXT,
    answered_at     TEXT,
    task_id         TEXT
);

CREATE INDEX IF NOT EXISTS calls_job_id ON calls(job_id);
CREATE INDEX IF NOT EXISTS decisions_target_status ON decisions(target, status);

CREATE TABLE IF NOT EXISTS goals (
    id           INTEGER PRIMARY KEY,
    target       TEXT NOT NULL,
    goal         TEXT NOT NULL,
    role         TEXT NOT NULL,
    branch       TEXT NOT NULL,
    worktree     TEXT NOT NULL,
    check_cmd    TEXT NOT NULL,
    attempt      INTEGER NOT NULL DEFAULT 1,
    current_task TEXT,
    job_id       INTEGER REFERENCES jobs(id),
    created      TEXT NOT NULL,
    finished     TEXT,
    outcome      TEXT CHECK (outcome IN ('pass', 'fail', 'error', 'timeout')),
    detail       TEXT
);

CREATE INDEX IF NOT EXISTS goals_target ON goals(target);
"""


def state_dir() -> Path:
    """$TEAM_STATE, or $HOME/state/team when it is unset."""
    env = os.environ.get("TEAM_STATE")
    return Path(env) if env else Path.home() / "state" / "team"


def db_path() -> Path:
    return state_dir() / "team.db"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    """Open the ledger. Does not create tables; call init() for that."""
    path = Path(path) if path is not None else db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init(conn: sqlite3.Connection) -> None:
    """Create the jobs, calls and goals tables. Safe to run on an existing ledger.

    A ledger made before calls.ts, jobs.task_ref or a decisions column existed gets the column
    added in place; existing rows keep NULL there, since it was never recorded.
    """
    with conn:
        conn.executescript(SCHEMA)
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(calls)")}
        if "ts" not in cols:
            conn.execute("ALTER TABLE calls ADD COLUMN ts TEXT")
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(jobs)")}
        if "task_ref" not in cols:
            conn.execute("ALTER TABLE jobs ADD COLUMN task_ref TEXT")

        # Ensure decisions table has all columns (migrate old ledgers)
        dec_cols = {r["name"] for r in conn.execute("PRAGMA table_info(decisions)")}
        if "task_id" not in dec_cols:
            conn.execute("ALTER TABLE decisions ADD COLUMN task_id TEXT")
        if "source" not in dec_cols:
            conn.execute("ALTER TABLE decisions ADD COLUMN source TEXT NOT NULL DEFAULT 'overseer'")


def start_job(
    conn: sqlite3.Connection,
    target: str,
    target_commit: str,
    role: str,
    role_version: str,
    attempt: int = 1,
    task_ref: str | None = None,
) -> int:
    """Record that a job has started and return its id.

    `task_ref` is the kanban task id, or `claude:<session>` for a Claude run.
    """
    with conn:
        cur = conn.execute(
            "INSERT INTO jobs (target, target_commit, role, role_version, attempt,"
            " started, task_ref) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (target, target_commit, role, role_version, attempt, _now(), task_ref),
        )
    return cur.lastrowid


def finish_job(
    conn: sqlite3.Connection,
    job_id: int,
    outcome: str,
    grader: str,
    grader_detail: str | None = None,
) -> None:
    """Record a job's graded outcome. A job can only be finished once."""
    if outcome not in OUTCOMES:
        raise ValueError(f"outcome must be one of {OUTCOMES}, got {outcome!r}")
    with conn:
        cur = conn.execute(
            "UPDATE jobs SET ended = ?, outcome = ?, grader = ?, grader_detail = ?"
            " WHERE id = ? AND ended IS NULL",
            (_now(), outcome, grader, grader_detail, job_id),
        )
    if cur.rowcount != 1:
        raise LookupError(f"no unfinished job with id {job_id}")


def record_call(
    conn: sqlite3.Connection,
    job_id: int,
    provider: str,
    model: str,
    task_class: str,
    tokens_in: int | None,
    tokens_out: int | None,
    latency_ms: int | None,
    ok: bool,
    error: str | None = None,
) -> int:
    """Record one model call made by a job and return its id."""
    with conn:
        cur = conn.execute(
            "INSERT INTO calls (job_id, provider, model, task_class, tokens_in,"
            " tokens_out, latency_ms, ok, error, ts)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (job_id, provider, model, task_class, tokens_in, tokens_out,
             latency_ms, int(ok), error, _now()),
        )
    return cur.lastrowid


def list_jobs(
    conn: sqlite3.Connection,
    target: str | None = None,
    role: str | None = None,
    limit: int | None = None,
) -> list[dict]:
    """Jobs, newest first, optionally filtered by target and role."""
    sql = "SELECT * FROM jobs"
    where, args = [], []
    if target is not None:
        where.append("target = ?")
        args.append(target)
    if role is not None:
        where.append("role = ?")
        args.append(role)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC"
    if limit is not None:
        sql += " LIMIT ?"
        args.append(limit)
    return [dict(row) for row in conn.execute(sql, args)]


def set_task_ref(conn: sqlite3.Connection, job_id: int, task_ref: str) -> None:
    """Set a job's task_ref, once it is known (a Claude session id)."""
    with conn:
        conn.execute("UPDATE jobs SET task_ref = ? WHERE id = ?", (task_ref, job_id))


def get_job(conn: sqlite3.Connection, job_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return dict(row) if row else None


GOAL_FIELDS = ("attempt", "current_task", "job_id")


def create_goal(
    conn: sqlite3.Connection,
    target: str,
    goal: str,
    role: str,
    branch: str,
    worktree: str,
    check: str,
) -> int:
    """Start tracking a goal `team run` works on; returns its id."""
    with conn:
        cur = conn.execute(
            "INSERT INTO goals (target, goal, role, branch, worktree, check_cmd, created)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (target, goal, role, branch, worktree, check, _now()),
        )
    return cur.lastrowid


def update_goal(conn: sqlite3.Connection, goal_id: int, **fields) -> None:
    """Change a goal's attempt, current_task or job_id."""
    bad = set(fields) - set(GOAL_FIELDS)
    if bad:
        raise ValueError(f"cannot update goal fields {sorted(bad)}")
    if not fields:
        return
    sets = ", ".join(f"{k} = ?" for k in fields)
    with conn:
        conn.execute(f"UPDATE goals SET {sets} WHERE id = ?", (*fields.values(), goal_id))


def finish_goal(
    conn: sqlite3.Connection,
    goal_id: int,
    outcome: str,
    detail: str | None = None,
) -> None:
    """Mark a goal finished. A goal can only be finished once."""
    if outcome not in OUTCOMES:
        raise ValueError(f"outcome must be one of {OUTCOMES}, got {outcome!r}")
    with conn:
        cur = conn.execute(
            "UPDATE goals SET finished = ?, outcome = ?, detail = ?"
            " WHERE id = ? AND finished IS NULL",
            (_now(), outcome, detail, goal_id),
        )
    if cur.rowcount != 1:
        raise LookupError(f"no unfinished goal with id {goal_id}")


def active_goals(conn: sqlite3.Connection, target: str | None = None) -> list[dict]:
    """Goals not yet finished, oldest first."""
    sql = "SELECT * FROM goals WHERE finished IS NULL"
    args: list = []
    if target is not None:
        sql += " AND target = ?"
        args.append(target)
    sql += " ORDER BY id"
    return [dict(row) for row in conn.execute(sql, args)]


def get_goal(conn: sqlite3.Connection, goal_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM goals WHERE id = ?", (goal_id,)).fetchone()
    return dict(row) if row else None
