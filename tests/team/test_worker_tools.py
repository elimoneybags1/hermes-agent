"""The Team plugin's worker hook: no card re-reads, no file re-reads."""

import importlib.util
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


def load():
    spec = importlib.util.spec_from_file_location(
        "team_worker_tools", REPO / "plugins" / "team" / "worker_tools.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def wt():
    module = load()
    yield module
    module.forget()


def test_only_inside_a_dispatched_worker(wt, monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    assert wt.pre_tool_call(tool_name="kanban_show", args={}, session_id="s") is None
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_1")
    verdict = wt.pre_tool_call(tool_name="kanban_show", args={}, session_id="s")
    assert verdict["action"] == "block" and "task card" in verdict["message"]


def test_kanban_hand_off_tools_pass_and_the_rest_are_refused(wt):
    for name in ("kanban_request_review", "kanban_block"):
        assert wt.decide(name, {"summary": "done"}, "s") is None
    for name in ("kanban_show", "kanban_list", "kanban_create", "kanban_attachments"):
        assert wt.decide(name, {}, "s")["action"] == "block", name
    assert "kanban_request_review" in wt.decide("kanban_complete", {}, "s")["message"]


def test_second_read_of_an_unchanged_file_is_refused(wt, tmp_path):
    f = tmp_path / "a.py"
    f.write_text("x = 1\n")
    args = {"path": str(f)}
    assert wt.decide("read_file", args, "s") is None
    verdict = wt.decide("read_file", dict(args), "s")
    assert verdict["action"] == "block" and str(f) in verdict["message"]
    # a different range is a different read
    assert wt.decide("read_file", {"path": str(f), "offset": 10, "limit": 5}, "s") is None
    # another session has not read it
    assert wt.decide("read_file", dict(args), "other") is None


def test_a_changed_file_may_be_read_again(wt, tmp_path):
    f = tmp_path / "a.py"
    f.write_text("x = 1\n")
    args = {"path": str(f)}
    assert wt.decide("read_file", args, "s") is None
    assert wt.decide("patch", {"path": str(f), "old": "1", "new": "2"}, "s") is None
    assert wt.decide("read_file", dict(args), "s") is None, "a patch clears the read"
    assert wt.decide("read_file", dict(args), "s")["action"] == "block"
    f.write_text("x = 3\n" * 10)  # changed by a shell command: size differs
    assert wt.decide("read_file", dict(args), "s") is None


def test_missing_file_and_other_tools_pass(wt, tmp_path):
    missing = {"path": str(tmp_path / "nope.py")}
    assert wt.decide("read_file", missing, "s") is None
    assert wt.decide("read_file", dict(missing), "s") is None
    assert wt.decide("terminal", {"command": "ls"}, "s") is None
    assert wt.decide("read_file", {}, "s") is None
