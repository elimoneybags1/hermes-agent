import sqlite3
from datetime import datetime, timedelta, timezone, UTC

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


def test_init_creates_every_table_with_the_agreed_columns(conn):
    def cols(table):
        return [r["name"] for r in conn.execute(f"PRAGMA table_info({table})")]

    assert cols("jobs") == [
        "id", "target", "target_commit", "role", "role_version", "attempt",
        "started", "ended", "outcome", "grader", "grader_detail", "task_ref",
        "model_choice", "part_id",
    ]
    assert cols("calls") == [
        "id", "job_id", "task_ref", "session_id", "profile", "provider", "model",
        "response_model", "base_url", "task_class", "tokens_in", "tokens_out",
        "latency_ms", "ok", "error", "ts", "api_calls",
    ]
    assert cols("goals")[-3:] == ["part_id", "base", "needs_commits"]
    assert cols("decisions") == [
        "id", "target", "created", "question", "context", "options", "recommendation",
        "goal_id", "task_id", "source", "status", "answer", "answered_at",
    ]
    assert cols("plans") == ["id", "target", "title", "created", "updated", "status", "path", "summary"]
    assert cols("parts") == [
        "id", "plan_id", "target", "title", "stage", "branch", "depends_on",
        "owned_paths", "status", "created", "finished",
    ]
    assert cols("reviews") == ["id", "target", "created", "source", "summary",
                               "decisions_created", "actions", "drift"]
    assert cols("pauses") == ["id", "target", "until", "reason", "created"]
    assert cols("violations") == ["id", "ts", "target", "task_ref", "kind", "detail"]
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_init_is_idempotent(conn):
    job = ledger.start_job(conn, "team", "abc123", "implement", "def456")
    ledger.init(conn)
    assert [j["id"] for j in ledger.list_jobs(conn)] == [job]


def test_job_lifecycle_with_model_choice(conn):
    choice = {"spec": "tier-medium", "provider": "litellm", "model": "tier-medium", "why": "first candidate"}
    job = ledger.start_job(conn, "team", "abc123", "implement", "def456", attempt=2,
                           task_ref="t_1", model_choice=choice)
    [row] = ledger.list_jobs(conn)
    assert row["id"] == job and row["attempt"] == 2
    assert row["model_choice"] == choice
    assert row["ended"] is None and row["outcome"] is None

    ledger.finish_job(conn, job, "pass", "check", "12 passed")
    row = ledger.get_job(conn, job)
    assert row["outcome"] == "pass" and row["grader"] == "check"
    assert row["grader_detail"] == "12 passed"
    assert row["ended"] >= row["started"]
    assert ledger.pass_rate(conn, "team") == (1, 1)
    assert ledger.pass_rate(conn, "team", role="review") == (0, 0)


def test_finish_job_rejects_unknown_outcome_and_double_finish(conn):
    job = ledger.start_job(conn, "team", "abc", "implement", "v1")
    with pytest.raises(ValueError):
        ledger.finish_job(conn, job, "passed", "check")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE jobs SET outcome = 'maybe' WHERE id = ?", (job,))
    ledger.finish_job(conn, job, "fail", "check")
    with pytest.raises(LookupError):
        ledger.finish_job(conn, job, "pass", "check")
    with pytest.raises(LookupError):
        ledger.finish_job(conn, 999, "pass", "check")


def test_open_job_for_task(conn):
    a = ledger.start_job(conn, "team", "c", "implement", "v", task_ref="t_1")
    ledger.finish_job(conn, a, "fail", "check")
    b = ledger.start_job(conn, "team", "c", "implement", "v", attempt=2, task_ref="t_1")
    assert ledger.open_job_for_task(conn, "t_1")["id"] == b
    assert ledger.open_job_for_task(conn, "t_9") is None


def test_record_call_without_a_job_then_attach(conn):
    cid = ledger.record_call(conn, None, "litellm", "tier-medium", "implement", 120, 8, 340, True,
                             task_ref="t_1", session_id="s1", profile="implement",
                             response_model="z-ai/glm-5.3", base_url="http://127.0.0.1:4000/v1",
                             api_calls=1)
    [row] = ledger.calls_for(conn, task_ref="t_1")
    assert row["id"] == cid and row["job_id"] is None
    assert row["response_model"] == "z-ai/glm-5.3" and row["profile"] == "implement"
    job = ledger.start_job(conn, "team", "c", "implement", "v", task_ref="t_1")
    assert ledger.attach_calls(conn, "t_1", job) == 1
    assert ledger.calls_for(conn, job_id=job)[0]["id"] == cid
    assert ledger.attach_calls(conn, "t_1", job) == 0


def test_record_call_sets_utc_ts_and_requires_existing_job(conn):
    job = ledger.start_job(conn, "team", "abc", "implement", "v1")
    ledger.record_call(conn, job, "litellm", "m", "implement", 1, 1, 1, True)
    [row] = conn.execute("SELECT ts FROM calls").fetchall()
    ts = datetime.fromisoformat(row["ts"])
    assert ts.utcoffset() == timedelta(0) and ts.microsecond == 0
    assert abs(datetime.now(UTC) - ts) < timedelta(minutes=1)
    with pytest.raises(sqlite3.IntegrityError):
        ledger.record_call(conn, 999, "litellm", "m", "implement", 1, 1, 1, True)
    with pytest.raises(ValueError):
        ledger.calls_for(conn)


def test_list_jobs_filters_and_orders(conn):
    a = ledger.start_job(conn, "team", "c1", "implement", "v1")
    b = ledger.start_job(conn, "brain", "c2", "implement", "v1")
    c = ledger.start_job(conn, "team", "c3", "review", "v1")
    assert [j["id"] for j in ledger.list_jobs(conn)] == [c, b, a]
    assert [j["id"] for j in ledger.list_jobs(conn, target="team")] == [c, a]
    assert [j["id"] for j in ledger.list_jobs(conn, target="team", role="implement")] == [a]
    assert [j["id"] for j in ledger.list_jobs(conn, limit=1)] == [c]


def test_goal_lifecycle(conn):
    job = ledger.start_job(conn, "team", "c", "implement", "v", task_ref="t_1")
    goal = ledger.create_goal(conn, "team", "do x", "implement", "team/x", "/wt/x", "pytest")
    assert ledger.get_goal(conn, goal)["base"] == "main"
    assert ledger.get_goal(conn, goal)["needs_commits"] == 1
    ledger.update_goal(conn, goal, attempt=2, current_task="t_1", job_id=job)
    [active] = ledger.active_goals(conn, "team")
    assert (active["attempt"], active["current_task"], active["job_id"]) == (2, "t_1", job)
    assert ledger.active_goals(conn, "other") == []
    with pytest.raises(ValueError):
        ledger.update_goal(conn, goal, branch="nope")
    ledger.finish_goal(conn, goal, "pass", "landed abc")
    assert ledger.active_goals(conn) == []
    assert ledger.list_goals(conn) == []
    assert ledger.list_goals(conn, all_goals=True)[0]["detail"] == "landed abc"
    with pytest.raises(LookupError):
        ledger.finish_goal(conn, goal, "fail")


def test_decisions_inbox_and_paused_goals(conn):
    goal = ledger.create_goal(conn, "team", "do x", "implement", "team/x", "/wt/x", "pytest")
    d = ledger.create_decision(conn, "team", "Trail by % or ATR?", "entries use ATR(14)",
                               ["%", "ATR"], "ATR", goal_id=goal, task_id="t_1", source="boss")
    [open_] = ledger.list_decisions(conn, "team")
    assert open_["id"] == d and open_["options"] == ["%", "ATR"] and open_["status"] == "open"
    assert ledger.paused_goals(conn, "team") == {goal}
    ledger.answer_decision(conn, d, "ATR, log every adjustment")
    assert ledger.list_decisions(conn, "team") == []
    assert ledger.get_decision(conn, d)["answer"] == "ATR, log every adjustment"
    assert ledger.paused_goals(conn, "team") == set()
    with pytest.raises(LookupError):
        ledger.dismiss_decision(conn, d)
    e = ledger.create_decision(conn, "team", "q", "c", ["a"], "a")
    ledger.dismiss_decision(conn, e)
    assert [x["status"] for x in ledger.list_decisions(conn, status=None)] == ["answered", "dismissed"]
    with pytest.raises(ValueError):
        ledger.list_decisions(conn, status="maybe")


def test_plans_parts_and_reviews(conn):
    plan = ledger.create_plan(conn, "team", "trailing stops", path="plan.md", summary="s")
    part = ledger.create_part(conn, plan, "team", "ATR trail", depends_on=[], owned_paths=["a.py"])
    p = ledger.get_part(conn, part)
    assert p["stage"] == "implement" and p["owned_paths"] == ["a.py"] and p["status"] == "open"
    second = ledger.create_part(conn, plan, "team", "apply to open positions", depends_on=[part])
    assert [x["id"] for x in ledger.list_parts(conn, plan_id=plan)] == [part, second]
    assert ledger.list_parts(conn, target="other") == []
    ledger.set_part_stage(conn, part, "review")
    ledger.set_part_stage(conn, part, "done", status="landed")
    p = ledger.get_part(conn, part)
    assert p["stage"] == "done" and p["status"] == "landed" and p["finished"]
    with pytest.raises(ValueError):
        ledger.set_part_stage(conn, part, "shipping")
    with pytest.raises(LookupError):
        ledger.set_part_stage(conn, 999, "done")

    goal = ledger.create_goal(conn, "team", "ATR trail", "implement", "team/p1", "/wt", "pytest",
                              part_id=part, base="team/part-1", needs_commits=False)
    g = ledger.get_goal(conn, goal)
    assert (g["part_id"], g["base"], g["needs_commits"]) == (part, "team/part-1", 0)

    ledger.update_plan(conn, plan, status="active")
    assert ledger.list_plans(conn, "team")[0]["status"] == "active"
    with pytest.raises(LookupError):
        ledger.update_plan(conn, 999, status="done")

    rid = ledger.add_review(conn, "team", "overseer", "all good", decisions_created=[1],
                            actions=[{"kind": "comment", "task_id": "t_1"}], drift="none")
    [r] = ledger.list_reviews(conn, "team", limit=5)
    assert r["id"] == rid and r["actions"][0]["kind"] == "comment" and r["decisions_created"] == [1]


def test_pauses(conn):
    now = 2_000_000_000.0
    assert ledger.active_pause(conn, "team", now) is None
    ledger.add_pause(conn, "team", now + 60, "free quota")
    ledger.add_pause(conn, "team", now + 3600, "free quota")
    ledger.add_pause(conn, "other", now - 1, "over")
    pause = ledger.active_pause(conn, "team", now)
    assert pause["until"] == "2033-05-18T04:33:20+00:00"
    assert pause["reason"] == "free quota"
    assert ledger.active_pause(conn, "team", now + 3600) is None
    assert ledger.active_pause(conn, "other", now) is None


def test_violations(conn):
    v = ledger.record_violation(conn, "non-catalog", "model gpt-5 is not in the catalog",
                                target="team", task_ref="t_1")
    [row] = ledger.violations_for(conn, "t_1")
    assert row["id"] == v and row["kind"] == "non-catalog" and row["ts"]
    assert ledger.violations_for(conn, "t_2") == []
    with pytest.raises(ValueError):
        ledger.record_violation(conn, "bad-vibes", "x")
