"""Tests for team/claude_run.py."""

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from team import claude_run


class FakeCompletedProcess:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_clean_env_scrubs_all_keys():
    env = {
        "ANTHROPIC_API_KEY": "key1",
        "ANTHROPIC_AUTH_TOKEN": "token1",
        "ANTHROPIC_BASE_URL": "url1",
        "CLAUDE_CODE_USE_BEDROCK": "true",
        "CLAUDE_CODE_USE_VERTEX": "true",
        "PATH": "/usr/bin",
        "HOME": "/home/user",
    }
    clean = claude_run.clean_env(env)
    for k in claude_run.SCRUB:
        assert k not in clean
    assert clean["PATH"] == "/usr/bin"
    assert clean["HOME"] == "/home/user"


def test_clean_env_defaults_to_os_environ(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "should_be_removed")
    monkeypatch.setenv("KEEP_ME", "value")
    clean = claude_run.clean_env()
    assert "ANTHROPIC_API_KEY" not in clean
    assert clean["KEEP_ME"] == "value"


def test_run_success_parses_json_output():
    payload = {
        "is_error": False,
        "result": "done",
        "session_id": "sess-123",
        "num_turns": 5,
        "usage": {"input_tokens": 100, "output_tokens": 50},
    }
    fake_runner = MagicMock(return_value=FakeCompletedProcess(0, json.dumps(payload)))

    result = claude_run.run(Path("/tmp/wt"), "brief", runner=fake_runner)

    assert result["ok"] is True
    assert result["result"] == "done"
    assert result["session_id"] == "sess-123"
    assert result["num_turns"] == 5
    assert result["usage"] == {"input_tokens": 100, "output_tokens": 50}
    assert result["duration_ms"] >= 0

    # Check argv passed to runner
    args, kwargs = fake_runner.call_args
    assert args[0] == [
        "claude",
        "-p",
        "brief",
        "--output-format",
        "json",
        "--max-turns",
        "60",
        "--dangerously-skip-permissions",
    ]
    assert kwargs["cwd"] == Path("/tmp/wt")
    for k in claude_run.SCRUB:
        assert k not in kwargs["env"]


def test_run_error_payload_is_error_true():
    payload = {
        "is_error": True,
        "result": "something failed",
        "session_id": "sess-123",
        "num_turns": 3,
        "usage": None,
    }
    fake_runner = MagicMock(return_value=FakeCompletedProcess(0, json.dumps(payload)))

    result = claude_run.run(Path("/tmp/wt"), "brief", runner=fake_runner)

    assert result["ok"] is False
    assert result["result"] == "something failed"


def test_run_nonzero_exit_returns_ok_false():
    fake_runner = MagicMock(
        return_value=FakeCompletedProcess(1, "", "claude: command not found")
    )

    result = claude_run.run(Path("/tmp/wt"), "brief", runner=fake_runner)

    assert result["ok"] is False
    assert "command not found" in result["result"]


def test_run_invalid_json_returns_ok_false():
    fake_runner = MagicMock(return_value=FakeCompletedProcess(0, "not json"))

    result = claude_run.run(Path("/tmp/wt"), "brief", runner=fake_runner)

    assert result["ok"] is False
    assert result["result"] == "output is not valid JSON"


def test_run_timeout_returns_ok_false():
    import subprocess

    def raiser(*a, **kw):
        raise subprocess.TimeoutExpired("claude", 1800)

    fake_runner = MagicMock(side_effect=raiser)

    result = claude_run.run(Path("/tmp/wt"), "brief", timeout_s=1800, runner=fake_runner)

    assert result["ok"] is False
    assert result["result"] == "timeout after 1800s"


def test_run_custom_max_turns():
    fake_runner = MagicMock(return_value=FakeCompletedProcess(0, "{}"))

    claude_run.run(Path("/tmp/wt"), "brief", max_turns=10, runner=fake_runner)

    args, _ = fake_runner.call_args
    assert "--max-turns" in args[0]
    idx = args[0].index("--max-turns")
    assert args[0][idx + 1] == "10"


def test_run_passes_custom_timeout():
    import subprocess

    def raiser(*a, **kw):
        raise subprocess.TimeoutExpired("claude", 900)

    fake_runner = MagicMock(side_effect=raiser)

    result = claude_run.run(Path("/tmp/wt"), "brief", timeout_s=900, runner=fake_runner)

    assert result["result"] == "timeout after 900s"


def test_build_cmd_options():
    schema = {"type": "object", "properties": {"x": {"type": "string"}}}
    cmd = claude_run._build_cmd("brief", tools=["Read", "Bash"], model="opus", json_schema=schema)
    assert cmd[cmd.index("--tools") + 1] == "Read,Bash"
    assert cmd[cmd.index("--model") + 1] == "opus"
    assert json.loads(cmd[cmd.index("--json-schema") + 1]) == schema


def test_build_cmd_empty_tools_means_no_tools():
    cmd = claude_run._build_cmd("brief", tools=[])
    assert cmd[cmd.index("--tools") + 1] == ""


def _role_root(tmp: Path) -> Path:
    role = tmp / "repo" / "roles" / "implement"
    role.mkdir(parents=True)
    (role / "role.toml").write_text('name = "implement"\ndoes = "Builds code."\ntools = ["terminal", "file"]\n')
    (role / "SOUL.md").write_text("You implement.")
    return tmp / "repo"


def test_run_with_role_is_build_mode(tmp_path, monkeypatch):
    monkeypatch.setenv("TEAM_STATE", str(tmp_path / "state"))
    root = _role_root(tmp_path)
    fake_runner = MagicMock(return_value=FakeCompletedProcess(0, json.dumps(
        {"is_error": False, "result": "{}", "structured_output": {"a": 1}})))

    result = claude_run.run(tmp_path, "brief", runner=fake_runner, role="implement", root=root,
                            check="pytest -q", allowed_files=["a.py", "tests/*"],
                            decision_file=tmp_path / "d.json")

    assert result["structured"] == {"a": 1}
    cmd = fake_runner.call_args.args[0]
    env = fake_runner.call_args.kwargs["env"]
    state = tmp_path / "state" / "claude"
    assert cmd[cmd.index("--plugin-dir") + 1] == str(state / "implement")
    assert cmd[cmd.index("--agent") + 1] == "team-implement:implement"
    assert cmd[cmd.index("--setting-sources") + 1] == "project,local"
    assert cmd[cmd.index("--settings") + 1] == str(state / "settings.json")
    assert "--strict-mcp-config" in cmd
    assert cmd[cmd.index("--tools") + 1] == "Bash,Read,Edit,Write,Glob,Grep"
    assert env["TEAM_CHECK"] == "pytest -q"
    assert env["TEAM_ALLOWED_FILES"] == "a.py:tests/*"
    assert env["TEAM_DECISION_FILE"] == str(tmp_path / "d.json")
    for k in claude_run.SCRUB:
        assert k not in env


def test_run_without_role_loads_no_team_setup():
    fake_runner = MagicMock(return_value=FakeCompletedProcess(0, json.dumps({"is_error": False})))
    claude_run.run(Path("/tmp/wt"), "brief", runner=fake_runner)
    cmd = fake_runner.call_args.args[0]
    for flag in ("--plugin-dir", "--agent", "--settings", "--tools"):
        assert flag not in cmd
