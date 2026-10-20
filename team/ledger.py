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
    grader_detail TEXT
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
    error       TEXT
);

CREATE INDEX IF NOT EXISTS calls_job_id ON calls(job_id);
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
    """Create the jobs and calls tables. Safe to run on an existing ledger."""
    with conn:
        conn.executescript(SCHEMA)


def start_job(
    conn: sqlite3.Connection,
    target: str,
    target_commit: str,
    role: str,
    role_version: str,
    attempt: int = 1,
) -> int:
    """Record that a job has started and return its id."""
    with conn:
        cur = conn.execute(
            "INSERT INTO jobs (target, target_commit, role, role_version, attempt, started)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (target, target_commit, role, role_version, attempt, _now()),
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
            " tokens_out, latency_ms, ok, error) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (job_id, provider, model, task_class, tokens_in, tokens_out,
             latency_ms, int(ok), error),
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
