"""Tests for team/flow.py: goals through the team, from a temp TEAM_STATE."""

from __future__ import annotations

import json
import sqlite3
import time

import pytest

from team import flow, ledger

NOW = time.time()

ROLES = [
    {"name": "boss", "title": "Foreman", "status": "active", "runs_on": {"harness": "hermes-subscription", "model": "opus"}},
    {"name": "spec", "title": "Spec", "status": "planned", "runs_on": {"harness": "hermes", "model": "team-auto"}},
    {"name": "implement", "title": "Implement", "status": "active", "runs_on": {"harness": "hermes", "model": "team-auto"}},
]
PIPELINE = ["boss", "spec", "implement"]


def _board(path, tasks, links=()):
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT, status TEXT NOT NULL, assignee TEXT,"
        " model_override TEXT, worker_pid INTEGER, started_at INTEGER, completed_at INTEGER, branch_name TEXT,"
        " last_failure_error TEXT, block_kind TEXT, created_at INTEGER)"
    )
    conn.execute("CREATE TABLE task_links (parent_id TEXT, child_id TEXT)")
    for t in tasks:
        conn.execute(
            "INSERT INTO tasks (id, title, status, assignee, model_override, worker_pid, started_at, block_kind, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (t["id"], t.get("title", t["id"]), t["status"], "team-implement", t.get("model"), t.get("pid"),
             t.get("started_at"), t.get("block_kind"), int(NOW)),
        )
    for parent, child in links:
        conn.execute("INSERT INTO task_links VALUES (?, ?)", (parent, child))
    conn.commit()
    conn.close()


@pytest.fixture
def state(tmp_path, monkeypatch):
    """Four goals on `team`: landed, running, queued and blocked, plus one failed; one plan."""
    root = tmp_path / "state"
    monkeypatch.setenv("TEAM_STATE", str(root))
    root.mkdir()
    conn = ledger.connect(root / "team.db")
    ledger.init(conn)
    # 1: landed, attempt 2, free model known from the ledger
    g1 = ledger.create_goal(conn, "team", "Add the widget\n\nIt draws a box.", "implement", "team/w", "/wt/w", "uv run pytest -q")
    j1 = ledger.start_job(conn, "team", "abc", "implement", "v1")
    ledger.set_task_ref(conn, j1, "t_1")
    ledger.record_call(conn, j1, "openrouter", "nvidia/nemotron-3-ultra-550b-a55b:free", "implement", 10, 5, None, True)
    ledger.finish_job(conn, j1, "pass", "pytest")
    ledger.update_goal(conn, g1, attempt=2, current_task="t_1", job_id=j1)
    ledger.finish_goal(conn, g1, "pass", "landed")
    # 2: running on the proxy, from a plan
    plan_id = "11111111-1111-1111-1111-111111111111"
    g2 = ledger.create_goal(conn, "team", f"Team plan task: {plan_id}:T2\nWire the API\n\nDetails here.\n\nTeam scoped assignment:\n{{}}",
                            "implement", "team/api", "/wt/api", "uv run pytest -q")
    j2 = ledger.start_job(conn, "team", "abc", "implement", "v1")
    ledger.set_task_ref(conn, j2, "t_2")
    ledger.update_goal(conn, g2, current_task="t_2", job_id=j2)
    # 3: queued (task exists, not started)
    g3 = ledger.create_goal(conn, "team", f"Team plan task: {plan_id}:T3\nAdd the tests", "implement", "team/t", "/wt/t", "uv run pytest -q")
    ledger.update_goal(conn, g3, current_task="t_3")
    # 4: blocked
    g4 = ledger.create_goal(conn, "team", "Rename things", "implement", "team/r", "/wt/r", "uv run pytest -q")
    ledger.update_goal(conn, g4, current_task="t_4")
    # 5: failed, no task on the board any more
    g5 = ledger.create_goal(conn, "team", "Old failure", "implement", "team/o", "/wt/o", "uv run pytest -q")
    ledger.finish_goal(conn, g5, "fail", "gave up")
    conn.close()
    _board(
        flow.board_db("team"),
        [
            {"id": "t_1", "status": "done", "model": "nvidia/nemotron-3-ultra-550b-a55b:free"},
            {"id": "t_2", "status": "running", "model": "team-auto", "pid": 4242, "started_at": int(NOW) - 120},
            {"id": "t_3", "status": "todo", "model": "team-auto"},
            {"id": "t_4", "status": "blocked", "model": "team-auto", "block_kind": "needs_input"},
        ],
        links=[("t_2", "t_3")],
    )
    plans = root / "planning"
    plans.mkdir()
    (plans / f"{plan_id}.json").write_text(json.dumps({
        "id": plan_id, "target": "team", "objective": "Ship the widget API", "revision": 3,
        "created_at": "2026-10-26T10:00:00+00:00", "updated_at": "2026-10-26T11:00:00+00:00",
        "contract": {"tasks": [
            {"id": "T1", "title": "Schema", "role": "implement", "depends_on": [], "files": ["a.py"]},
            {"id": "T2", "title": "Wire the API", "role": "implement", "depends_on": [], "files": ["b.py"]},
            {"id": "T3", "title": "Add the tests", "role": "implement", "depends_on": ["T2"], "files": ["c.py"]},
            {"id": "T4", "title": "Docs", "role": "implement", "depends_on": ["T1", "T3"], "files": ["d.md"]},
        ]},
        "task_goals": {"T2": g2, "T3": g3},
    }))
    (plans / "junk.json").write_text("not json")
    return root


def _snap(state):
    conn = ledger.connect(state / "team.db")
    try:
        return flow.snapshot(conn, ROLES, PIPELINE)
    finally:
        conn.close()


class TestGoals:
    def test_stages_and_tracks(self, state):
        d = _snap(state)
        by_id = {c["id"]: c for c in d["goals"]}
        assert by_id[1]["stage"] == "landed"
        assert [t["state"] for t in by_id[1]["track"]] == ["done", "done", "done"]
        assert by_id[2]["stage"] == "running"
        assert [(t["station"], t["state"]) for t in by_id[2]["track"]] == [("boss", "done"), ("implement", "current"), ("landed", "todo")]
        assert by_id[3]["stage"] == "waiting"  # its parent t_2 is still running
        assert by_id[4]["stage"] == "blocked" and by_id[4]["block_kind"] == "needs_input"
        assert by_id[5]["stage"] == "failed"
        assert [t["state"] for t in by_id[5]["track"]] == ["done", "failed", "todo"]

    def test_title_and_description_strip_plan_marker_and_assignment(self, state):
        d = _snap(state)
        by_id = {c["id"]: c for c in d["goals"]}
        assert by_id[2]["title"] == "Wire the API"
        assert by_id[2]["description"] == "Details here."
        assert by_id[2]["plan_id"] == "11111111-1111-1111-1111-111111111111" and by_id[2]["plan_task"] == "T2"
        assert by_id[1]["title"] == "Add the widget" and by_id[1]["description"] == "It draws a box."
        assert by_id[1]["plan_id"] is None

    def test_models(self, state):
        d = _snap(state)
        by_id = {c["id"]: c for c in d["goals"]}
        assert by_id[2]["model"] == {"model": "team-auto", "source": "task", "live": True, "behind_proxy": None}
        assert by_id[1]["model"] == {"model": "nvidia/nemotron-3-ultra-550b-a55b:free", "source": "ledger", "live": False}
        assert by_id[5]["model"] == {"model": "team-auto", "source": "role", "live": False}

    def test_proxy_resolves_the_real_model_behind_team_auto(self, state):
        usage = state / "litellm" / "usage.db"
        usage.parent.mkdir()
        conn = sqlite3.connect(usage)
        conn.execute("CREATE TABLE calls (id INTEGER PRIMARY KEY, ts TEXT, session TEXT, tier TEXT, model TEXT, ok INTEGER)")
        conn.execute("INSERT INTO calls VALUES (1, '2026-10-26T18:00:00+00:00', 't_2', 'MEDIUM', 'nvidia_nim/poolside/laguna-xs-2.1', 1)")
        conn.execute("INSERT INTO calls VALUES (2, '2026-10-26T18:01:00+00:00', 't_2', 'SIMPLE', 'nvidia_nim/openai/gpt-oss-20b', 1)")
        conn.execute("INSERT INTO calls VALUES (3, '2026-10-26T18:02:00+00:00', 't_2', 'MEDIUM', 'nvidia_nim/x/failed', 0)")
        conn.execute("INSERT INTO calls VALUES (4, '2026-10-26T18:03:00+00:00', 't_9', 'MEDIUM', 'other', 1)")
        conn.commit()
        conn.close()
        d = _snap(state)
        by_id = {c["id"]: c for c in d["goals"]}
        assert by_id[2]["model"]["behind_proxy"] == {"model": "nvidia_nim/openai/gpt-oss-20b", "tier": "SIMPLE", "ts": "2026-10-26T18:01:00+00:00"}
        assert d["agents"][2]["live_model"] == "nvidia_nim/openai/gpt-oss-20b"

    def test_task_details(self, state):
        d = _snap(state)
        by_id = {c["id"]: c for c in d["goals"]}
        assert by_id[2]["worker_pid"] == 4242 and by_id[2]["task_started"]
        assert by_id[3]["parents"] == ["t_2"]
        assert by_id[1]["attempt"] == 2
        assert [j["outcome"] for j in by_id[1]["jobs"]] == ["pass"]

    def test_counts(self, state):
        d = _snap(state)
        assert d["counts"] == {"made": 5, "waiting": 1, "queued": 0, "running": 1, "review": 0, "blocked": 1, "landed": 1, "failed": 1}

    def test_queued_once_the_parent_is_done(self, state):
        conn = sqlite3.connect(flow.board_db("team"))
        conn.execute("UPDATE tasks SET status='done' WHERE id='t_2'")
        conn.commit()
        conn.close()
        d = _snap(state)
        by_id = {c["id"]: c for c in d["goals"]}
        assert by_id[3]["stage"] == "queued"


class TestAgents:
    def test_agents_follow_pipeline_order_with_goals_by_stage(self, state):
        d = _snap(state)
        assert [a["name"] for a in d["agents"]] == ["boss", "spec", "implement"]
        assert d["stations"] == ["boss", "spec", "implement", "landed"]
        impl = d["agents"][2]
        assert [c["id"] for c in impl["running"]] == [2]
        assert [c["id"] for c in impl["queued"]] == [3]  # waiting counts as queued, in dispatch order
        assert [c["id"] for c in impl["blocked"]] == [4]
        assert [c["id"] for c in impl["done"]] == [5, 1]  # newest first
        assert impl["model"] == "team-auto" and impl["live_model"] == "team-auto"
        assert d["agents"][0]["live_model"] is None

    def test_role_outside_roster_still_listed(self, state):
        conn = ledger.connect(state / "team.db")
        ledger.create_goal(conn, "team", "Built by a builder", "builder", "team/b", "/wt/b", "true")
        conn.close()
        d = _snap(state)
        names = [a["name"] for a in d["agents"]]
        assert names == ["boss", "spec", "implement", "builder"]
        assert d["agents"][3]["status"] == "unknown"


class TestPlans:
    def test_plan_tasks_waves_and_stages(self, state):
        d = _snap(state)
        [p] = d["plans"]
        assert p["objective"] == "Ship the widget API" and p["dispatched"] == 2
        tasks = {t["id"]: t for t in p["tasks"]}
        assert tasks["T1"]["stage"] == "planned"
        assert tasks["T2"]["stage"] == "running" and tasks["T2"]["model"] == "team-auto"
        assert tasks["T3"]["stage"] == "waiting"
        assert tasks["T4"]["stage"] == "waiting"  # planned, but T1 and T3 are not landed
        # T1 and T2 can run together; T3 waits for T2; T4 waits for both.
        assert p["waves"] == [["T1", "T2"], ["T3"], ["T4"]]

    def test_parallel_groups_handles_unknown_deps_and_cycles(self):
        tasks = [{"id": "a", "depends_on": ["zzz"]}, {"id": "b", "depends_on": ["c"]}, {"id": "c", "depends_on": ["b"]}]
        assert flow.parallel_groups(tasks) == [["a"], ["b", "c"]]
        assert flow.parallel_groups([]) == []


class TestEmptyAndFingerprint:
    def test_no_ledger(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TEAM_STATE", str(tmp_path / "empty"))
        d = flow.snapshot(None, ROLES, PIPELINE)
        assert d["goals"] == [] and d["plans"] == []
        assert [a["name"] for a in d["agents"]] == ["boss", "spec", "implement"]

    def test_no_boards_or_plans(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TEAM_STATE", str(tmp_path / "empty"))
        assert flow.read_tasks("team") == {}
        assert flow.read_plans() == []
        assert flow.fingerprint() == ""

    def test_fingerprint_changes_with_board_and_plans(self, state):
        a = flow.fingerprint()
        assert a
        conn = sqlite3.connect(flow.board_db("team"))
        conn.execute("UPDATE tasks SET status='done' WHERE id='t_2'")
        conn.commit()
        conn.close()
        b = flow.fingerprint()
        assert b != a
        (state / "planning" / "22222222-2222-2222-2222-222222222222.json").write_text(json.dumps({"id": "x"}))
        assert flow.fingerprint() != b
