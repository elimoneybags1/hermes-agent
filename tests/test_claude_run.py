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


def test_build_cmd_with_tools():
    """Test _build_cmd includes --allowed-tools."""
    cmd = claude_run._build_cmd("brief", max_turns=5, tools=["Read", "Write", "Bash"])
    assert "--allowed-tools" in cmd
    idx = cmd.index("--allowed-tools")
    assert cmd[idx + 1] == "Read,Write,Bash"


def test_build_cmd_with_model():
    """Test _build_cmd includes --model."""
    cmd = claude_run._build_cmd("brief", model="claude-3-opus")
    assert "--model" in cmd
    idx = cmd.index("--model")
    assert cmd[idx + 1] == "claude-3-opus"


def test_build_cmd_with_json_schema():
    """Test _build_cmd includes --schema with temp file."""
    schema = {"type": "object", "properties": {"result": {"type": "string"}}}
    cmd = claude_run._build_cmd("brief", json_schema=schema)
    assert "--schema" in cmd
    idx = cmd.index("--schema")
    schema_path = cmd[idx + 1]
    assert schema_path.endswith(".json")
    # Verify the file contains the schema
    with open(schema_path) as f:
        loaded = json.load(f)
    assert loaded == schema


def test_build_cmd_with_allowed_files():
    """Test _build_cmd includes --allowed-files."""
    cmd = claude_run._build_cmd("brief", allowed_files=["src/**", "tests/**"])
    assert "--allowed-files" in cmd
    idx = cmd.index("--allowed-files")
    assert cmd[idx + 1] == "src/**:tests/**"


def test_build_cmd_with_check():
    """Test _build_cmd includes --check."""
    cmd = claude_run._build_cmd("brief", check="pytest")
    assert "--check" in cmd
    idx = cmd.index("--check")
    assert cmd[idx + 1] == "pytest"


def test_build_cmd_with_decision_file():
    """Test _build_cmd includes --decision-file."""
    cmd = claude_run._build_cmd("brief", decision_file="/tmp/decision.json")
    assert "--decision-file" in cmd
    idx = cmd.index("--decision-file")
    assert cmd[idx + 1] == "/tmp/decision.json"


def test_run_passes_tools():
    """Test run() passes tools to _build_cmd."""
    fake_runner = MagicMock(return_value=FakeCompletedProcess(0, '{"is_error": false}'))
    claude_run.run(Path("/tmp/wt"), "brief", tools=["Read", "Write"], runner=fake_runner)
    args, _ = fake_runner.call_args
    assert "--allowed-tools" in args[0]


def test_run_passes_model():
    """Test run() passes model to _build_cmd."""
    fake_runner = MagicMock(return_value=FakeCompletedProcess(0, '{"is_error": false}'))
    claude_run.run(Path("/tmp/wt"), "brief", model="test-model", runner=fake_runner)
    args, _ = fake_runner.call_args
    assert "--model" in args[0]
    idx = args[0].index("--model")
    assert args[0][idx + 1] == "test-model"


def test_run_passes_allowed_files():
    """Test run() passes allowed_files to _build_cmd."""
    fake_runner = MagicMock(return_value=FakeCompletedProcess(0, '{"is_error": false}'))
    claude_run.run(Path("/tmp/wt"), "brief", allowed_files=["src/**"], runner=fake_runner)
    args, _ = fake_runner.call_args
    assert "--allowed-files" in args[0]


def test_run_passes_check():
    """Test run() passes check to _build_cmd."""
    fake_runner = MagicMock(return_value=FakeCompletedProcess(0, '{"is_error": false}'))
    claude_run.run(Path("/tmp/wt"), "brief", check="make test", runner=fake_runner)
    args, _ = fake_runner.call_args
    assert "--check" in args[0]


def test_run_passes_decision_file():
    """Test run() passes decision_file to _build_cmd."""
    fake_runner = MagicMock(return_value=FakeCompletedProcess(0, '{"is_error": false}'))
    claude_run.run(Path("/tmp/wt"), "brief", decision_file="/tmp/decision.json", runner=fake_runner)
    args, _ = fake_runner.call_args
    assert "--decision-file" in args[0]