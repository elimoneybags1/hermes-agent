"""Tests for team/runtime.py: the Hermes runtime picture, read from a temp TEAM_STATE."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest

from team import ledger, runtime

NOW = time.time()


def _make_sessions_db(path: Path, rows: list[dict]) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT NOT NULL, model TEXT, title TEXT,"
        " started_at REAL NOT NULL, ended_at REAL, end_reason TEXT, message_count INTEGER DEFAULT 0,"
        " tool_call_count INTEGER DEFAULT 0, api_call_count INTEGER DEFAULT 0, input_tokens INTEGER DEFAULT 0,"
        " output_tokens INTEGER DEFAULT 0, last_activity_at REAL, last_activity_description TEXT,"
        " cwd TEXT, git_branch TEXT, parent_session_id TEXT, profile_name TEXT)"
    )
    for r in rows:
        cols = ", ".join(r)
        marks = ", ".join("?" for _ in r)
        conn.execute(f"INSERT INTO sessions ({cols}) VALUES ({marks})", list(r.values()))
    conn.commit()
    conn.close()


@pytest.fixture
def state(tmp_path, monkeypatch):
    """A TEAM_STATE with one Hermes profile (two sessions), two proposals and a ledger."""
    root = tmp_path / "state"
    monkeypatch.setenv("TEAM_STATE", str(root))
    home = root / "hermes" / "profiles" / "optimizer"
    (home / "logs").mkdir(parents=True)
    (home / "memories").mkdir()
    (home / "skills" / "team" / "evidence-to-proposal").mkdir(parents=True)
    (home / "skills" / "team" / "evidence-to-proposal" / "SKILL.md").write_text("# skill\n")
    (home / "skills" / "web").mkdir()
    (home / "memories" / "notes.md").write_text("lesson\n")
    (home / "config.yaml").write_text("model:\n  provider: custom\n  default: opus\n")
    (home / "logs" / "agent.log").write_text("first line\n2026-10-26 INFO agent: Turn ended\n")
    (home / "logs" / "errors.log").write_text("2026-10-26 WARNING tool error\n")
    _make_sessions_db(
        home / "state.db",
        [
            {
                "id": "aaaa1111-0000-0000-0000-000000000001", "source": "cli", "model": "opus",
                "title": "Older, finished", "started_at": NOW - 7200, "ended_at": NOW - 7000,
                "end_reason": "agent_close", "message_count": 10, "tool_call_count": 4,
                "api_call_count": 3, "input_tokens": 1000, "output_tokens": 200,
                "last_activity_at": NOW - 7000, "cwd": "/repo", "profile_name": "optimizer",
            },
            {
                "id": "bbbb2222-0000-0000-0000-000000000002", "source": "cli", "model": "opus",
                "title": "Running now", "started_at": NOW - 60, "ended_at": None,
                "message_count": 2, "tool_call_count": 1, "api_call_count": 1,
                "input_tokens": 50, "output_tokens": 5, "last_activity_at": NOW - 10,
                "last_activity_description": "reading the ledger", "profile_name": "optimizer",
            },
        ],
    )
    props = root / "optimizer" / "proposals"
    props.mkdir(parents=True)
    first = {
        "id": "11111111-1111-1111-1111-111111111111", "created_at": "2026-10-26T16:35:43+00:00",
        "status": "proposed", "subject": "first idea", "scope": "implement", "location": "team/run.py",
        "before": "b", "after": "a", "reason": "r", "validation": "v", "uncertainty": "u",
        "evidence_ids": ["job:1"], "evidence": [{"id": "job:1", "data": {"big": "payload"}}],
    }
    second = {**first, "id": "22222222-2222-2222-2222-222222222222", "created_at": "2026-10-26T16:39:12+00:00",
              "subject": "second idea", "supersedes": first["id"], "evidence": [{"id": "job:1"}, {"id": "job:2"}]}
    (props / f"{first['id']}.json").write_text(json.dumps(first))
    (props / f"{second['id']}.json").write_text(json.dumps(second))
    conn = ledger.connect(root / "team.db")
    ledger.init(conn)
    job = ledger.start_job(conn, "team", "abc", "implement", "v1")
    ledger.record_call(conn, job, "claude-code-subscription", "claude", "implement", 52, 31660, 376583, True, api_calls=7)
    ledger.record_call(conn, job, "openrouter", "nvidia/nemotron-3-ultra-550b-a55b:free", "implement", 1000, 100, None, True)
    ledger.record_call(conn, job, "openrouter", "nvidia/nemotron-3-ultra-550b-a55b:free", "implement", None, None, None, False, error="quota")
    conn.close()
    return root


class TestTeamRun:
    @pytest.mark.parametrize(
        "argv",
        [
            [12, "team", "run"],
            [13, "uv", "run", "team", "run"],
            [14, "/Users/x/work/Team/.venv/bin/team", "run", "--once"],
            [15, "python", "-m", "team", "run"],
        ],
    )
    def test_matches_team_run(self, argv):
        assert runtime.team_run([argv]) == {"alive": True, "pids": [argv[0]]}

    @pytest.mark.parametrize(
        "argv",
        [
            [20, "uv", "run", "--directory", "/x/Team", "team", "ui", "--no-open"],
            [21, "team", "deck"],
            [22, "hermes", "chat", "--query-file", "run.md"],
            [23, "team"],
        ],
    )
    def test_ignores_other_commands(self, argv):
        assert runtime.team_run([argv]) == {"alive": False, "pids": []}

    def test_live_scan_runs(self):
        status = runtime.team_run()
        assert set(status) == {"alive", "pids"}
        assert isinstance(status["alive"], bool)


class TestProfiles:
    def test_profile_status(self, state):
        [p] = runtime.profiles()
        assert p["role"] == "optimizer"
        assert p["model"] == {"provider": "custom", "default": "opus"}
        assert p["memories"] == 1
        assert p["team_skills"] == ["evidence-to-proposal"]
        s = p["sessions"]
        assert s["error"] is None
        assert (s["count"], s["active"]) == (2, 1)
        # Newest activity first; the running one is active and has no end.
        assert [x["title"] for x in s["items"]] == ["Running now", "Older, finished"]
        running, done = s["items"]
        assert running["active"] is True and running["ended_at"] is None
        assert running["last_activity"] == "reading the ledger"
        assert done["active"] is False and done["end_reason"] == "agent_close"
        assert done["started_at"].endswith("+00:00")
        assert p["logs"]["agent.log"]["last"] == "2026-10-26 INFO agent: Turn ended"
        assert p["logs"]["errors.log"]["last"] == "2026-10-26 WARNING tool error"
        assert p["logs"]["agent.log"]["mtime"]

    def test_session_limit(self, state):
        [p] = runtime.profiles(session_limit=1)
        assert p["sessions"]["count"] == 2
        assert len(p["sessions"]["items"]) == 1

    def test_profile_without_sessions_or_logs(self, state):
        home = state / "hermes" / "profiles" / "boss"
        home.mkdir()
        (home / "config.yaml").write_text("model:\n  default: opus\n")
        boss = [p for p in runtime.profiles() if p["role"] == "boss"][0]
        assert boss["sessions"] == {"count": 0, "active": 0, "items": [], "error": None}
        assert boss["logs"] == {"agent.log": None, "errors.log": None}
        assert boss["memories"] == 0 and boss["team_skills"] == []
        assert boss["model"] == {"provider": None, "default": "opus"}

    def test_broken_sessions_db_is_reported_not_raised(self, state):
        (state / "hermes" / "profiles" / "optimizer" / "state.db").write_text("not a database")
        [p] = runtime.profiles()
        assert p["sessions"]["items"] == []
        assert p["sessions"]["error"]

    def test_no_profiles_dir(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TEAM_STATE", str(tmp_path / "empty"))
        assert runtime.profiles() == []
        assert runtime.proposals_summary() == []
        assert runtime.fingerprint() == ""


class TestProposals:
    def test_newest_first_without_evidence_payload(self, state):
        items = runtime.proposals_summary()
        assert [p["subject"] for p in items] == ["second idea", "first idea"]
        assert all("evidence" not in p for p in items)
        assert [p["evidence_count"] for p in items] == [2, 1]

    def test_supersession_status(self, state):
        second, first = runtime.proposals_summary()
        assert first["status"] == "superseded"
        assert first["superseded_by"] == second["id"]
        assert second["status"] == "proposed"
        assert second["supersedes"] == first["id"]


class TestCalls:
    def test_grouped_by_provider_in_window(self, state):
        conn = ledger.connect(state / "team.db")
        try:
            summary = runtime.calls_summary(conn)
        finally:
            conn.close()
        assert summary["window_h"] == 24
        by = {(p["provider"], p["model"]): p for p in summary["providers"]}
        free = by[("openrouter", "nvidia/nemotron-3-ultra-550b-a55b:free")]
        assert (free["calls"], free["ok"], free["tokens_in"], free["tokens_out"]) == (2, 1, 1000, 100)
        sub = by[("claude-code-subscription", "claude")]
        assert (sub["calls"], sub["ok"], sub["api_calls"]) == (1, 1, 7)
        assert sub["last_ts"]

    def test_window_excludes_old_calls(self, state):
        conn = ledger.connect(state / "team.db")
        try:
            conn.execute("UPDATE calls SET ts = '2020-01-01T00:00:00+00:00'")
            conn.commit()
            assert runtime.calls_summary(conn)["providers"] == []
        finally:
            conn.close()

    def test_no_ledger(self):
        assert runtime.calls_summary(None) == {"window_h": 24, "providers": []}


class TestSnapshotAndFingerprint:
    def test_snapshot_shape(self, state):
        conn = ledger.connect(state / "team.db")
        try:
            data = runtime.snapshot(conn)
        finally:
            conn.close()
        assert set(data) == {"generated_at", "state_dir", "run", "profiles", "calls", "proposals"}
        assert data["state_dir"] == str(state)
        assert len(data["profiles"]) == 1 and len(data["proposals"]) == 2
        json.dumps(data)  # serializable as the API sends it

    def test_fingerprint_changes_with_sessions_and_proposals(self, state):
        a = runtime.fingerprint()
        assert a
        db = state / "hermes" / "profiles" / "optimizer" / "state.db"
        conn = sqlite3.connect(db)
        conn.execute("INSERT INTO sessions (id, source, started_at) VALUES ('c', 'cli', ?)", (NOW,))
        conn.commit()
        conn.close()
        b = runtime.fingerprint()
        assert b != a
        (state / "optimizer" / "proposals" / "33333333-3333-3333-3333-333333333333.json").write_text("{}")
        assert runtime.fingerprint() != b

    def test_fingerprint_is_stable_when_nothing_changes(self, state):
        assert runtime.fingerprint() == runtime.fingerprint()


class TestCli:
    def test_json(self, state, capsys):
        assert runtime.main(["--json"]) == 0
        data = json.loads(capsys.readouterr().out)
        assert data["profiles"][0]["role"] == "optimizer"
        assert [p["status"] for p in data["proposals"]] == ["proposed", "superseded"]

    def test_text(self, state, capsys):
        assert runtime.main([]) == 0
        out = capsys.readouterr().out
        assert "team run:" in out
        assert "optimizer\tcustom/opus\tsessions 2 (1 active)\tmemories 1\tteam skills 1" in out
        assert "proposal\tproposed" in out
