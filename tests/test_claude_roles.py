"""Tests for team/claude_roles.py."""

from __future__ import annotations

import json
import tempfile
import tomli_w
from pathlib import Path

from team import claude_roles


def _make_role_structure(tmp: Path, role: str, role_toml: dict, soul: str, skills: dict[str, str] | None = None) -> Path:
    """Create a role directory structure for testing."""
    role_dir = tmp / "roles" / role
    role_dir.mkdir(parents=True)
    (role_dir / "role.toml").write_bytes(tomli_w.dumps(role_toml).encode())
    (role_dir / "SOUL.md").write_text(soul)
    if skills:
        for skill_rel, content in skills.items():
            skill_path = role_dir / "skills" / skill_rel
            skill_path.parent.mkdir(parents=True, exist_ok=True)
            skill_path.write_text(content)
    return role_dir


def test_map_tools_to_claude():
    """Test tool mapping from Team to Claude Code."""
    # file -> Read, Edit, Write, Glob, Grep
    assert claude_roles._map_tools_to_claude(["file"]) == ["Read", "Edit", "Write", "Glob", "Grep"]
    
    # terminal -> Bash
    assert claude_roles._map_tools_to_claude(["terminal"]) == ["Bash"]
    
    # read-only -> Read, Grep, Glob
    assert claude_roles._map_tools_to_claude(["read-only"]) == ["Read", "Grep", "Glob"]
    
    # todo, skills -> empty
    assert claude_roles._map_tools_to_claude(["todo"]) == []
    assert claude_roles._map_tools_to_claude(["skills"]) == []
    
    # Mixed tools - deduplicated
    result = claude_roles._map_tools_to_claude(["file", "terminal", "read-only"])
    assert "Read" in result
    assert "Edit" in result
    assert "Write" in result
    assert "Glob" in result
    assert "Grep" in result
    assert "Bash" in result
    # Check no duplicates
    assert len(result) == len(set(result))


def test_generate_plugin_basic(tmp_path: Path):
    """Test basic plugin generation."""
    role_toml = {
        "name": "implement",
        "title": "Implementer",
        "does": "Implements features",
        "status": "active",
        "runs_on": {"harness": "hermes", "model": "nvidia/nemotron-3-ultra-550b-a55b:free"},
        "tools": ["file", "terminal"],
        "hands_to": "review",
    }
    soul = "# Implementer\n\nYou implement features."
    skills = {
        "commit-before-done/SKILL.md": "# Commit Before Done\n\nCommit your work.",
    }
    
    role_dir = _make_role_structure(tmp_path, "implement", role_toml, soul, skills)
    
    # Generate plugin
    plugin_path = claude_roles.generate_plugin(tmp_path, "implement")
    
    # Check plugin structure
    assert plugin_path == tmp_path / "claude" / "implement"
    assert (plugin_path / ".claude-plugin" / "plugin.json").exists()
    assert (plugin_path / "agents" / "implement.md").exists()
    assert (plugin_path / "skills" / "commit-before-done" / "SKILL.md").exists()
    assert (plugin_path / "hooks" / "hooks.json").exists()
    
    # Check plugin.json
    plugin_json = json.loads((plugin_path / ".claude-plugin" / "plugin.json").read_text())
    assert plugin_json["name"] == "team-implement"
    assert plugin_json["version"] == "0.1.0"
    
    # Check agent.md
    agent_content = (plugin_path / "agents" / "implement.md").read_text()
    assert agent_content.startswith("---\nname: implement\n")
    assert "You implement features." in agent_content
    assert "No person is reading this session." in agent_content  # BUILD_RULES
    
    # Check skill copied
    skill_content = (plugin_path / "skills" / "commit-before-done" / "SKILL.md").read_text()
    assert skill_content == "# Commit Before Done\n\nCommit your work."
    
    # Check hooks.json
    hooks = json.loads((plugin_path / "hooks" / "hooks.json").read_text())
    assert "hooks" in hooks
    assert "Stop" in hooks["hooks"]
    assert "PreToolUse" in hooks["hooks"]
    assert hooks["hooks"]["Stop"][0]["hooks"][0]["command"] == "team hook stop"
    assert hooks["hooks"]["PreToolUse"][0]["hooks"][0]["command"] == "team hook pretool"


def test_generate_plugin_idempotent(tmp_path: Path):
    """Test that plugin generation is idempotent (no regen if sources unchanged)."""
    role_toml = {
        "name": "implement",
        "title": "Implementer",
        "does": "Implements features",
        "status": "active",
        "runs_on": {"harness": "hermes", "model": "nvidia/nemotron-3-ultra-550b-a55b:free"},
        "tools": ["file"],
    }
    soul = "# Implementer"
    
    role_dir = _make_role_structure(tmp_path, "implement", role_toml, soul)
    
    # First generation
    plugin_path1 = claude_roles.generate_plugin(tmp_path, "implement")
    mtime1 = (plugin_path1 / ".claude-plugin" / "plugin.json").stat().st_mtime
    
    # Second generation (should not regenerate)
    plugin_path2 = claude_roles.generate_plugin(tmp_path, "implement")
    mtime2 = (plugin_path2 / ".claude-plugin" / "plugin.json").stat().st_mtime
    
    assert plugin_path1 == plugin_path2
    assert mtime1 == mtime2


def test_generate_plugin_regenerates_on_change(tmp_path: Path):
    """Test that plugin regenerates when source files change."""
    role_toml = {
        "name": "implement",
        "title": "Implementer",
        "does": "Implements features",
        "status": "active",
        "runs_on": {"harness": "hermes", "model": "nvidia/nemotron-3-ultra-550b-a55b:free"},
        "tools": ["file"],
    }
    soul = "# Implementer v1"
    
    role_dir = _make_role_structure(tmp_path, "implement", role_toml, soul)
    
    # First generation
    plugin_path = claude_roles.generate_plugin(tmp_path, "implement")
    mtime1 = (plugin_path / ".claude-plugin" / "plugin.json").stat().st_mtime
    
    # Modify SOUL.md
    import time
    time.sleep(0.01)  # Ensure mtime changes
    (role_dir / "SOUL.md").write_text("# Implementer v2")
    
    # Second generation
    plugin_path2 = claude_roles.generate_plugin(tmp_path, "implement")
    mtime2 = (plugin_path2 / ".claude-plugin" / "plugin.json").stat().st_mtime
    
    assert mtime2 > mtime1
    # Check content updated
    agent_content = (plugin_path2 / "agents" / "implement.md").read_text()
    assert "Implementer v2" in agent_content


def test_get_role_tools(tmp_path: Path):
    """Test getting role tools from a temp role structure."""
    role_toml = {
        "name": "implement",
        "title": "Implementer",
        "does": "Implements features",
        "status": "active",
        "runs_on": {"harness": "hermes", "model": "nvidia/nemotron-3-ultra-550b-a55b:free"},
        "tools": ["file", "terminal"],
    }
    soul = "# Implementer"
    
    _make_role_structure(tmp_path, "implement", role_toml, soul)
    
    tools = claude_roles.get_role_tools("implement", state_dir=tmp_path)
    assert "Read" in tools
    assert "Edit" in tools
    assert "Write" in tools
    assert "Glob" in tools
    assert "Grep" in tools
    assert "Bash" in tools


def test_plugin_dir():
    """Test plugin_dir helper."""
    from team import ledger
    state_dir = ledger.state_dir()
    plugin_path = claude_roles.plugin_dir(state_dir, "implement")
    assert plugin_path == state_dir / "claude" / "implement"