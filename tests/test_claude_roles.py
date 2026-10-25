"""Tests for team/claude_roles.py: the build-mode plugin, settings and gates."""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

from team import claude_roles


def _role(root: Path, name: str = "implement", tools=("terminal", "file", "skills")) -> Path:
    role = root / "roles" / name
    (role / "skills" / "commit-first").mkdir(parents=True)
    tools_toml = ", ".join(f'"{t}"' for t in tools)
    (role / "role.toml").write_text(f'name = "{name}"\ndoes = "Builds: one task."\ntools = [{tools_toml}]\n')
    (role / "SOUL.md").write_text("You build one task.")
    (role / "skills" / "commit-first" / "SKILL.md").write_text("---\nname: commit-first\n---\nCommit.")
    return role


def test_map_tools_to_claude():
    assert claude_roles._map_tools_to_claude(["file"]) == ["Read", "Edit", "Write", "Glob", "Grep"]
    assert claude_roles._map_tools_to_claude(["terminal"]) == ["Bash"]
    assert claude_roles._map_tools_to_claude(["read-only"]) == ["Read", "Grep", "Glob"]
    assert claude_roles._map_tools_to_claude(["todo", "skills"]) == ["TodoWrite", "Skill"]
    assert claude_roles._map_tools_to_claude(["browser"]) == []
    assert claude_roles._map_tools_to_claude(["file", "read-only"]) == ["Read", "Edit", "Write", "Glob", "Grep"]


def test_generate_plugin(tmp_path):
    _role(tmp_path / "repo")
    out = claude_roles.generate_plugin(tmp_path / "repo", "implement", state_dir=tmp_path / "state")

    assert out == tmp_path / "state" / "claude" / "implement"
    assert json.loads((out / ".claude-plugin" / "plugin.json").read_text())["name"] == "team-implement"
    agent = (out / "agents" / "implement.md").read_text()
    assert agent.startswith('---\nname: implement\ndescription: "Builds: one task."\n---')
    assert "You build one task." in agent
    assert claude_roles.BUILD_RULES in agent
    assert (out / "skills" / "commit-first" / "SKILL.md").read_text().endswith("Commit.")
    hooks = json.loads((out / "hooks" / "hooks.json").read_text())["hooks"]
    assert hooks["Stop"][0]["hooks"][0]["command"].endswith("-m team hook stop")
    assert hooks["PreToolUse"][0]["hooks"][0]["command"].endswith("-m team hook pretool")
    assert not (tmp_path / "repo" / ".claude-plugin").exists()


def test_generate_plugin_regenerates_only_on_change(tmp_path):
    role = _role(tmp_path / "repo")
    out = claude_roles.generate_plugin(tmp_path / "repo", "implement", state_dir=tmp_path / "state")
    marker = out / "agents" / "marker"
    marker.write_text("x")
    claude_roles.generate_plugin(tmp_path / "repo", "implement", state_dir=tmp_path / "state")
    assert marker.exists()

    later = time.time() + 5
    (role / "SOUL.md").write_text("Changed soul.")
    os.utime(role / "SOUL.md", (later, later))
    claude_roles.generate_plugin(tmp_path / "repo", "implement", state_dir=tmp_path / "state")
    assert not marker.exists()
    assert "Changed soul." in (out / "agents" / "implement.md").read_text()


def test_role_tools(tmp_path):
    _role(tmp_path, tools=("read-only",))
    assert claude_roles.role_tools(tmp_path, "implement") == ["Read", "Grep", "Glob"]


def test_settings_file_keeps_protection_drops_the_rest(tmp_path):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / "settings.json").write_text(json.dumps({
        "permissions": {"allow": ["Bash(ls:*)"], "deny": ["Read(**/.env)"], "defaultMode": "auto"},
        "hooks": {"PreToolUse": [{"matcher": "*", "hooks": [{"type": "command", "command": "confine"}]}],
                  "SessionStart": [{"hooks": [{"type": "command", "command": "herdr"}]}]},
        "enabledPlugins": {"x@y": True},
    }))
    out = claude_roles.settings_file(state_dir=tmp_path / "state", home=home)
    s = json.loads(out.read_text())
    assert s["permissions"] == {"deny": ["Read(**/.env)"]}
    assert s["hooks"] == {"PreToolUse": [{"matcher": "*", "hooks": [{"type": "command", "command": "confine"}]}]}
    assert s["claudeMdExcludes"] == [str(home / ".claude" / "CLAUDE.md")]
    assert "enabledPlugins" not in s


def test_settings_file_without_user_settings(tmp_path):
    s = json.loads(claude_roles.settings_file(state_dir=tmp_path, home=tmp_path / "nohome").read_text())
    assert s["permissions"] == {"deny": []}
    assert "hooks" not in s


def _repo(path: Path) -> Path:
    path.mkdir()
    for cmd in (["init", "-q"], ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
        subprocess.run(["git", *cmd], cwd=path, check=True)
    (path / "a.py").write_text("x = 1\n")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=path, check=True)
    return path


def test_stop_gate_clean_and_passing_allows(tmp_path):
    repo = _repo(tmp_path / "wt")
    payload = {"session_id": "s1", "cwd": str(repo)}
    assert claude_roles.stop_gate(payload, {"TEAM_CHECK": "true"}, state_dir=tmp_path) is None


def test_stop_gate_refuses_dirty_tree(tmp_path):
    repo = _repo(tmp_path / "wt")
    (repo / "a.py").write_text("x = 2\n")
    reason = claude_roles.stop_gate({"session_id": "s1", "cwd": str(repo)}, {}, state_dir=tmp_path)
    assert "Uncommitted changes" in reason and "a.py" in reason


def test_stop_gate_refuses_failing_check_then_gives_up(tmp_path):
    repo = _repo(tmp_path / "wt")
    payload = {"session_id": "s2", "cwd": str(repo)}
    env = {"TEAM_CHECK": "echo broken-test; exit 1", "TEAM_STOP_RETRIES": "2"}
    for _ in range(2):
        reason = claude_roles.stop_gate(payload, env, state_dir=tmp_path)
        assert "broken-test" in reason
    assert claude_roles.stop_gate(payload, env, state_dir=tmp_path) is None


def test_edit_gate(tmp_path):
    env = {"TEAM_ALLOWED_FILES": "team/a.py:tests/*"}

    def edit(path, tool="Edit"):
        return claude_roles.edit_gate({"tool_name": tool, "cwd": str(tmp_path),
                                       "tool_input": {"file_path": path}}, env)

    assert edit(str(tmp_path / "team" / "a.py")) is None
    assert edit("tests/test_a.py", "Write") is None
    assert "not one of this task's files" in edit(str(tmp_path / "team" / "b.py"))
    assert "outside the worktree" in edit("/etc/hosts")
    assert claude_roles.edit_gate({"tool_name": "Edit", "cwd": str(tmp_path),
                                   "tool_input": {"file_path": "/etc/hosts"}}, {}) is None
    assert claude_roles.edit_gate({"tool_name": "Bash", "cwd": str(tmp_path),
                                   "tool_input": {}}, env) is None
