"""Tests for team/decisions.py using real sqlite ledger via TEAM_STATE in tmp_path."""

import json
import pytest
import os

from team import ledger, decisions


@pytest.fixture
def conn(monkeypatch, tmp_path):
    """Create a fresh ledger connection with decisions table."""
    state = tmp_path / "state"
    monkeypatch.setenv("TEAM_STATE", str(state))
    c = ledger.connect()
    ledger.init(c)
    yield c
    c.close()


def test_create_decision(conn):
    """Create a decision and verify it's in the DB."""
    d_id = decisions.create(
        conn,
        target="team",
        question="Should we use Python 3.12?",
        context="New features need 3.12",
        options=["yes", "no"],
        recommendation="yes",
        goal_id=5,
        source="overseer",
        task_id="t_3",
    )
    assert d_id == 1

    row = conn.execute("SELECT * FROM decisions WHERE id = 1").fetchone()
    assert row["target"] == "team"
    assert row["question"] == "Should we use Python 3.12?"
    assert row["context"] == "New features need 3.12"
    assert json.loads(row["options"]) == ["yes", "no"]
    assert row["recommendation"] == "yes"
    assert row["goal_id"] == 5
    assert row["source"] == "overseer"
    assert row["status"] == "open"
    assert row["task_id"] == "t_3"
    assert row["answer"] is None
    assert row["answered_at"] is None


def test_list_open_decisions(conn):
    """List open decisions, optionally filtered by target."""
    decisions.create(conn, "team", "Q1", "C", ["a", "b"], "a", goal_id=1, task_id="t_1")
    decisions.create(conn, "brain", "Q2", "C", ["a", "b"], "a", task_id="t_2")
    decisions.create(conn, "team", "Q3", "C", ["a", "b"], "a")

    all_open = decisions.list_open(conn)
    assert len(all_open) == 3
    # newest first
    assert all_open[0]["question"] == "Q3"
    assert all_open[1]["question"] == "Q2"
    assert all_open[2]["question"] == "Q1"

    team_only = decisions.list_open(conn, target="team")
    assert len(team_only) == 2
    assert all(r["target"] == "team" for r in team_only)


def test_answer_decision_appends_to_log(monkeypatch, conn, tmp_path):
    """Answer a decision and verify DB update and decisions.md append."""
    state = tmp_path / "state"
    monkeypatch.setenv("TEAM_STATE", str(state))

    d_id = decisions.create(conn, "team", "Use Rust?", "Performance", ["yes", "no"], "yes")
    decisions.answer(conn, d_id, "Yes, we'll use Rust for the hot path.")

    # DB updated
    row = conn.execute("SELECT * FROM decisions WHERE id = ?", (d_id,)).fetchone()
    assert row["status"] == "answered"
    assert row["answer"] == "Yes, we'll use Rust for the hot path."
    assert row["answered_at"] is not None

    # Log file created with correct format
    log = state / "targets" / "team" / "decisions.md"
    content = log.read_text()
    assert "## " in content
    assert "Use Rust?" in content
    assert "Yes, we'll use Rust for the hot path." in content


def test_answer_creates_log_file_on_first_answer(monkeypatch, conn, tmp_path):
    """First answer creates the decisions.md file, never an empty one."""
    state = tmp_path / "state"
    monkeypatch.setenv("TEAM_STATE", str(state))

    d_id = decisions.create(conn, "team", "Q", "C", ["a", "b"], "a")
    assert not decisions.log_path("team").exists()

    decisions.answer(conn, d_id, "Answer text")
    log = decisions.log_path("team")
    assert log.exists()
    content = log.read_text()
    assert "Q" in content
    assert "Answer text" in content
    assert content.strip() != ""


def test_answer_multiple_decisions_appends(monkeypatch, conn, tmp_path):
    """Multiple answers append to the same decisions.md."""
    state = tmp_path / "state"
    monkeypatch.setenv("TEAM_STATE", str(state))

    d1 = decisions.create(conn, "team", "Q1", "C", ["a", "b"], "a")
    d2 = decisions.create(conn, "team", "Q2", "C", ["a", "b"], "a")
    decisions.answer(conn, d1, "A1")
    decisions.answer(conn, d2, "A2")

    content = decisions.log_path("team").read_text()
    # Both questions and answers present
    assert "Q1" in content and "A1" in content
    assert "Q2" in content and "A2" in content
    # Two headers
    assert content.count("## ") == 2


def test_dismiss_decision(conn):
    """Dismiss a decision."""
    d_id = decisions.create(conn, "team", "Q", "C", ["a", "b"], "a")
    decisions.dismiss(conn, d_id)

    row = conn.execute("SELECT * FROM decisions WHERE id = ?", (d_id,)).fetchone()
    assert row["status"] == "dismissed"
    assert row["answer"] == ""
    assert row["answered_at"] is not None


def test_answer_nonexistent_raises(conn):
    """Answering a non-existent decision raises LookupError."""
    with pytest.raises(LookupError):
        decisions.answer(conn, 999, "answer")


def test_answer_already_answered_raises(conn):
    """Answering an already answered decision raises ValueError."""
    d_id = decisions.create(conn, "team", "Q", "C", ["a", "b"], "a")
    decisions.answer(conn, d_id, "A")
    with pytest.raises(ValueError):
        decisions.answer(conn, d_id, "A again")


def test_dismiss_nonexistent_raises(conn):
    """Dismissing a non-existent decision raises LookupError."""
    with pytest.raises(LookupError):
        decisions.dismiss(conn, 999)


def test_dismiss_already_answered_raises(conn):
    """Dismissing an already answered decision raises ValueError."""
    d_id = decisions.create(conn, "team", "Q", "C", ["a", "b"], "a")
    decisions.answer(conn, d_id, "A")
    with pytest.raises(ValueError):
        decisions.dismiss(conn, d_id)


def test_paused_goals(conn):
    """paused_goals returns goal_ids with open decisions."""
    decisions.create(conn, "team", "Q1", "C", ["a", "b"], "a", goal_id=10)
    decisions.create(conn, "team", "Q2", "C", ["a", "b"], "a", goal_id=20)
    decisions.create(conn, "team", "Q3", "C", ["a", "b"], "a", goal_id=30)
    decisions.create(conn, "brain", "Q4", "C", ["a", "b"], "a", goal_id=40)

    # Answer one
    d2 = conn.execute("SELECT id FROM decisions WHERE goal_id = 20").fetchone()["id"]
    decisions.answer(conn, d2, "Done")

    paused = decisions.paused_goals(conn, "team")
    assert paused == {10, 30}


def test_init_migrates_old_decisions_table(monkeypatch, tmp_path):
    """init() adds missing columns to an old decisions table."""
    c = ledger.connect(tmp_path / "old.db")
    # Create old schema without task_id and source
    c.executescript("""
        CREATE TABLE jobs (id INTEGER PRIMARY KEY, target TEXT, target_commit TEXT,
            role TEXT, role_version TEXT, attempt INTEGER, started TEXT, ended TEXT,
            outcome TEXT, grader TEXT, grader_detail TEXT);
        CREATE TABLE calls (id INTEGER PRIMARY KEY, job_id INTEGER, provider TEXT,
            model TEXT, task_class TEXT, tokens_in INTEGER, tokens_out INTEGER,
            latency_ms INTEGER, ok INTEGER, error TEXT, ts TEXT);
        CREATE TABLE decisions (id INTEGER PRIMARY KEY, target TEXT, created TEXT,
            question TEXT, context TEXT, options TEXT, recommendation TEXT,
            goal_id INTEGER, status TEXT, answer TEXT, answered_at TEXT);
    """)
    c.execute("INSERT INTO decisions (target, created, question, context, options, recommendation, goal_id, status) VALUES ('team', '2026-01-01T00:00:00', 'Q', 'C', '[]', 'r', 1, 'open')")
    c.commit()

    ledger.init(c)
    ledger.init(c)  # idempotent

    cols = {r["name"] for r in c.execute("PRAGMA table_info(decisions)")}
    assert "task_id" in cols
    assert "source" in cols

    # Existing row preserved with default source
    row = c.execute("SELECT * FROM decisions WHERE id = 1").fetchone()
    assert row["target"] == "team"
    assert row["source"] == "overseer"
    assert row["task_id"] is None
    c.close()


def test_create_with_defaults(conn):
    """create() uses default source='overseer' and no goal_id/task_id."""
    d_id = decisions.create(conn, "team", "Q", "C", ["a", "b"], "a")
    row = conn.execute("SELECT * FROM decisions WHERE id = ?", (d_id,)).fetchone()
    assert row["source"] == "overseer"
    assert row["goal_id"] is None
    assert row["task_id"] is None