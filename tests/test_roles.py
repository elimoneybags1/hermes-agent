"""Tests for team/roles.py."""

from __future__ import annotations

import os
import tempfile

import pytest

from team import roles


@pytest.fixture
def temp_roles_dir(tmp_path):
    """Create a temporary roles directory structure."""
    roles_dir = tmp_path / "roles"
    roles_dir.mkdir()

    # Create boss role
    boss_dir = roles_dir / "boss"
    boss_dir.mkdir()
    (boss_dir / "role.toml").write_text(
        'name = "boss"\n'
        'title = "Foreman"\n'
        'does = "Talks to Eli"\n'
        'graded_by = "Eli"\n'
        'status = "planned"\n'
        '\n'
        '[runs_on]\n'
        'harness = "claude-code"\n'
        'model = "claude"\n'
    )

    # Create implement role
    impl_dir = roles_dir / "implement"
    impl_dir.mkdir()
    (impl_dir / "role.toml").write_text(
        'name = "implement"\n'
        'title = "Implement"\n'
        'does = "Writes the code"\n'
        'graded_by = "pytest"\n'
        'status = "active"\n'
        '\n'
        '[runs_on]\n'
        'harness = "hermes"\n'
        'model = "nvidia/nemotron-3-ultra-550b-a55b:free"\n'
        '\n'
        '[escalation]\n'
        'harness = "crew"\n'
        'model = "claude"\n'
        'after_failures = 2\n'
        '\n'
        '[budget]\n'
        'max_turns = 40\n'
        'run_budget_s = 900\n'
    )

    # Create a role with minimal fields
    minimal_dir = roles_dir / "minimal"
    minimal_dir.mkdir()
    (minimal_dir / "role.toml").write_text(
        'name = "minimal"\n'
        'title = "Minimal"\n'
        'does = "Does little"\n'
        'graded_by = "none"\n'
        'status = "planned"\n'
        '\n'
        '[runs_on]\n'
        'harness = "hermes"\n'
        'model = "test"\n'
    )

    return tmp_path


def test_load_roles_defaults_to_repo_root(temp_roles_dir, monkeypatch):
    """load_roles() with no root finds roles from this file's parent.parent."""
    # Point to our temp dir by patching __file__
    import team.roles as roles_mod
    monkeypatch.setattr(roles_mod, "__file__", str(temp_roles_dir / "team" / "roles.py"))
    (temp_roles_dir / "team").mkdir()

    role_list = roles.load_roles()
    assert len(role_list) == 3
    names = [r["name"] for r in role_list]
    assert names == ["boss", "implement", "minimal"]


def test_load_roles_explicit_root(temp_roles_dir):
    """load_roles(root=...) uses the given root."""
    role_list = roles.load_roles(root=temp_roles_dir)
    assert len(role_list) == 3
    names = [r["name"] for r in role_list]
    assert names == ["boss", "implement", "minimal"]


def test_load_roles_sorted_by_name(temp_roles_dir):
    """Roles are returned sorted by directory name."""
    role_list = roles.load_roles(root=temp_roles_dir)
    names = [r["name"] for r in role_list]
    assert names == sorted(names)


def test_load_roles_skips_non_dirs(temp_roles_dir):
    """Non-directories in roles/ are ignored."""
    (temp_roles_dir / "roles" / "not_a_dir.toml").write_text("junk")
    role_list = roles.load_roles(root=temp_roles_dir)
    assert len(role_list) == 3


def test_load_roles_skips_missing_role_toml(temp_roles_dir):
    """Directories without role.toml are ignored."""
    (temp_roles_dir / "roles" / "empty").mkdir()
    role_list = roles.load_roles(root=temp_roles_dir)
    assert len(role_list) == 3


def test_load_roles_includes_all_fields(temp_roles_dir):
    """All fields from role.toml are preserved, plus name from dir."""
    role_list = roles.load_roles(root=temp_roles_dir)
    impl = next(r for r in role_list if r["name"] == "implement")
    assert impl["title"] == "Implement"
    assert impl["does"] == "Writes the code"
    assert impl["graded_by"] == "pytest"
    assert impl["status"] == "active"
    assert impl["runs_on"] == {"harness": "hermes", "model": "nvidia/nemotron-3-ultra-550b-a55b:free"}
    assert impl["escalation"] == {"harness": "crew", "model": "claude", "after_failures": 2}
    assert impl["budget"] == {"max_turns": 40, "run_budget_s": 900}


def test_load_roles_optional_fields_absent_when_missing(temp_roles_dir):
    """escalation and budget are absent (not empty dict) when not in TOML."""
    role_list = roles.load_roles(root=temp_roles_dir)
    boss = next(r for r in role_list if r["name"] == "boss")
    assert "escalation" not in boss
    assert "budget" not in boss


def test_load_roles_empty_when_no_roles_dir(tmp_path):
    """Returns empty list when roles/ doesn't exist."""
    empty = tmp_path / "empty"
    empty.mkdir()
    role_list = roles.load_roles(root=empty)
    assert role_list == []


def test_load_roles_no_hardcoded_paths(temp_roles_dir):
    """No hardcoded user directories in the implementation."""
    import inspect
    source = inspect.getsource(roles.load_roles)
    assert "home" not in source.lower()
    assert "/users/" not in source.lower()
    assert "~" not in source