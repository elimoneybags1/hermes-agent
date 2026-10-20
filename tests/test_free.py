"""Tests for the free model guard."""

import json
import subprocess
import sys
from team.free import check_command, is_free_model


class TestIsFreeModel:
    def test_free_suffix(self):
        assert is_free_model("some-model:free") is True
        assert is_free_model("provider/model:free") is True
    
    def test_free_exact(self):
        assert is_free_model("gemini-2.5-flash") is True
        assert is_free_model("openai/gpt-oss-120b") is True
    
    def test_paid_model(self):
        assert is_free_model("gpt-4") is False
        assert is_free_model("claude-3-opus") is False
        assert is_free_model("anthropic/claude-3") is False


class TestCheckCommand:
    def test_allowed_free_chat(self):
        cmd = 'hermes chat -m "model:free"'
        assert check_command(cmd) is None
    
    def test_allowed_free_chat_long_form(self):
        cmd = 'hermes chat --model="model:free"'
        assert check_command(cmd) is None
    
    def test_allowed_free_exact_model(self):
        cmd = 'hermes chat -m gemini-2.5-flash'
        assert check_command(cmd) is None
    
    def test_missing_model_refused(self):
        cmd = 'hermes chat'
        reason = check_command(cmd)
        assert reason is not None
        assert "requires explicit -m/--model" in reason
    
    def test_paid_model_refused(self):
        cmd = 'hermes chat -m gpt-4'
        reason = check_command(cmd)
        assert reason is not None
        assert "not free" in reason
    
    def test_paid_provider_refused(self):
        cmd = 'hermes chat -m "model:free" --provider openai'
        reason = check_command(cmd)
        assert reason is not None
        assert "provider" in reason.lower()
        assert "not free" in reason
    
    def test_hermes_after_and_refused(self):
        cmd = 'echo hello && hermes chat -q "test"'
        reason = check_command(cmd)
        assert reason is not None
        assert "requires explicit -m/--model" in reason
    
    def test_non_hermes_command_allowed(self):
        cmd = 'echo hello world'
        assert check_command(cmd) is None
    
    def test_hermes_sessions_list_allowed(self):
        cmd = 'hermes sessions list'
        assert check_command(cmd) is None
    
    def test_hermes_fallback_list_allowed(self):
        cmd = 'hermes fallback list'
        assert check_command(cmd) is None
    
    def test_unbalanced_quotes_refused(self):
        cmd = "hermes chat -m 'unclosed"
        reason = check_command(cmd)
        assert reason is not None
        assert "unbalanced quotes" in reason
    
    def test_implicit_chat_with_query_flag(self):
        cmd = 'hermes -q "test" -m "model:free"'
        assert check_command(cmd) is None
    
    def test_implicit_chat_without_model_refused(self):
        cmd = 'hermes -q "test"'
        reason = check_command(cmd)
        assert reason is not None
        assert "requires explicit -m/--model" in reason
    
    def test_model_equals_form(self):
        cmd = 'hermes chat --model=gemini-2.5-flash'
        assert check_command(cmd) is None
    
    def test_provider_equals_form(self):
        cmd = 'hermes chat -m "model:free" --provider=openai'
        reason = check_command(cmd)
        assert reason is not None
        assert "provider" in reason.lower()


class TestCLI:
    def _run_free_check(self, command: str) -> tuple[int, str, str]:
        payload = {"tool_name": "Bash", "tool_input": {"command": command}}
        result = subprocess.run(
            [sys.executable, "-m", "team", "free-check"],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
        )
        return result.returncode, result.stdout, result.stderr
    
    def test_allowed_free_chat_cli(self):
        code, _, _ = self._run_free_check('hermes chat -m "model:free"')
        assert code == 0
    
    def test_missing_model_refused_cli(self):
        code, _, stderr = self._run_free_check('hermes chat')
        assert code == 2
        assert "requires explicit -m/--model" in stderr
    
    def test_paid_model_refused_cli(self):
        code, _, stderr = self._run_free_check('hermes chat -m gpt-4')
        assert code == 2
        assert "not free" in stderr
    
    def test_paid_provider_refused_cli(self):
        code, _, stderr = self._run_free_check('hermes chat -m "model:free" --provider openai')
        assert code == 2
        assert "provider" in stderr.lower()
    
    def test_non_hermes_allowed_cli(self):
        code, _, _ = self._run_free_check('echo hello')
        assert code == 0
    
    def test_hermes_sessions_allowed_cli(self):
        code, _, _ = self._run_free_check('hermes sessions list')
        assert code == 0
    
    def test_malformed_json_exits_zero(self):
        result = subprocess.run(
            [sys.executable, "-m", "team", "free-check"],
            input="not json",
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
    
    def test_missing_fields_exits_zero(self):
        payload = {"tool_name": "Bash"}
        result = subprocess.run(
            [sys.executable, "-m", "team", "free-check"],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
    
    def test_non_bash_tool_exits_zero(self):
        payload = {"tool_name": "Edit", "tool_input": {"command": "hermes chat"}}
        result = subprocess.run(
            [sys.executable, "-m", "team", "free-check"],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0