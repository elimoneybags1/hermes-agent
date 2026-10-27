"""Outcomes ledger: every job Team runs, every model call, and what Eli decided.

SQLite at ``$TEAM_STATE/team.db``. A job's outcome is written by its grading
check, never by a model. There is no cost column: the free gate refuses any
paid model, so every call costs nothing; the ``violations`` table records any
call that reached something outside the catalog.

Tables (all present from day one, see PLAN.md Phase 4):

- ``jobs``: one attempt of one role on one target.
- ``calls``: one model call, written by the Team Hermes plugin's
  ``post_api_request`` hook or by the Claude runner; ``job_id`` is NULL when
  the call cannot be tied to a job yet, ``task_ref`` ties it later.
- ``goals``: what ``team run`` is tracking; survives restarts.
- ``decisions``: Eli's inbox.
- ``plans`` and ``parts``: the boss's plans and the parts it releases.
- ``reviews``: overseer and review-stage verdicts.
- ``pauses``: a target paused (free quota) until a time.
- ``violations``: anything that reached a non-catalog model or an
  off-gateway URL.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone, UTC
from pathlib import Path

OUTCOMES = ("pass", "fail", "error", "timeout")
DECISION_STATUSES = ("open", "answered", "dismissed")
PLAN_STATUSES = ("draft", "active", "done")
PART_STAGES = ("spec", "test_write", "implement", "test_verify", "review", "land", "done")
VIOLATION_KINDS = ("off-gateway", "non-catalog", "paid-key")

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
    task_ref      TEXT,
    model_choice  TEXT,
    part_id       INTEGER REFERENCES parts(id)
);

CREATE TABLE IF NOT EXISTS calls (
    id             INTEGER PRIMARY KEY,
    job_id         INTEGER REFERENCES jobs(id),
    task_ref       TEXT,
    session_id     TEXT,
    profile        TEXT,
    provider       TEXT NOT NULL,
    model          TEXT NOT NULL,
    response_model TEXT,
    base_url       TEXT,
    task_class     TEXT NOT NULL,
    tokens_in      INTEGER,
    tokens_out     INTEGER,
    latency_ms     INTEGER,
    ok             INTEGER NOT NULL CHECK (ok IN (0, 1)),
    error          TEXT,
    ts             TEXT NOT NULL,
    api_calls      INTEGER
);

CREATE TABLE IF NOT EXISTS goals (
    id            INTEGER PRIMARY KEY,
    target        TEXT NOT NULL,
    goal          TEXT NOT NULL,
    role          TEXT NOT NULL,
    branch        TEXT NOT NULL,
    worktree      TEXT NOT NULL,
    check_cmd     TEXT NOT NULL,
    attempt       INTEGER NOT NULL DEFAULT 1,
    current_task  TEXT,
    job_id        INTEGER REFERENCES jobs(id),
    created       TEXT NOT NULL,
    finished      TEXT,
    outcome       TEXT CHECK (outcome IN ('pass', 'fail', 'error', 'timeout')),
    detail        TEXT,
    part_id       INTEGER REFERENCES parts(id),
    base          TEXT NOT NULL DEFAULT 'main',
    needs_commits INTEGER NOT NULL DEFAULT 1 CHECK (needs_commits IN (0, 1))
);

CREATE TABLE IF NOT EXISTS decisions (
    id              INTEGER PRIMARY KEY,
    target          TEXT NOT NULL,
    created         TEXT NOT NULL,
    question        TEXT NOT NULL,
    context         TEXT NOT NULL,
    options         TEXT NOT NULL,
    recommendation  TEXT NOT NULL,
    goal_id         INTEGER REFERENCES goals(id),
    task_id         TEXT,
    source          TEXT NOT NULL,
    status          TEXT NOT NULL CHECK (status IN ('open', 'answered', 'dismissed')),
    answer          TEXT,
    answered_at     TEXT
);

CREATE TABLE IF NOT EXISTS plans (
    id       INTEGER PRIMARY KEY,
    target   TEXT NOT NULL,
    title    TEXT NOT NULL,
    created  TEXT NOT NULL,
    updated  TEXT NOT NULL,
    status   TEXT NOT NULL CHECK (status IN ('draft', 'active', 'done')),
    path     TEXT,
    summary  TEXT
);

CREATE TABLE IF NOT EXISTS parts (
    id          INTEGER PRIMARY KEY,
    plan_id     INTEGER NOT NULL REFERENCES plans(id),
    target      TEXT NOT NULL,
    title       TEXT NOT NULL,
    stage       TEXT NOT NULL CHECK (stage IN
                  ('spec', 'test_write', 'implement', 'test_verify', 'review', 'land', 'done')),
    branch      TEXT,
    depends_on  TEXT NOT NULL DEFAULT '[]',
    owned_paths TEXT NOT NULL DEFAULT '[]',
    status      TEXT NOT NULL DEFAULT 'open',
    created     TEXT NOT NULL,
    finished    TEXT
);

CREATE TABLE IF NOT EXISTS reviews (
    id                INTEGER PRIMARY KEY,
    target            TEXT NOT NULL,
    created           TEXT NOT NULL,
    source            TEXT NOT NULL,
    summary           TEXT NOT NULL,
    decisions_created TEXT NOT NULL DEFAULT '[]',
    actions           TEXT NOT NULL DEFAULT '[]',
    drift             TEXT
);

CREATE TABLE IF NOT EXISTS pauses (
    id      INTEGER PRIMARY KEY,
    target  TEXT NOT NULL,
    until   TEXT NOT NULL,
    reason  TEXT NOT NULL,
    created TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS violations (
    id       INTEGER PRIMARY KEY,
    ts       TEXT NOT NULL,
    target   TEXT,
    task_ref TEXT,
    kind     TEXT NOT NULL CHECK (kind IN ('off-gateway', 'non-catalog', 'paid-key')),
    detail   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS calls_job_id ON calls(job_id);
CREATE INDEX IF NOT EXISTS calls_task_ref ON calls(task_ref);
CREATE INDEX IF NOT EXISTS goals_target ON goals(target);
CREATE INDEX IF NOT EXISTS decisions_target_status ON decisions(target, status);
CREATE INDEX IF NOT EXISTS parts_plan_id ON parts(plan_id);
CREATE INDEX IF NOT EXISTS pauses_target ON pauses(target);
CREATE INDEX IF NOT EXISTS violations_task_ref ON violations(task_ref);
"""


def state_dir() -> Path:
    """``$TEAM_STATE``, or ``$HOME/state/team`` when it is unset."""
    env = os.environ.get("TEAM_STATE")
    return Path(env) if env else Path.home() / "state" / "team"


def db_path() -> Path:
    return state_dir() / "team.db"


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).isoformat(timespec="seconds")


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    """Open the ledger in WAL mode. Does not create tables; call ``init``."""
    path = Path(path) if path is not None else db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init(conn: sqlite3.Connection) -> None:
    """Create every table and index. Safe to run on an existing ledger."""
    with conn:
        conn.executescript(SCHEMA)


def _check(value: str, allowed: tuple[str, ...], what: str) -> None:
    if value not in allowed:
        raise ValueError(f"{what} must be one of {allowed}, got {value!r}")


# --- jobs -------------------------------------------------------------------

def start_job(
    conn: sqlite3.Connection,
    target: str,
    target_commit: str,
    role: str,
    role_version: str,
    attempt: int = 1,
    task_ref: str | None = None,
    model_choice: dict | None = None,
    part_id: int | None = None,
) -> int:
    """Record that a job has started and return its id.

    ``task_ref`` is the kanban task id, or ``claude:<session>`` for a Claude
    run. ``model_choice`` is what was chosen and why (spec, provider, model,
    reason), stored as JSON.
    """
    with conn:
        cur = conn.execute(
            "INSERT INTO jobs (target, target_commit, role, role_version, attempt,"
            " started, task_ref, model_choice, part_id)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (target, target_commit, role, role_version, attempt, _now(), task_ref,
             json.dumps(model_choice) if model_choice is not None else None, part_id),
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
    _check(outcome, OUTCOMES, "outcome")
    with conn:
        cur = conn.execute(
            "UPDATE jobs SET ended = ?, outcome = ?, grader = ?, grader_detail = ?"
            " WHERE id = ? AND ended IS NULL",
            (_now(), outcome, grader, grader_detail, job_id),
        )
    if cur.rowcount != 1:
        raise LookupError(f"no unfinished job with id {job_id}")


def set_task_ref(conn: sqlite3.Connection, job_id: int, task_ref: str) -> None:
    """Set a job's task_ref once it is known (a Claude session id)."""
    with conn:
        conn.execute("UPDATE jobs SET task_ref = ? WHERE id = ?", (task_ref, job_id))


def get_job(conn: sqlite3.Connection, job_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return _job(row) if row else None


def _job(row: sqlite3.Row) -> dict:
    d = dict(row)
    if d.get("model_choice"):
        d["model_choice"] = json.loads(d["model_choice"])
    return d


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
    return [_job(row) for row in conn.execute(sql, args)]


def open_job_for_task(conn: sqlite3.Connection, task_ref: str) -> dict | None:
    """The newest unfinished job with this task_ref, or None."""
    row = conn.execute(
        "SELECT * FROM jobs WHERE task_ref = ? AND ended IS NULL ORDER BY id DESC LIMIT 1",
        (task_ref,),
    ).fetchone()
    return _job(row) if row else None


def pass_rate(conn: sqlite3.Connection, target: str | None = None, role: str | None = None) -> tuple[int, int]:
    """(passes, finished) for the given target and role. Report both, never a bare rate."""
    sql = "SELECT SUM(outcome = 'pass'), COUNT(*) FROM jobs WHERE ended IS NOT NULL"
    args: list = []
    if target is not None:
        sql += " AND target = ?"
        args.append(target)
    if role is not None:
        sql += " AND role = ?"
        args.append(role)
    passes, total = conn.execute(sql, args).fetchone()
    return int(passes or 0), int(total or 0)


# --- calls ------------------------------------------------------------------

def record_call(
    conn: sqlite3.Connection,
    job_id: int | None,
    provider: str,
    model: str,
    task_class: str,
    tokens_in: int | None,
    tokens_out: int | None,
    latency_ms: int | None,
    ok: bool,
    error: str | None = None,
    api_calls: int | None = None,
    task_ref: str | None = None,
    session_id: str | None = None,
    profile: str | None = None,
    response_model: str | None = None,
    base_url: str | None = None,
) -> int:
    """Record one model call and return its id.

    ``job_id`` may be None when the call is logged by the Hermes plugin before
    Team has tied the task to a job; ``task_ref`` joins it later.
    ``response_model`` is the model that actually answered (LiteLLM and
    Portkey may route to a different one than was asked for).
    """
    with conn:
        cur = conn.execute(
            "INSERT INTO calls (job_id, task_ref, session_id, profile, provider, model,"
            " response_model, base_url, task_class, tokens_in, tokens_out, latency_ms,"
            " ok, error, ts, api_calls)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (job_id, task_ref, session_id, profile, provider, model, response_model,
             base_url, task_class, tokens_in, tokens_out, latency_ms, int(ok), error,
             _now(), api_calls),
        )
    return cur.lastrowid


def calls_for(conn: sqlite3.Connection, job_id: int | None = None, task_ref: str | None = None) -> list[dict]:
    """Calls for a job or a task, oldest first."""
    if job_id is None and task_ref is None:
        raise ValueError("job_id or task_ref is required")
    where, args = [], []
    if job_id is not None:
        where.append("job_id = ?")
        args.append(job_id)
    if task_ref is not None:
        where.append("task_ref = ?")
        args.append(task_ref)
    sql = "SELECT * FROM calls WHERE " + " OR ".join(where) + " ORDER BY id"
    return [dict(r) for r in conn.execute(sql, args)]


def attach_calls(conn: sqlite3.Connection, task_ref: str, job_id: int) -> int:
    """Give every unattached call for ``task_ref`` this job id; returns how many."""
    with conn:
        cur = conn.execute(
            "UPDATE calls SET job_id = ? WHERE task_ref = ? AND job_id IS NULL",
            (job_id, task_ref),
        )
    return cur.rowcount


# --- goals ------------------------------------------------------------------

GOAL_FIELDS = ("attempt", "current_task", "job_id")


def create_goal(
    conn: sqlite3.Connection,
    target: str,
    goal: str,
    role: str,
    branch: str,
    worktree: str,
    check: str,
    part_id: int | None = None,
    base: str = "main",
    needs_commits: bool = True,
) -> int:
    """Start tracking a goal ``team run`` works on; returns its id."""
    with conn:
        cur = conn.execute(
            "INSERT INTO goals (target, goal, role, branch, worktree, check_cmd, created,"
            " part_id, base, needs_commits) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (target, goal, role, branch, worktree, check, _now(), part_id, base,
             int(needs_commits)),
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


def finish_goal(conn: sqlite3.Connection, goal_id: int, outcome: str, detail: str | None = None) -> None:
    """Mark a goal finished. A goal can only be finished once."""
    _check(outcome, OUTCOMES, "outcome")
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


def list_goals(conn: sqlite3.Connection, target: str | None = None, all_goals: bool = False) -> list[dict]:
    """Goals, newest first; only unfinished ones unless ``all_goals``."""
    sql = "SELECT * FROM goals"
    where, args = [], []
    if not all_goals:
        where.append("finished IS NULL")
    if target is not None:
        where.append("target = ?")
        args.append(target)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC"
    return [dict(row) for row in conn.execute(sql, args)]


# --- decisions --------------------------------------------------------------

def create_decision(
    conn: sqlite3.Connection,
    target: str,
    question: str,
    context: str,
    options: list[str],
    recommendation: str,
    goal_id: int | None = None,
    task_id: str | None = None,
    source: str = "run",
) -> int:
    with conn:
        cur = conn.execute(
            "INSERT INTO decisions (target, created, question, context, options,"
            " recommendation, goal_id, task_id, source, status)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'open')",
            (target, _now(), question, context, json.dumps(list(options)),
             recommendation, goal_id, task_id, source),
        )
    return cur.lastrowid


def _decision(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["options"] = json.loads(d["options"])
    return d


def get_decision(conn: sqlite3.Connection, decision_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM decisions WHERE id = ?", (decision_id,)).fetchone()
    return _decision(row) if row else None


def list_decisions(conn: sqlite3.Connection, target: str | None = None, status: str | None = "open") -> list[dict]:
    """Decisions, oldest first; open ones by default, every status with ``status=None``."""
    sql = "SELECT * FROM decisions"
    where, args = [], []
    if target is not None:
        where.append("target = ?")
        args.append(target)
    if status is not None:
        _check(status, DECISION_STATUSES, "status")
        where.append("status = ?")
        args.append(status)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id"
    return [_decision(r) for r in conn.execute(sql, args)]


def answer_decision(conn: sqlite3.Connection, decision_id: int, answer: str) -> None:
    _settle_decision(conn, decision_id, "answered", answer)


def dismiss_decision(conn: sqlite3.Connection, decision_id: int) -> None:
    _settle_decision(conn, decision_id, "dismissed", None)


def _settle_decision(conn: sqlite3.Connection, decision_id: int, status: str, answer: str | None) -> None:
    with conn:
        cur = conn.execute(
            "UPDATE decisions SET status = ?, answer = ?, answered_at = ?"
            " WHERE id = ? AND status = 'open'",
            (status, answer, _now(), decision_id),
        )
    if cur.rowcount != 1:
        raise LookupError(f"no open decision with id {decision_id}")


def paused_goals(conn: sqlite3.Connection, target: str) -> set[int]:
    """Goal ids with an open decision: ``team run`` makes no new attempt on them."""
    rows = conn.execute(
        "SELECT DISTINCT goal_id FROM decisions WHERE target = ? AND status = 'open'"
        " AND goal_id IS NOT NULL",
        (target,),
    )
    return {r[0] for r in rows}


# --- plans and parts --------------------------------------------------------

def create_plan(conn: sqlite3.Connection, target: str, title: str, path: str | None = None,
                summary: str | None = None, status: str = "draft") -> int:
    _check(status, PLAN_STATUSES, "status")
    now = _now()
    with conn:
        cur = conn.execute(
            "INSERT INTO plans (target, title, created, updated, status, path, summary)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (target, title, now, now, status, path, summary),
        )
    return cur.lastrowid


def update_plan(conn: sqlite3.Connection, plan_id: int, status: str | None = None,
                summary: str | None = None) -> None:
    sets, args = ["updated = ?"], [_now()]
    if status is not None:
        _check(status, PLAN_STATUSES, "status")
        sets.append("status = ?")
        args.append(status)
    if summary is not None:
        sets.append("summary = ?")
        args.append(summary)
    with conn:
        cur = conn.execute(f"UPDATE plans SET {', '.join(sets)} WHERE id = ?", (*args, plan_id))
    if cur.rowcount != 1:
        raise LookupError(f"no plan with id {plan_id}")


def list_plans(conn: sqlite3.Connection, target: str | None = None) -> list[dict]:
    sql, args = "SELECT * FROM plans", []
    if target is not None:
        sql += " WHERE target = ?"
        args.append(target)
    return [dict(r) for r in conn.execute(sql + " ORDER BY id DESC", args)]


def create_part(conn: sqlite3.Connection, plan_id: int, target: str, title: str,
                stage: str = "implement", branch: str | None = None,
                depends_on: list[int] | None = None, owned_paths: list[str] | None = None) -> int:
    _check(stage, PART_STAGES, "stage")
    with conn:
        cur = conn.execute(
            "INSERT INTO parts (plan_id, target, title, stage, branch, depends_on, owned_paths,"
            " created) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (plan_id, target, title, stage, branch, json.dumps(depends_on or []),
             json.dumps(owned_paths or []), _now()),
        )
    return cur.lastrowid


def _part(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["depends_on"] = json.loads(d["depends_on"])
    d["owned_paths"] = json.loads(d["owned_paths"])
    return d


def get_part(conn: sqlite3.Connection, part_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM parts WHERE id = ?", (part_id,)).fetchone()
    return _part(row) if row else None


def list_parts(conn: sqlite3.Connection, plan_id: int | None = None, target: str | None = None) -> list[dict]:
    sql, where, args = "SELECT * FROM parts", [], []
    if plan_id is not None:
        where.append("plan_id = ?")
        args.append(plan_id)
    if target is not None:
        where.append("target = ?")
        args.append(target)
    if where:
        sql += " WHERE " + " AND ".join(where)
    return [_part(r) for r in conn.execute(sql + " ORDER BY id", args)]


def set_part_stage(conn: sqlite3.Connection, part_id: int, stage: str, status: str | None = None) -> None:
    _check(stage, PART_STAGES, "stage")
    sets, args = ["stage = ?"], [stage]
    if status is not None:
        sets.append("status = ?")
        args.append(status)
    if stage == "done":
        sets.append("finished = ?")
        args.append(_now())
    with conn:
        cur = conn.execute(f"UPDATE parts SET {', '.join(sets)} WHERE id = ?", (*args, part_id))
    if cur.rowcount != 1:
        raise LookupError(f"no part with id {part_id}")


# --- reviews ----------------------------------------------------------------

def add_review(conn: sqlite3.Connection, target: str, source: str, summary: str,
               decisions_created: list[int] | None = None, actions: list[dict] | None = None,
               drift: str | None = None) -> int:
    with conn:
        cur = conn.execute(
            "INSERT INTO reviews (target, created, source, summary, decisions_created, actions, drift)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (target, _now(), source, summary, json.dumps(decisions_created or []),
             json.dumps(actions or []), drift),
        )
    return cur.lastrowid


def list_reviews(conn: sqlite3.Connection, target: str | None = None, limit: int | None = None) -> list[dict]:
    sql, args = "SELECT * FROM reviews", []
    if target is not None:
        sql += " WHERE target = ?"
        args.append(target)
    sql += " ORDER BY id DESC"
    if limit is not None:
        sql += " LIMIT ?"
        args.append(limit)
    out = []
    for r in conn.execute(sql, args):
        d = dict(r)
        d["decisions_created"] = json.loads(d["decisions_created"])
        d["actions"] = json.loads(d["actions"])
        out.append(d)
    return out


# --- pauses and violations --------------------------------------------------

def add_pause(conn: sqlite3.Connection, target: str, until: float, reason: str) -> int:
    """Pause a target's dispatch until ``until`` (epoch seconds); returns its id."""
    with conn:
        cur = conn.execute(
            "INSERT INTO pauses (target, until, reason, created) VALUES (?, ?, ?, ?)",
            (target, _iso(until), reason, _now()),
        )
    return cur.lastrowid


def active_pause(conn: sqlite3.Connection, target: str, now: float) -> dict | None:
    """The target's pause that lasts longest past ``now``, or None."""
    row = conn.execute(
        "SELECT * FROM pauses WHERE target = ? AND until > ? ORDER BY until DESC LIMIT 1",
        (target, _iso(now)),
    ).fetchone()
    return dict(row) if row else None


def record_violation(conn: sqlite3.Connection, kind: str, detail: str,
                     target: str | None = None, task_ref: str | None = None) -> int:
    """A call reached something outside the catalog; ``team run`` blocks the task."""
    _check(kind, VIOLATION_KINDS, "kind")
    with conn:
        cur = conn.execute(
            "INSERT INTO violations (ts, target, task_ref, kind, detail) VALUES (?, ?, ?, ?, ?)",
            (_now(), target, task_ref, kind, detail),
        )
    return cur.lastrowid


def violations_for(conn: sqlite3.Connection, task_ref: str) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM violations WHERE task_ref = ? ORDER BY id", (task_ref,))]
