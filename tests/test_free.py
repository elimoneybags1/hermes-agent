#!/usr/bin/env python3
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

    def test_proxy_free_only_while_its_config_checks(self, monkeypatch):
        import team.free as free
        monkeypatch.setattr(free, "proxy_is_free", lambda: True)
        for name in ("team-auto", "tier-simple", "tier-medium", "tier-complex", "tier-reasoning"):
            assert is_free_model(name) is True
        assert is_free_model("tier-paid") is False
        monkeypatch.setattr(free, "proxy_is_free", lambda: False)
        assert is_free_model("team-auto") is False
        assert is_free_model("tier-complex") is False

    def test_proxy_check_fails_closed(self, monkeypatch, tmp_path):
        import team.free as free
        bad = tmp_path / "build.py"
        bad.write_text("import sys; sys.exit(1)\n")
        monkeypatch.setattr(free, "_PROXY_BUILD", bad)
        free.proxy_is_free.cache_clear()
        try:
            assert free.proxy_is_free() is False
        finally:
            free.proxy_is_free.cache_clear()

    def test_proxy_config_in_repo_checks(self):
        import team.free as free
        free.proxy_is_free.cache_clear()
        assert free.proxy_is_free() is True


class TestCheckCommand:
    def test_allowed_free_chat(self):
        cmd = 'hermes chat -m "model:free"'
        # No team-* profile -> refused
        reason = check_command(cmd)
        assert reason is not None
        assert "requires explicit -m/--model with a free model, or -p/--profile team-*" in reason

    def test_allowed_free_chat_long_form(self):
        cmd = 'hermes chat --model="model:free"'
        # No team-* profile -> refused
        reason = check_command(cmd)
        assert reason is not None
        assert "requires explicit -m/--model with a free model, or -p/--profile team-*" in reason

    def test_allowed_free_exact_model(self):
        cmd = 'hermes chat -m gemini-2.5-flash'
        # No team-* profile -> refused
        reason = check_command(cmd)
        assert reason is not None
        assert "requires explicit -m/--model with a free model, or -p/--profile team-*" in reason

    def test_missing_model_refused(self):
        cmd = 'hermes chat'
        reason = check_command(cmd)
        assert reason is not None
        assert "requires explicit -m/--model" in reason

    def test_paid_model_refused(self):
        cmd = 'hermes chat -m gpt-4'
        # Even with a team-* profile, a non-free model is refused
        reason = check_command(cmd)
        assert reason is not None
        # First refusal is about profile, but if profile were team-*, then "not free"
        # Since there's no profile, it's about the profile requirement
        assert "requires explicit -m/--model with a free model, or -p/--profile team-*" in reason

    def test_paid_provider_refused(self):
        cmd = 'hermes chat -m "model:free" --provider openai'
        # No team-* profile -> refused for profile reason first
        reason = check_command(cmd)
        assert reason is not None
        assert "requires explicit -m/--model with a free model, or -p/--profile team-*" in reason

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
        # No team-* profile -> refused
        reason = check_command(cmd)
        assert reason is not None
        assert "requires -p/--profile team-*" in reason

    def test_implicit_chat_without_model_refused(self):
        cmd = 'hermes -q "test"'
        reason = check_command(cmd)
        assert reason is not None
        assert "requires -p/--profile team-*" in reason

    def test_model_equals_form(self):
        cmd = 'hermes chat --model=gemini-2.5-flash'
        # No team-* profile -> refused
        reason = check_command(cmd)
        assert reason is not None
        assert "requires explicit -m/--model with a free model, or -p/--profile team-*" in reason

    def test_provider_equals_form(self):
        cmd = 'hermes chat -m "model:free" --provider=openai'
        # No team-* profile -> refused for profile reason first
        reason = check_command(cmd)
        assert reason is not None
        assert "requires explicit -m/--model with a free model, or -p/--profile team-*" in reason


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
        # No team-* profile -> refused
        assert code == 2

    def test_missing_model_refused_cli(self):
        code, _, stderr = self._run_free_check('hermes chat')
        assert code == 2
        assert "requires explicit -m/--model" in stderr

    def test_paid_model_refused_cli(self):
        code, _, stderr = self._run_free_check('hermes chat -m gpt-4')
        # No team-* profile -> refused for profile reason
        assert code == 2
        assert "requires explicit -m/--model with a free model, or -p/--profile team-*" in stderr

    def test_paid_provider_refused_cli(self):
        code, _, stderr = self._run_free_check('hermes chat -m "model:free" --provider openai')
        # No team-* profile -> refused for profile reason
        assert code == 2
        assert "requires explicit -m/--model with a free model, or -p/--profile team-*" in stderr

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


class TestReviewFindings:
    """Tests for the 5 review findings."""

    # Finding 1: Newlines and '&' as separators
    def test_newline_separator_refused(self):
        cmd = 'echo hi\nhermes chat -q x'
        reason = check_command(cmd)
        assert reason is not None
        assert "requires explicit -m/--model" in reason

    def test_ampersand_separator_refused(self):
        cmd = 'echo hi & hermes chat -q x'
        reason = check_command(cmd)
        assert reason is not None
        assert "requires explicit -m/--model" in reason

    # Finding 2: Subshells, brace groups, function bodies, $(), backticks, bash -c
    def test_function_body_refused(self):
        # The call inside the body is parsed, so the specific reason is reported.
        cmd = 'run() { s=$1; hermes chat -Q --yolo --in /tmp/w --query-file /tmp/b.md > /tmp/o 2>&1; }; run a & run b & wait'
        reason = check_command(cmd)
        assert reason is not None
        assert "requires explicit -m/--model" in reason

    def test_function_body_with_free_model_allowed(self):
        # Now requires a team-* profile even with a free model
        cmd = 'run() { s=$1; hermes chat -Q --yolo --in /tmp/w --query-file /tmp/b.md -m nvidia/nemotron-3-ultra-550b-a55b:free > /tmp/o 2>&1; }; run a & run b & wait'
        reason = check_command(cmd)
        assert reason is not None
        assert "requires explicit -m/--model with a free model, or -p/--profile team-*" in reason

    def test_brace_group_refused(self):
        cmd = '{ hermes chat -q test; }'
        reason = check_command(cmd)
        assert reason is not None
        assert "requires explicit -m/--model" in reason

    def test_subshell_paren_refused(self):
        cmd = '( hermes chat -q test )'
        reason = check_command(cmd)
        assert reason is not None
        assert "requires explicit -m/--model" in reason

    def test_command_substitution_dollar_paren_refused(self):
        cmd = 'echo $(hermes chat -q test)'
        reason = check_command(cmd)
        assert reason is not None
        assert "requires explicit -m/--model" in reason

    def test_backticks_refused(self):
        cmd = 'echo `hermes chat -q test`'
        reason = check_command(cmd)
        assert reason is not None
        assert "requires explicit -m/--model" in reason

    def test_bash_c_refused(self):
        cmd = 'bash -c "hermes chat -q test"'
        reason = check_command(cmd)
        assert reason is not None
        assert "requires explicit -m/--model" in reason

    def test_sh_c_refused(self):
        cmd = 'sh -c "hermes chat -q test"'
        reason = check_command(cmd)
        assert reason is not None
        assert "requires explicit -m/--model" in reason

    def test_zsh_c_refused(self):
        cmd = 'zsh -c "hermes chat -q test"'
        reason = check_command(cmd)
        assert reason is not None
        assert "requires explicit -m/--model" in reason

    # Finding 3: Leading VAR=value assignments and wrappers
    def test_var_assignment_before_hermes_allowed(self):
        cmd = 'FOO=bar hermes chat -m "model:free"'
        # No team-* profile -> refused
        reason = check_command(cmd)
        assert reason is not None
        assert "requires explicit -m/--model with a free model, or -p/--profile team-*" in reason

    def test_env_wrapper_allowed(self):
        cmd = 'env FOO=bar hermes chat -m "model:free"'
        # No team-* profile -> refused
        reason = check_command(cmd)
        assert reason is not None
        assert "requires explicit -m/--model with a free model, or -p/--profile team-*" in reason

    def test_command_wrapper_allowed(self):
        cmd = 'command hermes chat -m "model:free"'
        # No team-* profile -> refused
        reason = check_command(cmd)
        assert reason is not None
        assert "requires explicit -m/--model with a free model, or -p/--profile team-*" in reason

    def test_exec_wrapper_allowed(self):
        cmd = 'exec hermes chat -m "model:free"'
        # No team-* profile -> refused
        reason = check_command(cmd)
        assert reason is not None
        assert "requires explicit -m/--model with a free model, or -p/--profile team-*" in reason

    def test_nohup_wrapper_allowed(self):
        cmd = 'nohup hermes chat -m "model:free"'
        # No team-* profile -> refused
        reason = check_command(cmd)
        assert reason is not None
        assert "requires explicit -m/--model with a free model, or -p/--profile team-*" in reason

    def test_time_wrapper_allowed(self):
        cmd = 'time hermes chat -m "model:free"'
        # No team-* profile -> refused
        reason = check_command(cmd)
        assert reason is not None
        assert "requires explicit -m/--model with a free model, or -p/--profile team-*" in reason

    def test_timeout_wrapper_allowed(self):
        cmd = 'timeout 10 hermes chat -m "model:free"'
        # No team-* profile -> refused
        reason = check_command(cmd)
        assert reason is not None
        assert "requires explicit -m/--model with a free model, or -p/--profile team-*" in reason

    def test_nice_wrapper_allowed(self):
        cmd = 'nice hermes chat -m "model:free"'
        # No team-* profile -> refused
        reason = check_command(cmd)
        assert reason is not None
        assert "requires explicit -m/--model with a free model, or -p/--profile team-*" in reason

    def test_uv_run_wrapper_allowed(self):
        cmd = 'uv run hermes chat -m "model:free"'
        # No team-* profile -> refused
        reason = check_command(cmd)
        assert reason is not None
        assert "requires explicit -m/--model with a free model, or -p/--profile team-*" in reason

    def test_uvx_wrapper_allowed(self):
        cmd = 'uvx hermes chat -m "model:free"'
        # No team-* profile -> refused
        reason = check_command(cmd)
        assert reason is not None
        assert "requires explicit -m/--model with a free model, or -p/--profile team-*" in reason

    def test_var_assignment_without_model_refused(self):
        cmd = 'FOO=bar hermes chat -q test'
        reason = check_command(cmd)
        assert reason is not None
        assert "requires explicit -m/--model" in reason

    # Finding 4: --model X (space-separated) support
    def test_model_space_separated_allowed(self):
        cmd = 'hermes chat --model "model:free"'
        # No team-* profile -> refused
        reason = check_command(cmd)
        assert reason is not None
        assert "requires explicit -m/--model with a free model, or -p/--profile team-*" in reason

    def test_model_space_separated_without_model_refused(self):
        cmd = 'hermes chat --model'
        reason = check_command(cmd)
        assert reason is not None
        assert "requires explicit -m/--model" in reason

    def test_provider_space_separated_refused(self):
        cmd = 'hermes chat -m "model:free" --provider openai'
        # No team-* profile -> refused for profile reason first
        reason = check_command(cmd)
        assert reason is not None
        assert "requires explicit -m/--model with a free model, or -p/--profile team-*" in reason

    def test_provider_equals_form_refused(self):
        cmd = 'hermes chat -m "model:free" --provider=openai'
        # No team-* profile -> refused for profile reason first
        reason = check_command(cmd)
        assert reason is not None
        assert "requires explicit -m/--model with a free model, or -p/--profile team-*" in reason

    # Finding 5: Backstop - pgrep and similar harmless commands are refused
    def test_pgrep_hermes_chat_refused(self):
        cmd = 'pgrep -f "hermes chat"'
        reason = check_command(cmd)
        assert reason is not None
        assert "cannot verify hermes invocation is pinned to a free model" in reason

    def test_hermes_sessions_export_allowed(self):
        cmd = 'hermes sessions export - --session-id X --format jsonl'
        assert check_command(cmd) is None


class TestFailOpen:
    """Each command here got past the guard before; all must be refused."""

    def test_bare_hermes_refused(self):
        # No subcommand starts an interactive chat on the default model.
        assert check_command('hermes') is not None

    def test_implicit_chat_paid_model_without_query_refused(self):
        assert check_command('hermes -m anthropic/claude-sonnet-4') is not None

    def test_other_subcommand_refused(self):
        assert check_command('hermes cron run nightly') is not None

    def test_other_subcommand_with_free_model_refused(self):
        assert check_command('hermes -m x:free config set model anthropic/claude-sonnet-4') is not None

    def test_fallback_add_refused(self):
        assert check_command('hermes fallback add anthropic') is not None

    def test_sessions_other_action_refused(self):
        assert check_command('hermes sessions delete X') is not None

    def test_repeated_model_last_wins_refused(self):
        assert check_command('hermes chat -m x:free -m gpt-4') is not None

    def test_attached_short_model_refused(self):
        assert check_command('hermes chat -m x:free -mgpt-4') is not None

    def test_clustered_short_model_refused(self):
        assert check_command('hermes chat -m x:free -Qm gpt-4') is not None

    def test_abbreviated_model_refused(self):
        assert check_command('hermes chat -m x:free --mod gpt-4') is not None

    def test_abbreviated_provider_refused(self):
        assert check_command('hermes chat -m x:free --prov anthropic') is not None

    def test_provider_case_refused(self):
        assert check_command('hermes chat -m x:free --provider Anthropic') is not None

    def test_unlisted_provider_refused(self):
        # gemini-2.5-flash is free on Google's tier, not through OpenRouter.
        assert check_command('hermes chat -m gemini-2.5-flash --provider openrouter') is not None

    def test_uppercase_command_refused(self):
        # macOS filesystems are case-insensitive: HERMES runs hermes.
        assert check_command('HERMES -m gpt-4') is not None

    def test_unknown_wrapper_refused(self):
        assert check_command('sudo -u eli hermes -m gpt-4') is not None

    def test_xargs_refused(self):
        assert check_command('echo hi | xargs hermes -m gpt-4') is not None

    def test_substitution_in_allowed_command_refused(self):
        assert check_command('hermes sessions list $(hermes -m gpt-4)') is not None

    def test_substitution_in_free_chat_refused(self):
        assert check_command('hermes chat -m x:free -q "$(hermes -m gpt-4)"') is not None

    def test_redirect_ampersand_does_not_hide_args(self):
        assert check_command('hermes chat -m x:free -q hi 2>&1 --provider anthropic') is not None

    def test_unquoted_variable_arg_refused(self):
        # $EXTRA may expand to "--provider anthropic".
        assert check_command('hermes chat -m x:free -q hi $EXTRA') is not None

    def test_brace_expansion_arg_refused(self):
        assert check_command('hermes chat -m x:free -q hi {--provider,anthropic}') is not None

    def test_variable_command_refused(self):
        assert check_command('H=hermes; $H -m gpt-4') is not None

    def test_pipe_into_shell_refused(self):
        assert check_command('echo hermes -m gpt-4 | sh') is not None

    def test_glob_command_refused(self):
        assert check_command('herm?s -m gpt-4') is not None

    def test_separator_inside_quotes_refused(self):
        assert check_command('bash -c "true; hermes -m gpt-4"') is not None

    def test_process_substitution_refused(self):
        assert check_command('bash <(echo hermes -m gpt-4)') is not None

    def test_eval_string_refused(self):
        assert check_command('eval "hermes -m gpt-4"') is not None


class TestStillAllowed:
    """Ordinary commands the stricter parsing must not refuse."""

    def test_top_level_model_before_chat(self):
        # No team-* profile -> refused
        cmd = 'hermes -m x:free chat -q hi'
        reason = check_command(cmd)
        assert reason is not None
        assert "requires explicit -m/--model with a free model, or -p/--profile team-*" in reason

    def test_line_continuation(self):
        # No team-* profile -> refused
        cmd = 'hermes chat -m x:free \\\n  -Q --query-file /tmp/b.md'
        reason = check_command(cmd)
        assert reason is not None
        assert "requires explicit -m/--model with a free model, or -p/--profile team-*" in reason

    def test_quoted_variable_as_option_value(self):
        # No team-* profile -> refused
        cmd = 'hermes chat -m x:free --query-file "$BRIEF" --in "$WT"'
        reason = check_command(cmd)
        assert reason is not None
        assert "requires explicit -m/--model with a free model, or -p/--profile team-*" in reason

    def test_grep_for_hermes(self):
        assert check_command('grep -rn hermes team/') is None

    def test_hermes_state_path(self):
        assert check_command('ls ~/.hermes/sessions') is None

    def test_pipe_out_of_sessions_list(self):
        assert check_command('hermes sessions list | grep chat') is None

    # New tests for team-* profile rules
    def test_team_profile_config_refused(self):
        assert check_command('hermes -p team-worker config set model.default openai/gpt-5') is not None

    def test_team_profile_kanban_dispatch_allowed(self):
        assert check_command('hermes -p team-worker kanban --board team dispatch --json') is None

    def test_team_profile_kanban_list_allowed(self):
        assert check_command('hermes -p team-worker kanban list') is None

    def test_team_profile_sessions_list_allowed(self):
        assert check_command('hermes -p team-worker sessions list') is None

    def test_team_profile_dashboard_allowed(self):
        assert check_command('hermes -p team-worker dashboard') is None

    def test_team_profile_skills_list_allowed(self):
        assert check_command('hermes -p team-worker skills list') is None

    def test_team_profile_profile_list_allowed(self):
        assert check_command('hermes -p team-worker profile list') is None

    def test_team_profile_profile_describe_allowed(self):
        assert check_command('hermes -p team-worker profile describe') is None

    def test_default_profile_kanban_list_allowed(self):
        assert check_command('hermes kanban list') is None

    def test_default_profile_kanban_show_allowed(self):
        assert check_command('hermes kanban show') is None

    def test_default_profile_kanban_dispatch_refused(self):
        assert check_command('hermes kanban dispatch') is not None

    def test_default_profile_sessions_list_allowed(self):
        assert check_command('hermes sessions list') is None

    def test_default_profile_sessions_export_allowed(self):
        assert check_command('hermes sessions export') is None

    def test_default_profile_profile_list_allowed(self):
        assert check_command('hermes profile list') is None

    def test_default_profile_profile_describe_refused(self):
        assert check_command('hermes profile describe') is not None

    def test_default_profile_fallback_list_allowed(self):
        assert check_command('hermes fallback list') is None

    def test_default_profile_dashboard_refused(self):
        assert check_command('hermes dashboard') is not None

    def test_default_profile_skills_list_refused(self):
        assert check_command('hermes skills list') is not None

    def test_default_profile_sessions_delete_refused(self):
        assert check_command('hermes sessions delete X') is not None

    def test_team_profile_auth_refused(self):
        assert check_command('hermes -p team-worker auth login') is not None

    def test_team_profile_setup_refused(self):
        assert check_command('hermes -p team-worker setup') is not None

    def test_team_profile_model_refused(self):
        assert check_command('hermes -p team-worker model') is not None

    def test_team_profile_gateway_refused(self):
        assert check_command('hermes -p team-worker gateway') is not None

    def test_team_profile_tools_refused(self):
        assert check_command('hermes -p team-worker tools') is not None