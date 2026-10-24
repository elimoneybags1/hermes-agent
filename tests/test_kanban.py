"""Tests for team.kanban."""

import json
import os
import subprocess
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from team import kanban


class FakeCompletedProcess:
    """A fake CompletedProcess for testing."""

    def __init__(self, returncode: int, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def fake_runner_factory(canned_results: dict[tuple, FakeCompletedProcess]):
    """Create a runner that returns canned results based on argv."""

    def runner(cmd, capture_output=True, text=True, env=None, **kwargs):
        key = tuple(cmd)
        if key in canned_results:
            return canned_results[key]
        # Default: return a failure for unexpected commands
        return FakeCompletedProcess(1, "", f"Unexpected command: {' '.join(cmd)}")

    return runner


def test_kanban_home():
    """kanban_home returns state_dir/kanban."""
    with patch("team.kanban.state_dir") as mock_state_dir:
        mock_state_dir.return_value = Path("/home/user/state/team")
        assert kanban.kanban_home() == Path("/home/user/state/team/kanban")


def test_create_task_success(monkeypatch, tmp_path):
    """create_task calls hermes with correct args and parses JSON output."""
    state_dir = tmp_path / "state" / "team"
    kanban_home = state_dir / "kanban"
    kanban_home.mkdir(parents=True)

    task_json = json.dumps({
        "id": "t_abc123",
        "title": "Test Task",
        "status": "todo",
        "result": None,
    })

    def runner(cmd, **kwargs):
        assert cmd[:4] == ["hermes", "-p", "team-worker", "kanban"]
        assert "--board" in cmd
        assert "create" in cmd
        assert "--json" in cmd
        assert "--assignee" in cmd
        assert "team-worker" in cmd
        assert kwargs.get("env", {}).get("HERMES_KANBAN_HOME") == str(kanban_home)
        return FakeCompletedProcess(0, task_json, "")

    with patch("team.kanban.state_dir") as mock_state_dir:
        mock_state_dir.return_value = state_dir

        result = kanban.create_task(
            board="test",
            title="Test Task",
            body="Test body",
            runner=runner,
        )

    assert result["id"] == "t_abc123"
    assert result["title"] == "Test Task"


def test_create_task_with_optional_args(monkeypatch, tmp_path):
    """create_task passes optional args correctly."""
    state_dir = tmp_path / "state" / "team"
    kanban_home = state_dir / "kanban"
    kanban_home.mkdir(parents=True)

    task_json = json.dumps({"id": "t_abc123", "title": "Test", "status": "todo"})

    captured_cmd = {}

    def runner(cmd, **kwargs):
        captured_cmd["argv"] = cmd
        captured_cmd["env"] = kwargs.get("env", {})
        return FakeCompletedProcess(0, task_json, "")

    with patch("team.kanban.state_dir") as mock_state_dir:
        mock_state_dir.return_value = state_dir

        kanban.create_task(
            board="myboard",
            title="Test",
            body="Body",
            assignee="custom-worker",
            workspace="worktree:/path",
            branch="team/test-branch",
            max_runtime=3600,
            skills=("skill1", "skill2"),
            runner=runner,
        )

    argv = captured_cmd["argv"]
    assert "--workspace" in argv
    assert "worktree:/path" in argv
    assert "--branch" in argv
    assert "team/test-branch" in argv
    assert "--max-runtime" in argv
    assert "3600s" in argv
    # skills are now repeated --skill flags
    assert argv.count("--skill") == 2
    assert "skill1" in argv
    assert "skill2" in argv
    assert captured_cmd["env"]["HERMES_KANBAN_HOME"] == str(kanban_home)


def test_create_task_failure_raises_kanban_error(tmp_path):
    """create_task raises KanbanError with stderr on non-zero exit."""
    kanban_home = tmp_path / "kanban"
    kanban_home.mkdir(parents=True)

    def runner(cmd, **kwargs):
        return FakeCompletedProcess(1, "", "Error: board not found")

    with patch("team.kanban.state_dir") as mock_state_dir:
        mock_state_dir.return_value = tmp_path / "state" / "team"

        with pytest.raises(kanban.KanbanError) as exc_info:
            kanban.create_task(board="test", title="Test", body="Body", runner=runner)

    assert "create failed" in str(exc_info.value)
    assert exc_info.value.stderr == "Error: board not found"


def test_show_returns_task_directly(tmp_path):
    """show returns the task when hermes returns the task directly."""
    kanban_home = tmp_path / "kanban"
    kanban_home.mkdir(parents=True)

    task_json = json.dumps({
        "id": "t_abc123",
        "title": "Test Task",
        "status": "running",
    })

    def runner(cmd, **kwargs):
        assert "show" in cmd
        assert "t_abc123" in cmd
        assert "--json" in cmd
        return FakeCompletedProcess(0, task_json, "")

    with patch("team.kanban.state_dir") as mock_state_dir:
        mock_state_dir.return_value = tmp_path / "state" / "team"

        result = kanban.show(board="test", task_id="t_abc123", runner=runner)

    assert result["id"] == "t_abc123"
    assert result["status"] == "running"


def test_show_returns_task_from_wrapped_object(tmp_path):
    """show returns task from wrapped object with 'task' key."""
    kanban_home = tmp_path / "kanban"
    kanban_home.mkdir(parents=True)

    wrapped_json = json.dumps({
        "task": {"id": "t_abc123", "title": "Test Task", "status": "done"},
        "runs": [{"id": 1, "profile": "team-worker"}],
    })

    def runner(cmd, **kwargs):
        return FakeCompletedProcess(0, wrapped_json, "")

    with patch("team.kanban.state_dir") as mock_state_dir:
        mock_state_dir.return_value = tmp_path / "state" / "team"

        result = kanban.show(board="test", task_id="t_abc123", runner=runner)

    assert result["id"] == "t_abc123"
    assert result["status"] == "done"


def test_list_tasks_success(tmp_path):
    """list_tasks returns list of tasks."""
    kanban_home = tmp_path / "kanban"
    kanban_home.mkdir(parents=True)

    tasks_json = json.dumps([
        {"id": "t_1", "title": "Task 1", "status": "todo"},
        {"id": "t_2", "title": "Task 2", "status": "done"},
    ])

    def runner(cmd, **kwargs):
        assert "list" in cmd
        assert "--json" in cmd
        return FakeCompletedProcess(0, tasks_json, "")

    with patch("team.kanban.state_dir") as mock_state_dir:
        mock_state_dir.return_value = tmp_path / "state" / "team"

        result = kanban.list_tasks(board="test", runner=runner)

    assert len(result) == 2
    assert result[0]["id"] == "t_1"
    assert result[1]["id"] == "t_2"


def test_list_tasks_with_status_filter(tmp_path):
    """list_tasks passes --status filter."""
    kanban_home = tmp_path / "kanban"
    kanban_home.mkdir(parents=True)

    captured_cmd = {}

    def runner(cmd, **kwargs):
        captured_cmd["argv"] = cmd
        return FakeCompletedProcess(0, "[]", "")

    with patch("team.kanban.state_dir") as mock_state_dir:
        mock_state_dir.return_value = tmp_path / "state" / "team"

        kanban.list_tasks(board="test", status="running", runner=runner)

    assert "--status" in captured_cmd["argv"]
    assert "running" in captured_cmd["argv"]


def test_dispatch_success(tmp_path):
    """dispatch returns parsed result."""
    kanban_home = tmp_path / "kanban"
    kanban_home.mkdir(parents=True)

    dispatch_json = json.dumps({"dispatched": 2, "skipped": 1})

    def runner(cmd, **kwargs):
        assert "dispatch" in cmd
        assert "--json" in cmd
        return FakeCompletedProcess(0, dispatch_json, "")

    with patch("team.kanban.state_dir") as mock_state_dir:
        mock_state_dir.return_value = tmp_path / "state" / "team"

        result = kanban.dispatch(board="test", runner=runner)

    assert result["dispatched"] == 2
    assert result["skipped"] == 1


def test_comment_success(tmp_path):
    """comment calls hermes with correct args."""
    kanban_home = tmp_path / "kanban"
    kanban_home.mkdir(parents=True)

    captured_cmd = {}

    def runner(cmd, **kwargs):
        captured_cmd["argv"] = cmd
        return FakeCompletedProcess(0, "", "")

    with patch("team.kanban.state_dir") as mock_state_dir:
        mock_state_dir.return_value = tmp_path / "state" / "team"

        kanban.comment(board="test", task_id="t_abc123", text="Hello", runner=runner)

    argv = captured_cmd["argv"]
    assert "comment" in argv
    assert "t_abc123" in argv
    assert "Hello" in argv
    assert "--author" in argv
    assert "team" in argv


def test_block_success(tmp_path):
    """block calls hermes with correct args."""
    kanban_home = tmp_path / "kanban"
    kanban_home.mkdir(parents=True)

    captured_cmd = {}

    def runner(cmd, **kwargs):
        captured_cmd["argv"] = cmd
        return FakeCompletedProcess(0, "", "")

    with patch("team.kanban.state_dir") as mock_state_dir:
        mock_state_dir.return_value = tmp_path / "state" / "team"

        kanban.block(board="test", task_id="t_abc123", reason="Need input", kind="needs_input", runner=runner)

    argv = captured_cmd["argv"]
    assert "block" in argv
    assert "t_abc123" in argv
    assert "Need input" in argv
    assert "--kind" in argv
    assert "needs_input" in argv


def test_block_default_kind(tmp_path):
    """block uses default kind='needs_input'."""
    kanban_home = tmp_path / "kanban"
    kanban_home.mkdir(parents=True)

    captured_cmd = {}

    def runner(cmd, **kwargs):
        captured_cmd["argv"] = cmd
        return FakeCompletedProcess(0, "", "")

    with patch("team.kanban.state_dir") as mock_state_dir:
        mock_state_dir.return_value = tmp_path / "state" / "team"

        kanban.block(board="test", task_id="t_abc123", reason="Need input", runner=runner)

    argv = captured_cmd["argv"]
    assert "--kind" in argv
    kind_idx = argv.index("--kind")
    assert argv[kind_idx + 1] == "needs_input"


def test_session_for_task_found(tmp_path):
    """session_for_task finds matching session and exports it."""
    state_dir = tmp_path / "state" / "team"
    kanban_home = state_dir / "kanban"
    kanban_home.mkdir(parents=True)

    # Real text table output from `hermes sessions list`
    sessions_list = """Title                        Workspace          Last Active   ID
──────────────────────────────────────────────────────────────────────────────────────────────────────────────
work kanban task t_abc1234 some task   —                  just now      20261024_150051_7ce33b
other session                                    —                  5m ago        20261024_144848_173531"""

    export_jsonl = json.dumps({
        "model": "nvidia/nemotron-3-ultra",
        "billing_provider": "openrouter",
        "input_tokens": 100,
        "output_tokens": 50,
        "estimated_cost_usd": 0.0,
        "api_call_count": 3,
    })

    call_count = {"list": 0, "export": 0}

    def runner(cmd, **kwargs):
        if "sessions" in cmd and "list" in cmd:
            call_count["list"] += 1
            return FakeCompletedProcess(0, sessions_list, "")
        if "sessions" in cmd and "export" in cmd:
            call_count["export"] += 1
            assert "--session-id" in cmd
            assert "20261024_150051_7ce33b" in cmd
            assert "--format" in cmd
            assert "jsonl" in cmd
            return FakeCompletedProcess(0, export_jsonl, "")
        return FakeCompletedProcess(1, "", "unexpected")

    with patch("team.kanban.state_dir") as mock_state_dir:
        mock_state_dir.return_value = state_dir

        result = kanban.session_for_task(task_id="t_abc123456789", runner=runner)

    assert result is not None
    assert result["session_id"] == "20261024_150051_7ce33b"
    assert result["model"] == "nvidia/nemotron-3-ultra"
    assert result["billing_provider"] == "openrouter"
    assert result["input_tokens"] == 100
    assert result["output_tokens"] == 50
    assert result["estimated_cost_usd"] == 0.0
    assert result["api_call_count"] == 3
    assert call_count["list"] == 1
    assert call_count["export"] == 1


def test_session_for_task_not_found(tmp_path):
    """session_for_task returns None when no matching session."""
    state_dir = tmp_path / "state" / "team"
    kanban_home = state_dir / "kanban"
    kanban_home.mkdir(parents=True)

    sessions_list = """Title                        Workspace          Last Active   ID
──────────────────────────────────────────────────────────────────────────────────────────────────────────────
work kanban task t_other123 some task   —                  just now      20261024_150051_7ce33b"""

    def runner(cmd, **kwargs):
        if "sessions" in cmd and "list" in cmd:
            return FakeCompletedProcess(0, sessions_list, "")
        return FakeCompletedProcess(1, "", "unexpected")

    with patch("team.kanban.state_dir") as mock_state_dir:
        mock_state_dir.return_value = state_dir

        result = kanban.session_for_task(task_id="t_abc123456789", runner=runner)

    assert result is None


def test_session_for_task_list_failure(tmp_path):
    """session_for_task returns None when list command fails."""
    state_dir = tmp_path / "state" / "team"
    kanban_home = state_dir / "kanban"
    kanban_home.mkdir(parents=True)

    def runner(cmd, **kwargs):
        return FakeCompletedProcess(1, "", "command failed")

    with patch("team.kanban.state_dir") as mock_state_dir:
        mock_state_dir.return_value = state_dir

        result = kanban.session_for_task(task_id="t_abc123456789", runner=runner)

    assert result is None


def test_session_for_task_export_failure(tmp_path):
    """session_for_task returns None when export command fails."""
    state_dir = tmp_path / "state" / "team"
    kanban_home = state_dir / "kanban"
    kanban_home.mkdir(parents=True)

    sessions_list = """Title                        Workspace          Last Active   ID
──────────────────────────────────────────────────────────────────────────────────────────────────────────────
work kanban task t_abc123 some task   —                  just now      20261024_150051_7ce33b"""

    def runner(cmd, **kwargs):
        if "sessions" in cmd and "list" in cmd:
            return FakeCompletedProcess(0, sessions_list, "")
        if "sessions" in cmd and "export" in cmd:
            return FakeCompletedProcess(1, "", "export failed")
        return FakeCompletedProcess(1, "", "unexpected")

    with patch("team.kanban.state_dir") as mock_state_dir:
        mock_state_dir.return_value = state_dir

        result = kanban.session_for_task(task_id="t_abc123456789", runner=runner)

    assert result is None


def test_session_for_task_invalid_json(tmp_path):
    """session_for_task returns None when export returns invalid JSON."""
    state_dir = tmp_path / "state" / "team"
    kanban_home = state_dir / "kanban"
    kanban_home.mkdir(parents=True)

    sessions_list = """Title                        Workspace          Last Active   ID
──────────────────────────────────────────────────────────────────────────────────────────────────────────────
work kanban task t_abc123 some task   —                  just now      20261024_150051_7ce33b"""

    def runner(cmd, **kwargs):
        if "sessions" in cmd and "list" in cmd:
            return FakeCompletedProcess(0, sessions_list, "")
        if "sessions" in cmd and "export" in cmd:
            return FakeCompletedProcess(0, "not json", "")
        return FakeCompletedProcess(1, "", "unexpected")

    with patch("team.kanban.state_dir") as mock_state_dir:
        mock_state_dir.return_value = state_dir

        result = kanban.session_for_task(task_id="t_abc123456789", runner=runner)

    assert result is None


def test_session_for_task_uses_first_nine_chars(tmp_path):
    """session_for_task matches on first 9 chars of task id."""
    state_dir = tmp_path / "state" / "team"
    kanban_home = state_dir / "kanban"
    kanban_home.mkdir(parents=True)

    sessions_list = """Title                        Workspace          Last Active   ID
──────────────────────────────────────────────────────────────────────────────────────────────────────────────
work kanban task t_abc1234 some task   —                  just now      20261024_150051_7ce33b"""

    export_jsonl = json.dumps({
        "model": "test-model",
        "billing_provider": "test-provider",
        "input_tokens": 10,
        "output_tokens": 5,
        "estimated_cost_usd": 0.0,
        "api_call_count": 1,
    })

    def runner(cmd, **kwargs):
        if "sessions" in cmd and "list" in cmd:
            return FakeCompletedProcess(0, sessions_list, "")
        if "sessions" in cmd and "export" in cmd:
            return FakeCompletedProcess(0, export_jsonl, "")
        return FakeCompletedProcess(1, "", "unexpected")

    with patch("team.kanban.state_dir") as mock_state_dir:
        mock_state_dir.return_value = state_dir

        # Task ID longer than 9 chars, should match on first 9
        result = kanban.session_for_task(task_id="t_abc12345extra", runner=runner)

    assert result is not None
    assert result["session_id"] == "20261024_150051_7ce33b"


def test_env_inheritance(tmp_path):
    """Environment is inherited and HERMES_KANBAN_HOME is set."""
    state_dir = tmp_path / "state" / "team"
    kanban_home = state_dir / "kanban"
    kanban_home.mkdir(parents=True)

    captured_env = {}

    def runner(cmd, **kwargs):
        captured_env["env"] = kwargs.get("env", {})
        return FakeCompletedProcess(0, "[]", "")

    with patch("team.kanban.state_dir") as mock_state_dir:
        mock_state_dir.return_value = state_dir
        # Set a custom env var to verify inheritance
        os.environ["CUSTOM_VAR"] = "test_value"

        kanban.list_tasks(board="test", runner=runner)

    env = captured_env["env"]
    assert env["HERMES_KANBAN_HOME"] == str(kanban_home)
    assert env["CUSTOM_VAR"] == "test_value"

def _recorder(stdout=""):
    calls = []

    def runner(cmd, **kwargs):
        calls.append((cmd, kwargs.get("env", {})))
        return FakeCompletedProcess(0, stdout, "")

    return runner, calls


def test_dispatch_max_spawns():
    runner, calls = _recorder("{}")
    kanban.dispatch("demo", max_spawns=2, runner=runner)
    assert calls[0][0] == ["hermes", "-p", "team-worker", "kanban", "--board", "demo",
                           "dispatch", "--json", "--max", "2"]


def test_show_full_keeps_runs_and_accepts_bare_task():
    payload = {"task": {"id": "t_1", "status": "running"},
               "runs": [{"id": 1, "worker_pid": 42, "started_at": 1792867727}]}
    runner, _ = _recorder(json.dumps(payload))
    full = kanban.show_full("demo", "t_1", runner=runner)
    assert full["runs"][-1]["worker_pid"] == 42
    assert kanban.show("demo", "t_1", runner=runner)["status"] == "running"

    runner, _ = _recorder(json.dumps({"id": "t_1", "status": "done"}))
    assert kanban.show_full("demo", "t_1", runner=runner) == {
        "task": {"id": "t_1", "status": "done"}, "runs": []}


def test_ensure_board_creates_only_when_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("TEAM_STATE", str(tmp_path))
    boards = json.dumps([{"slug": "default"}, {"slug": "team"}])
    runner, calls = _recorder(boards)
    assert kanban.ensure_board("team", runner=runner) is False
    assert kanban.ensure_board("demo", runner=runner) is True
    argvs = [c[0] for c in calls]
    assert argvs == [
        ["hermes", "-p", "team-worker", "kanban", "boards", "list", "--json"],
        ["hermes", "-p", "team-worker", "kanban", "boards", "list", "--json"],
        ["hermes", "-p", "team-worker", "kanban", "boards", "create", "demo"],
    ]
    assert calls[-1][1]["HERMES_KANBAN_HOME"] == str(tmp_path / "kanban")
