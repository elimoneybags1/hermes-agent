"""Claude Code plugin generation from Team roles."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

import tomli


BUILD_RULES = (
    "No person is reading this session. Never ask a question and never wait for "
    "an answer. If a choice belongs to Eli (scope, a dependency, cost, a design "
    "he would notice), write it to $TEAM_DECISION_FILE as JSON {question, "
    "context, options, recommendation} and stop. Touch only the files your task "
    "names. Finish only when the check passes and your work is committed."
)


def _read_role_toml(role_dir: Path) -> dict[str, Any]:
    """Read role.toml from a role directory."""
    toml_path = role_dir / "role.toml"
    with open(toml_path, "rb") as f:
        return tomli.load(f)


def _read_soul(role_dir: Path) -> str:
    """Read SOUL.md from a role directory."""
    soul_path = role_dir / "SOUL.md"
    return soul_path.read_text()


def _read_skills(role_dir: Path) -> dict[str, str]:
    """Read all skills from a role's skills directory."""
    skills_dir = role_dir / "skills"
    skills = {}
    if skills_dir.exists():
        for skill_path in skills_dir.rglob("SKILL.md"):
            # Get relative path from skills_dir
            rel = skill_path.relative_to(skills_dir)
            skills[str(rel)] = skill_path.read_text()
    return skills


def _map_tools_to_claude(tools: list[str]) -> list[str]:
    """Map Team tools to Claude Code tools."""
    mapping = {
        "file": ["Read", "Edit", "Write", "Glob", "Grep"],
        "terminal": ["Bash"],
        "read-only": ["Read", "Grep", "Glob"],
        "todo": [],
        "skills": [],
    }
    result = []
    for tool in tools:
        result.extend(mapping.get(tool, []))
    # Deduplicate while preserving order
    seen = set()
    deduped = []
    for t in result:
        if t not in seen:
            seen.add(t)
            deduped.append(t)
    return deduped


def plugin_dir(state_dir: Path, role: str) -> Path:
    """Return the plugin directory path for a role."""
    return state_dir / "claude" / role


def _plugin_needs_regen(plugin_path: Path, role_dir: Path) -> bool:
    """Check if plugin needs regeneration by comparing mtimes."""
    if not plugin_path.exists():
        return True
    plugin_mtime = plugin_path.stat().st_mtime
    # Check all source files
    for src in [role_dir / "role.toml", role_dir / "SOUL.md"]:
        if src.exists() and src.stat().st_mtime > plugin_mtime:
            return True
    # Check skills
    skills_dir = role_dir / "skills"
    if skills_dir.exists():
        for skill_path in skills_dir.rglob("SKILL.md"):
            if skill_path.stat().st_mtime > plugin_mtime:
                return True
    return False


def generate_plugin(state_dir: Path, role: str) -> Path:
    """Generate a Claude Code plugin from a Team role.

    Creates the plugin structure under $TEAM_STATE/claude/<role>/:
    - .claude-plugin/plugin.json
    - agents/<role>.md (with YAML frontmatter and SOUL.md + build rules)
    - skills/<skill>/SKILL.md (copied from role)
    - hooks/hooks.json (Stop and PreToolUse gates)

    Returns the plugin directory path.
    """
    role_dir = state_dir / "roles" / role
    plugin_path = plugin_dir(state_dir, role)
    
    if not _plugin_needs_regen(plugin_path, role_dir):
        return plugin_path
    
    # Remove old plugin if exists
    if plugin_path.exists():
        shutil.rmtree(plugin_path)
    
    # Read role data
    role_data = _read_role_toml(role_dir)
    soul = _read_soul(role_dir)
    skills = _read_skills(role_dir)
    tools = _map_tools_to_claude(role_data.get("tools", []))
    
    # Create directories
    (plugin_path / "agents").mkdir(parents=True, exist_ok=True)
    (plugin_path / "skills").mkdir(parents=True, exist_ok=True)
    (plugin_path / "hooks").mkdir(parents=True, exist_ok=True)
    
    # Write .claude-plugin/plugin.json
    plugin_json = {
        "name": f"team-{role}",
        "version": "0.1.0",
        "description": f"Team {role} role plugin for build mode",
    }
    (plugin_path / ".claude-plugin").mkdir(exist_ok=True)
    (plugin_path / ".claude-plugin" / "plugin.json").write_text(
        json.dumps(plugin_json, indent=2)
    )
    
    # Write agents/<role>.md with frontmatter
    agent_content = (
        f"---\n"
        f"name: {role}\n"
        f"description: {role_data.get('does', '')}\n"
        f"---\n\n"
        f"{soul}\n\n"
        f"{BUILD_RULES}\n"
    )
    (plugin_path / "agents" / f"{role}.md").write_text(agent_content)
    
    # Write skills
    for skill_rel, skill_content in skills.items():
        skill_path = plugin_path / "skills" / skill_rel
        skill_path.parent.mkdir(parents=True, exist_ok=True)
        skill_path.write_text(skill_content)
    
    # Write hooks/hooks.json
    hooks = {
        "hooks": {
            "Stop": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": "team hook stop",
                        }
                    ]
                }
            ],
            "PreToolUse": [
                {
                    "matcher": "Edit|Write|MultiEdit|NotebookEdit",
                    "hooks": [
                        {
                            "type": "command",
                            "command": "team hook pretool",
                        }
                    ]
                }
            ],
        }
    }
    (plugin_path / "hooks" / "hooks.json").write_text(json.dumps(hooks, indent=2))
    
    return plugin_path


def get_role_tools(role: str, state_dir: Path | None = None) -> list[str]:
    """Get the mapped Claude tools for a role."""
    if state_dir is None:
        from team import ledger
        state_dir = ledger.state_dir()
    role_dir = state_dir / "roles" / role
    role_data = _read_role_toml(role_dir)
    return _map_tools_to_claude(role_data.get("tools", []))