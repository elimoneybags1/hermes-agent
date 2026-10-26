"""Tests for team/boss_chat.py against the fake boss in tests/fake_boss.py."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

from team import boss_chat, ledger

FAKE = [sys.executable, str(Path(__file__).with_name("fake_boss.py"))]


def _wait(cond, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("TEAM_STATE", str(tmp_path / "state"))
    return tmp_path / "state"


@pytest.fixture
def chat(state, tmp_path):
    c = boss_chat.BossChat(tmp_path, command=FAKE)
    yield c
    c.stop()


def _replies(c):
    return [e["text"] for e in c.transcript if e["who"] == "boss"]


class TestLifecycle:
    def test_start_reads_banner_and_saves_session(self, chat, state):
        st = chat.start()
        assert st["alive"] and not st["busy"] and st["awaiting"] == "you"
        assert st["session_id"] == "fake-session-0001"
        assert json.loads((state / "boss" / "ui-session.json").read_text())["session_id"] == "fake-session-0001"

    def test_start_twice_is_one_process(self, chat):
        chat.start()
        pid = chat.proc.pid
        chat.start()
        assert chat.proc.pid == pid

    def test_stop(self, chat):
        chat.start()
        st = chat.stop()
        assert not st["alive"]
        assert chat.proc is None

    def test_resume_uses_saved_session(self, state, tmp_path):
        (state / "boss").mkdir(parents=True)
        (state / "boss" / "ui-session.json").write_text(json.dumps({"session_id": "saved-42"}))
        c = boss_chat.BossChat(tmp_path, command=FAKE)
        try:
            assert c.start()["session_id"] == "saved-42"
        finally:
            c.stop()

    def test_unknown_saved_session_falls_back_to_new(self, state, tmp_path):
        (state / "boss").mkdir(parents=True)
        (state / "boss" / "ui-session.json").write_text(json.dumps({"session_id": "unknown-session"}))
        c = boss_chat.BossChat(tmp_path, command=FAKE)
        try:
            st = c.start()
            assert st["alive"] and st["session_id"] == "fake-session-0001"
            assert any("could not resume session unknown-session" in e["text"] for e in c.transcript if e["who"] == "system")
        finally:
            c.stop()

    def test_new_session_clears_transcript_and_session(self, chat, state):
        chat.start()
        chat.send("hello")
        assert _wait(lambda: not chat.busy)
        st = chat.new_session()
        assert st["alive"] and st["transcript"] == []
        assert not (state / "boss" / "ui-transcript.jsonl").exists()

    def test_bad_command_reports_error(self, state, tmp_path):
        c = boss_chat.BossChat(tmp_path, command=["/nonexistent/boss"])
        st = c.start()
        assert not st["alive"] and st["error"]


class TestTurns:
    def test_send_and_reply(self, chat):
        chat.start()
        st = chat.send("hello there")
        assert st["busy"]
        assert _wait(lambda: not chat.busy)
        assert _replies(chat) == ["echo: hello there"]
        assert [e["who"] for e in chat.transcript] == ["you", "boss"]
        assert chat.awaiting == "you"

    def test_multiline_reply(self, chat):
        chat.start()
        chat.send("multi")
        assert _wait(lambda: not chat.busy)
        assert _replies(chat) == ["first line\nsecond line"]

    def test_clarifying_question_then_answer(self, chat):
        chat.start()
        chat.send("ask me")
        assert _wait(lambda: not chat.busy)
        assert chat.awaiting == "ask"
        assert _replies(chat)[-1].startswith("Which one?\n1. alpha\n2. beta")
        chat.send("2")
        assert _wait(lambda: not chat.busy)
        assert _replies(chat)[-1] == "you picked 2"
        assert chat.awaiting == "you"

    def test_tool_activity_is_split_from_the_reply(self, chat):
        chat.start()
        chat.send("tools please")
        assert _wait(lambda: not chat.busy)
        entry = [e for e in chat.transcript if e["who"] == "boss"][-1]
        assert entry["text"] == "one task is running"
        assert entry["tools"] == ["team", "kanban_list"]

    def test_split_reply(self):
        text, tools = boss_chat.split_reply(["  ┊ ⚡ team   0.0s", "  ┊ 💬 note", "boss> hello", "more"])
        assert (text, tools) == ("hello\nmore", ["team"])
        assert boss_chat.split_reply(["Which one?", "1. a"]) == ("Which one?\n1. a", [])
        assert boss_chat.split_reply(["warning: x", "boss> hi"]) == ("warning: x\n\nhi", [])

    def test_send_while_busy_is_refused(self, chat):
        chat.start()
        chat.send("one")
        with pytest.raises(RuntimeError, match="still answering"):
            chat.send("two")
        assert _wait(lambda: not chat.busy)

    def test_send_when_stopped_is_refused(self, chat):
        with pytest.raises(RuntimeError, match="not running"):
            chat.send("hi")

    def test_empty_is_refused(self, chat):
        chat.start()
        with pytest.raises(ValueError):
            chat.send("   ")

    def test_newlines_are_flattened_to_one_line(self, chat):
        chat.start()
        chat.send("a\nb")
        assert _wait(lambda: not chat.busy)
        assert _replies(chat) == ["echo: a b"]

    def test_crash_mid_turn_is_reported(self, chat):
        chat.start()
        chat.send("crash")
        assert _wait(lambda: not chat.alive() and not chat.busy)
        texts = [e["text"] for e in chat.transcript]
        assert "going down" in texts
        assert any(t.startswith("boss exited") for t in texts)

    def test_transcript_persists_across_instances(self, chat, state, tmp_path):
        chat.start()
        chat.send("remember this")
        assert _wait(lambda: not chat.busy)
        again = boss_chat.BossChat(tmp_path, command=FAKE)
        assert [e["text"] for e in again.transcript] == ["remember this", "echo: remember this"]

    def test_new_goals_during_a_turn_become_system_lines(self, chat, state):
        conn = ledger.connect(ledger.db_path())
        ledger.init(conn)
        chat.start()
        chat.send("plan it")
        assert _wait(lambda: not chat.busy)
        # A goal created while the boss answers (as planning_dispatch does).
        ledger.create_goal(conn, "team", "Add the thing\n\nDetails.", "implement", "team/x", "/tmp/x", "uv run pytest -q")
        conn.close()
        chat.send("and dispatch")
        assert _wait(lambda: not chat.busy)
        system = [e["text"] for e in chat.transcript if e["who"] == "system"]
        assert system == ["sent goal 1 to implement on team: Add the thing"]

    def test_fingerprint_tracks_turns(self, chat):
        a = chat.fingerprint()
        chat.start()
        b = chat.fingerprint()
        assert a != b
        chat.send("x")
        assert _wait(lambda: not chat.busy)
        assert chat.fingerprint() != b
