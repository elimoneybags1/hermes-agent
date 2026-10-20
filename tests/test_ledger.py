import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from team import ledger


@pytest.fixture
def conn(tmp_path):
    c = ledger.connect(tmp_path / "team.db")
    ledger.init(c)
    yield c
    c.close()


def test_db_path_uses_team_state(monkeypatch, tmp_path):
    monkeypatch.setenv("TEAM_STATE", str(tmp_path / "s"))
    assert ledger.db_path() == tmp_path / "s" / "team.db"


def test_db_path_defaults_under_home(monkeypatch, tmp_path):
    monkeypatch.delenv("TEAM_STATE", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert ledger.db_path() == tmp_path / "state" / "team" / "team.db"


def test_init_creates_agreed_columns(conn):
    def cols(table):
        return [r["name"] for r in conn.execute(f"PRAGMA table_info({table})")]

    assert cols("jobs") == [
        "id", "target", "target_commit", "role", "role_version", "attempt",
        "started", "ended", "outcome", "grader", "grader_detail",
    ]
    assert cols("calls") == [
        "id", "job_id", "provider", "model", "task_class", "tokens_in",
        "tokens_out", "latency_ms", "ok", "error", "ts",
    ]


def test_init_is_idempotent(conn):
    job = ledger.start_job(conn, "team", "abc123", "profiler", "def456")
    ledger.init(conn)
    assert [j["id"] for j in ledger.list_jobs(conn)] == [job]


def test_job_lifecycle(conn):
    job = ledger.start_job(conn, "team", "abc123", "profiler", "def456", attempt=2)
    [row] = ledger.list_jobs(conn)
    assert row["id"] == job
    assert row["attempt"] == 2
    assert row["started"]
    assert row["ended"] is None and row["outcome"] is None

    ledger.finish_job(conn, job, "pass", "pytest", "12 passed")
    [row] = ledger.list_jobs(conn)
    assert row["outcome"] == "pass"
    assert row["grader"] == "pytest"
    assert row["grader_detail"] == "12 passed"
    assert row["ended"] >= row["started"]


def test_finish_job_rejects_unknown_outcome(conn):
    job = ledger.start_job(conn, "team", "abc", "profiler", "v1")
    with pytest.raises(ValueError):
        ledger.finish_job(conn, job, "passed", "pytest")


def test_outcome_constraint_holds_in_sql(conn):
    job = ledger.start_job(conn, "team", "abc", "profiler", "v1")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE jobs SET outcome = 'maybe' WHERE id = ?", (job,))


def test_finish_job_twice_or_missing_raises(conn):
    job = ledger.start_job(conn, "team", "abc", "profiler", "v1")
    ledger.finish_job(conn, job, "fail", "pytest")
    with pytest.raises(LookupError):
        ledger.finish_job(conn, job, "pass", "pytest")
    with pytest.raises(LookupError):
        ledger.finish_job(conn, 999, "pass", "pytest")


def test_record_call(conn):
    job = ledger.start_job(conn, "team", "abc", "profiler", "v1")
    ledger.record_call(conn, job, "groq", "llama-3.3-70b", "classify", 120, 8, 340, True)
    ledger.record_call(conn, job, "groq", "llama-3.3-70b", "classify", None, None, 30000,
                       False, "timeout")
    rows = conn.execute("SELECT * FROM calls WHERE job_id = ? ORDER BY id", (job,)).fetchall()
    assert [r["ok"] for r in rows] == [1, 0]
    assert rows[0]["tokens_in"] == 120 and rows[0]["tokens_out"] == 8
    assert rows[1]["error"] == "timeout"


def test_record_call_sets_utc_ts(conn):
    job = ledger.start_job(conn, "team", "abc", "profiler", "v1")
    ledger.record_call(conn, job, "groq", "m", "classify", 1, 1, 1, True)
    [row] = conn.execute("SELECT ts FROM calls").fetchall()
    ts = datetime.fromisoformat(row["ts"])
    assert ts.utcoffset() == timedelta(0)
    assert ts.microsecond == 0
    assert abs(datetime.now(timezone.utc) - ts) < timedelta(minutes=1)


OLD_CALLS = """
CREATE TABLE jobs (
    id INTEGER PRIMARY KEY, target TEXT NOT NULL, target_commit TEXT NOT NULL,
    role TEXT NOT NULL, role_version TEXT NOT NULL,
    attempt INTEGER NOT NULL DEFAULT 1, started TEXT NOT NULL, ended TEXT,
    outcome TEXT, grader TEXT, grader_detail TEXT
);
CREATE TABLE calls (
    id INTEGER PRIMARY KEY, job_id INTEGER NOT NULL REFERENCES jobs(id),
    provider TEXT NOT NULL, model TEXT NOT NULL, task_class TEXT NOT NULL,
    tokens_in INTEGER, tokens_out INTEGER, latency_ms INTEGER,
    ok INTEGER NOT NULL, error TEXT
);
INSERT INTO jobs (target, target_commit, role, role_version, started)
    VALUES ('team', 'c1', 'profiler', 'v1', '2026-10-19T00:00:00+00:00');
INSERT INTO calls (job_id, provider, model, task_class, ok)
    VALUES (1, 'groq', 'm', 'classify', 1);
"""


def test_init_adds_ts_to_old_ledger_keeping_rows(tmp_path):
    c = ledger.connect(tmp_path / "old.db")
    c.executescript(OLD_CALLS)
    ledger.init(c)
    ledger.init(c)
    assert [r["name"] for r in c.execute("PRAGMA table_info(calls)")][-1] == "ts"
    assert [j["id"] for j in ledger.list_jobs(c)] == [1]
    [old] = c.execute("SELECT * FROM calls").fetchall()
    assert old["provider"] == "groq" and old["ts"] is None
    ledger.record_call(c, 1, "groq", "m", "classify", 1, 1, 1, True)
    assert c.execute("SELECT ts FROM calls WHERE id = 2").fetchone()["ts"]
    c.close()


def test_record_call_requires_existing_job(conn):
    with pytest.raises(sqlite3.IntegrityError):
        ledger.record_call(conn, 999, "groq", "m", "classify", 1, 1, 1, True)


def test_list_jobs_filters_and_orders(conn):
    a = ledger.start_job(conn, "team", "c1", "profiler", "v1")
    b = ledger.start_job(conn, "brain", "c2", "profiler", "v1")
    c = ledger.start_job(conn, "team", "c3", "fixer", "v1")
    assert [j["id"] for j in ledger.list_jobs(conn)] == [c, b, a]
    assert [j["id"] for j in ledger.list_jobs(conn, target="team")] == [c, a]
    assert [j["id"] for j in ledger.list_jobs(conn, target="team", role="profiler")] == [a]
    assert [j["id"] for j in ledger.list_jobs(conn, limit=1)] == [c]
