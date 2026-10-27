"""Claude build mode: the plugin, the gates, the runner's argv and env."""

import json
import subprocess

import pytest

from team import claude_roles, claude_run
from tests.team.test_roles import write_role


class FakeRunner:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    def __call__(self, cmd, **kwargs):
        self.calls.append((cmd, kwargs))
        answer = self.answers.pop(0) if self.answers else (0, "", "")
        if isinstance(answer, Exception):
            raise answer
        rc, out, err = answer
        return subprocess.CompletedProcess(cmd, rc, stdout=out, stderr=err)


def test_generate_plugin_and_regenerate_on_change(tmp_path, team_state):
    write_role(tmp_path, "implement", skills=("commit-before-done",))
    out = claude_roles.generate_plugin(tmp_path, "implement")
    assert out == team_state / "claude" / "implement"
    agent = (out / "agents" / "implement.md").read_text()
    assert agent.startswith(
        '---\nname: implement\ndescription: "Builds one task."\n---'
    )
    assert "# implement" in agent and claude_roles.BUILD_RULES in agent
    assert (out / "skills" / "commit-before-done" / "SKILL.md").exists()
    hooks = json.loads((out / "hooks" / "hooks.json").read_text())
    assert (
        hooks["hooks"]["PreToolUse"][0]["matcher"]
        == "Edit|Write|MultiEdit|NotebookEdit"
    )
    assert "-m team hook stop" in hooks["hooks"]["Stop"][0]["hooks"][0]["command"]
    plugin = json.loads((out / ".claude-plugin" / "plugin.json").read_text())
    assert plugin["name"] == "team-implement"
    stamp = (out / ".claude-plugin" / "plugin.json").stat().st_mtime
    assert claude_roles.generate_plugin(tmp_path, "implement") == out
    assert (out / ".claude-plugin" / "plugin.json").stat().st_mtime == stamp
    soul = tmp_path / "team" / "roles" / "implement" / "SOUL.md"
    soul.write_text("# implement v2\n")
    import os

    os.utime(soul, (stamp + 10, stamp + 10))
    claude_roles.generate_plugin(tmp_path, "implement")
    assert "# implement v2" in (out / "agents" / "implement.md").read_text()
    with pytest.raises(ValueError, match="unknown role"):
        claude_roles.generate_plugin(tmp_path, "ghost")
    assert claude_roles.agent_name("implement") == "team-implement:implement"
    assert claude_roles.role_tools(tmp_path, "implement") == [
        "Bash",
        "Read",
        "Edit",
        "Write",
        "Glob",
        "Grep",
    ]
    assert claude_roles.map_tools(["read-only", "file", "kanban"]) == [
        "Read",
        "Grep",
        "Glob",
        "Edit",
        "Write",
    ]


def test_settings_file_keeps_only_what_protects_eli(tmp_path, team_state):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / "settings.json").write_text(
        json.dumps(
            {
                "permissions": {"deny": ["Read(./.env)"], "allow": ["Bash(ls)"]},
                "hooks": {
                    "PreToolUse": [{"matcher": "Bash", "hooks": []}],
                    "Stop": [{"hooks": []}],
                },
                "enabledPlugins": {"x": True},
            }
        )
    )
    path = claude_roles.settings_file(home=home)
    data = json.loads(path.read_text())
    assert data == {
        "permissions": {"deny": ["Read(./.env)"]},
        "claudeMdExcludes": [str(home / ".claude" / "CLAUDE.md")],
        "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": []}]},
    }
    (home / ".claude" / "settings.json").write_text("{broken")
    assert json.loads(claude_roles.settings_file(home=home).read_text())[
        "permissions"
    ] == {"deny": []}


def test_stop_gate(tmp_path, team_state):
    payload = {"cwd": str(tmp_path), "session_id": "s1"}
    dirty = FakeRunner((0, " M a.py\n", ""))
    reason = claude_roles.stop_gate(payload, {}, runner=dirty)
    assert reason.startswith("Uncommitted changes") and "a.py" in reason
    failing = FakeRunner((0, "", ""), (1, "FAILED test_x\n", ""))
    reason = claude_roles.stop_gate(payload, {"TEAM_CHECK": "pytest"}, runner=failing)
    assert "`pytest` fails" in reason and "FAILED test_x" in reason
    assert failing.calls[1][0] == ["/bin/sh", "-c", "pytest"]
    clean = FakeRunner((0, "", ""), (0, "ok", ""))
    assert (
        claude_roles.stop_gate(payload, {"TEAM_CHECK": "pytest"}, runner=clean) is None
    )
    counter = team_state / "claude" / "stops" / "s1"
    assert counter.read_text() == "2"
    counter.write_text("3")
    assert (
        claude_roles.stop_gate(
            payload, {"TEAM_CHECK": "pytest"}, runner=FakeRunner((0, " M a.py", ""))
        )
        is None
    )
    assert (
        claude_roles.stop_gate(
            payload, {"TEAM_STOP_RETRIES": "5"}, runner=FakeRunner((0, " M a.py", ""))
        )
        is not None
    )


def test_edit_gate(tmp_path):
    env = {"TEAM_ALLOWED_FILES": "a.py:tests/*.py:docs/"}
    cwd = str(tmp_path)

    def payload(tool, path):
        return {"cwd": cwd, "tool_name": tool, "tool_input": {"file_path": path}}

    assert claude_roles.edit_gate(payload("Edit", "a.py"), env) is None
    assert (
        claude_roles.edit_gate(
            payload("Write", str(tmp_path / "tests" / "test_a.py")), env
        )
        is None
    )
    assert claude_roles.edit_gate(payload("Write", "docs/deep/x.md"), env) is None
    assert "b.py is not one of this task's files" in claude_roles.edit_gate(
        payload("Edit", "b.py"), env
    )
    assert "outside the worktree" in claude_roles.edit_gate(
        payload("Edit", "/etc/passwd"), env
    )
    assert claude_roles.edit_gate(payload("Read", "b.py"), env) is None
    assert claude_roles.edit_gate(payload("Edit", "b.py"), {}) is None
    assert (
        claude_roles.edit_gate({"cwd": cwd, "tool_name": "Edit", "tool_input": {}}, env)
        is None
    )


def test_clean_env_and_subscription_ready(monkeypatch):
    for name in claude_run.SCRUB:
        monkeypatch.setenv(name, "x")
    monkeypatch.setenv("KEEP", "y")
    env = claude_run.clean_env()
    assert not (set(claude_run.SCRUB) & set(env)) and env["KEEP"] == "y"
    ok, why = claude_run.subscription_ready(
        FakeRunner((0, json.dumps({"loggedIn": True, "authMethod": "claude.ai"}), ""))
    )
    assert ok and why == "claude.ai"
    ok, why = claude_run.subscription_ready(
        FakeRunner((0, json.dumps({"loggedIn": True, "authMethod": "console"}), ""))
    )
    assert not ok and "console" in why
    ok, why = claude_run.subscription_ready(FakeRunner((0, "nope", "")))
    assert not ok and "no JSON" in why
    ok, why = claude_run.subscription_ready(FakeRunner(OSError("no claude")))
    assert not ok and "no claude" in why


def test_run_build_mode_argv_env_and_results(tmp_path, team_state, monkeypatch):
    write_role(tmp_path, "implement", skills=("commit-before-done",))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-paid")
    out = {
        "is_error": False,
        "result": "done",
        "session_id": "sess-1",
        "num_turns": 7,
        "usage": {"input_tokens": 10, "output_tokens": 2},
        "structured_output": {"a": 1},
    }
    r = FakeRunner((0, json.dumps(out), ""))
    res = claude_run.run(
        tmp_path,
        "do it",
        role="implement",
        root=tmp_path,
        check="pytest -q",
        allowed_files=["a.py", "tests/"],
        decision_file=tmp_path / "d.json",
        max_turns=12,
        timeout_s=99,
        runner=r,
    )
    assert res["ok"] and res["session_id"] == "sess-1" and res["num_turns"] == 7
    assert res["usage"]["input_tokens"] == 10 and res["structured"] == {"a": 1}
    cmd, kwargs = r.calls[0]
    assert cmd[:7] == [
        "claude",
        "-p",
        "do it",
        "--output-format",
        "json",
        "--max-turns",
        "12",
    ]
    assert "--dangerously-skip-permissions" in cmd
    assert cmd[cmd.index("--setting-sources") + 1] == "project,local"
    assert cmd[cmd.index("--settings") + 1] == str(
        team_state / "claude" / "settings.json"
    )
    assert "--strict-mcp-config" in cmd
    assert cmd[cmd.index("--plugin-dir") + 1] == str(
        team_state / "claude" / "implement"
    )
    assert cmd[cmd.index("--agent") + 1] == "team-implement:implement"
    assert cmd[cmd.index("--allowedTools") + 1] == "Bash,Read,Edit,Write,Glob,Grep"
    env = kwargs["env"]
    assert "ANTHROPIC_API_KEY" not in env
    assert (
        env["TEAM_CHECK"] == "pytest -q" and env["TEAM_ALLOWED_FILES"] == "a.py:tests/"
    )
    assert env["TEAM_DECISION_FILE"] == str(tmp_path / "d.json")
    assert (
        kwargs["cwd"] == tmp_path
        and kwargs["timeout"] == 99
        and kwargs["stdin"] == subprocess.DEVNULL
    )

    assert (
        claude_run.run(tmp_path, "x", runner=FakeRunner((1, "", "boom")))["result"]
        == "boom"
    )
    assert (
        claude_run.run(tmp_path, "x", runner=FakeRunner((0, "not json", "")))["result"]
        == "output is not valid JSON"
    )
    res = claude_run.run(
        tmp_path,
        "x",
        runner=FakeRunner((0, json.dumps({"is_error": True, "result": "bad"}), "")),
    )
    assert not res["ok"] and res["result"] == "bad"
    res = claude_run.run(
        tmp_path,
        "x",
        timeout_s=3,
        runner=FakeRunner(subprocess.TimeoutExpired("claude", 3)),
    )
    assert not res["ok"] and res["result"] == "timeout after 3s"
    plain = FakeRunner((0, json.dumps(out), ""))
    claude_run.run(
        tmp_path,
        "x",
        tools=["Read"],
        model="sonnet",
        json_schema={"type": "object"},
        runner=plain,
    )
    cmd = plain.calls[0][0]
    assert "--setting-sources" not in cmd and cmd[cmd.index("--model") + 1] == "sonnet"
    assert cmd[cmd.index("--json-schema") + 1] == '{"type": "object"}'
