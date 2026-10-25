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
    home = Path.home()
    return home / ".hermes" / "profiles" / profile


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
    path.write_text(text)


def _write_skills(profile: str, skills: list[dict[str, Any]]) -> None:
    """Write skills to a Hermes profile, replacing the entire skills/ directory."""
    skills_dir = _hermes_profile_dir(profile) / "skills"
    # Remove existing skills directory
    if skills_dir.exists():
        import shutil
        shutil.rmtree(skills_dir)
    skills_dir.mkdir(parents=True, exist_ok=True)
    
    for skill in skills:
        skill_name = skill["name"]
        skill_text = skill["text"]
        skill_path = skills_dir / skill_name / "SKILL.md"
        skill_path.parent.mkdir(parents=True, exist_ok=True)
        skill_path.write_text(skill_text)


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
    if skills:
        _write_skills(profile, skills)
        changes.append(f"wrote {len(skills)} skills")
    
    # Write model.default
    if model:
        _write_model_default(profile, model)
        changes.append(f"set model.default={model}")
    
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