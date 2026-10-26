"""Hermes profile management for team agents."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

import yaml

from team.agents import _read_role_toml
from team.free import is_free_model


def profile_name(role: str) -> str:
    """Return the Hermes profile name for a team role."""
    return f"team-{role}"


def _hermes_profile_dir(profile: str) -> Path:
    """Get the path to a Hermes profile directory."""
    from team.hermes_runtime import worker_profile_root
    return worker_profile_root() / "profiles" / profile


def require_turn_budget(profile: str, expected: int) -> None:
    """Fail before queueing when the installed native runtime has no role cap."""
    path = _hermes_profile_dir(profile) / "config.yaml"
    data = yaml.safe_load(path.read_text()) if path.exists() else {}
    actual = (data or {}).get("agent", {}).get("max_turns")
    if type(actual) is not int or actual < 1 or actual > expected:
        raise ValueError(f"{profile} needs agent.max_turns <= {expected}; sync the role profile")


def _run_hermes_profile_create(profile: str, clone_from: str, description: str, runner: Any = None) -> None:
    """Create a Hermes profile by cloning from another."""
    if runner is None:
        runner = subprocess.run
    runner(
        [
            "hermes",
            "profile",
            "create",
            profile,
            "--clone-from",
            clone_from,
            "--no-alias",
            "--description",
            description,
        ],
        check=True,
        capture_output=True,
    )


def _write_soul_md(profile: str, text: str) -> None:
    """Write SOUL.md to a Hermes profile."""
    path = _hermes_profile_dir(profile) / "SOUL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    from team.hermes_runtime import FORK
    upstream = (FORK / "SOUL.md").read_text()
    existing = path.read_text() if path.exists() else ""
    combined = existing or text
    if upstream not in combined:
        combined = upstream + "\n\n# Team role specialization\n\n" + combined
    path.write_text(combined)


def _write_skills(profile: str, skills: list[dict[str, Any]]) -> None:
    """Seed the upstream library and role procedures without deleting learned skills."""
    from team.hermes_runtime import FORK
    skills_dir = _hermes_profile_dir(profile) / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)
    for source in (FORK / "skills").rglob("*"):
        if source.is_file():
            destination = skills_dir / source.relative_to(FORK / "skills")
            if not destination.exists():
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)
    for skill in skills:
        path = skills_dir / skill["name"] / "SKILL.md"
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(skill["text"])


def _write_model_default(profile: str, model: str) -> None:
    """Write model.default to a Hermes profile's config.yaml."""
    import yaml
    
    config_path = _hermes_profile_dir(profile) / "config.yaml"
    data = {}
    if config_path.exists():
        with config_path.open() as f:
            data = yaml.safe_load(f) or {}
    
    if "model" not in data:
        data["model"] = {}
    data["model"]["default"] = model
    
    with config_path.open("w") as f:
        yaml.dump(data, f)


def _write_role_profile_config(profile: str, role_tools: list[str], max_turns: int = 40) -> None:
    """Keep Hermes learning/context features; specialize the execution tools only."""
    path = _hermes_profile_dir(profile) / "config.yaml"
    data = yaml.safe_load(path.read_text()) if path.exists() else {}
    data = data or {}
    learning = {"skills", "memory", "session_search", "todo"}
    enabled = sorted(set(role_tools) | learning)
    data.setdefault("platform_toolsets", {})["cli"] = enabled
    data.pop("toolsets", None)
    if isinstance(max_turns, bool) or not isinstance(max_turns, int) or max_turns < 1:
        raise ValueError("role max_turns must be a positive integer")
    data.setdefault("agent", {})["max_turns"] = max_turns
    disabled = data["agent"].get("disabled_toolsets", [])
    data["agent"]["disabled_toolsets"] = [t for t in disabled if t not in learning]
    data.setdefault("skills", {})["project_discovery"] = True
    data.setdefault("memory", {}).update(memory_enabled=True, user_profile_enabled=True)
    data.setdefault("compression", {})["enabled"] = True
    data["compression"].pop("threshold_tokens", None)  # inherit upstream's model-aware threshold
    data.setdefault("auxiliary", {})["free_only"] = True
    data["auxiliary"].setdefault("background_review", {})["enabled"] = True
    # Preserve the free profile's credential and no-paid-fallback policy.
    path.write_text(yaml.safe_dump(data, sort_keys=False))


def _get_role_skills(root: Path, role: str) -> list[dict[str, Any]]:
    """Get all skills for a role from the repo."""
    skills = []
    skills_dir = root / "roles" / role / "skills"
    if skills_dir.is_dir():
        for skill_dir in sorted(skills_dir.iterdir()):
            if skill_dir.is_dir():
                skill_file = skill_dir / "SKILL.md"
                if skill_file.exists():
                    text = skill_file.read_text()
                    skills.append({
                        "name": skill_dir.name,
                        "text": text,
                    })
    return skills


def _get_role_instructions(root: Path, role: str) -> str | None:
    """Get instructions (SOUL.md) for a role."""
    if role == "boss":
        path = root / "plugin" / "agents" / "boss.md"
    else:
        path = root / "roles" / role / "SOUL.md"
    return path.read_text() if path.exists() else None


def sync(root: Path, role: str, runner: Any = None) -> str:
    """Sync a role to its Hermes profile.
    
    Creates the Hermes profile if missing by cloning from team-worker,
    then writes SOUL.md, skills, and model.default.
    
    Refuses non-free models and profiles not named team-*.
    
    Returns a summary of what changed.
    """
    profile = profile_name(role)
    
    # Refuse non-team-* profiles
    if not profile.startswith("team-"):
        raise ValueError(f"refused: profile '{profile}' does not start with 'team-'")
    
    # Check if model is free
    role_data = _read_role_toml(root, role)
    if role_data.get("runs_on", {}).get("harness") == "hermes-subscription":
        from team.hermes_runtime import prepare_profile
        return f"native subscription profile: {prepare_profile(role, root)}"
    model = role_data.get("runs_on", {}).get("model", "")
    if not is_free_model(model):
        raise ValueError(f"refused: model '{model}' is not free")
    
    changes = []
    
    # Create profile if missing
    profile_dir = _hermes_profile_dir(profile)
    if not profile_dir.exists():
        description = role_data.get("does", f"Team agent: {role}")
        _run_hermes_profile_create(profile, "team-worker", description, runner)
        changes.append(f"created profile {profile}")
    
    # Write SOUL.md
    instructions = _get_role_instructions(root, role)
    if instructions is not None:
        _write_soul_md(profile, instructions)
        changes.append("wrote SOUL.md")
    
    # Write skills
    skills = _get_role_skills(root, role)
    _write_skills(profile, skills)
    changes.append(f"seeded upstream library and {len(skills)} role skills; kept existing skills")
    
    # Write model.default
    if model:
        _write_model_default(profile, model)
        changes.append(f"set model.default={model}")
    
    # Preserve native learning while specializing execution capabilities.
    role_tools = role_data.get("tools", [])
    _write_role_profile_config(profile, role_tools, role_data.get("budget", {}).get("max_turns", 40))
    changes.append("enabled native Hermes memory, skills, session search and compression")
    
    return "; ".join(changes) if changes else "no changes"


def sync_all(root: Path) -> list[str]:
    """Sync all roles with status 'active' or 'planned'."""
    from team.roles import load_roles
    
    roles = load_roles(root)
    results = []
    for role_data in roles:
        name = role_data["name"]
        status = role_data.get("status", "")
        if status in ("active", "planned"):
            try:
                result = sync(root, name)
                results.append(f"{name}: {result}")
            except Exception as e:
                results.append(f"{name}: ERROR - {e}")
    return results