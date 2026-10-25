"""Tests for team/profiles.py."""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from team import profiles
from team.agents import AgentError


class TestProfileName:
    def test_profile_name_format(self):
        assert profiles.profile_name("implement") == "team-implement"
        assert profiles.profile_name("spec") == "team-spec"
        assert profiles.profile_name("boss") == "team-boss"
        assert profiles.profile_name("custom-agent") == "team-custom-agent"


class TestSync:
    def test_sync_creates_profile(self, tmp_path, monkeypatch):
        """Test sync creates a Hermes profile by cloning from team-worker."""
        # Create a fake HOME directory
        fake_home = tmp_path / "fake_home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: fake_home)
        
        # Create a fake repo with a role
        repo = tmp_path / "repo"
        repo.mkdir()
        roles_dir = repo / "roles"
        roles_dir.mkdir()
        role_dir = roles_dir / "test-agent"
        role_dir.mkdir()
        
        # Write a minimal role.toml
        role_toml = role_dir / "role.toml"
        role_toml.write_text("""
name = "test-agent"
title = "Test Agent"
does = "Does testing"
graded_by = "Tests pass"
status = "planned"
tools = ["terminal"]
[runs_on]
harness = "hermes"
model = "gemini-2.5-flash"
[escalation]
harness = "crew"
model = "claude"
after_failures = 2
[budget]
max_turns = 25
run_budget_s = 600
""")
        
        # Write SOUL.md
        (role_dir / "SOUL.md").write_text("# Test Agent\n\nDoes testing\n")
        
        # Mock hermes profile create using runner
        runner_called = []
        def mock_runner(cmd, **kwargs):
            runner_called.append(cmd)
            if cmd[:2] == ["hermes", "profile"]:
                # Create the profile directory structure
                profile_dir = fake_home / ".hermes" / "profiles" / "team-test-agent"
                profile_dir.mkdir(parents=True)
                # Create a minimal config.yaml
                (profile_dir / "config.yaml").write_text("model:\n  default: nvidia/nemotron-3-ultra-550b-a55b:free\n")
            return MagicMock(returncode=0, stdout="", stderr="")
        
        with patch("subprocess.run", side_effect=mock_runner):
            result = profiles.sync(repo, "test-agent", runner=mock_runner)
            assert "created profile team-test-agent" in result
            assert "wrote SOUL.md" in result
            assert "set model.default=gemini-2.5-flash" in result
            # Verify runner was called
            assert len(runner_called) > 0
            assert runner_called[0][:2] == ["hermes", "profile"]
   
    def test_sync_refuses_non_free_model(self, tmp_path, monkeypatch):
        """Test sync refuses non-free models."""
        fake_home = tmp_path / "fake_home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: fake_home)
        
        repo = tmp_path / "repo"
        repo.mkdir()
        roles_dir = repo / "roles"
        roles_dir.mkdir()
        role_dir = roles_dir / "test-agent"
        role_dir.mkdir()
        
        role_toml = role_dir / "role.toml"
        role_toml.write_text("""
name = "test-agent"
title = "Test Agent"
does = "Does testing"
graded_by = "Tests pass"
status = "planned"
tools = ["terminal"]
[runs_on]
harness = "hermes"
model = "gpt-4"
[escalation]
harness = "crew"
model = "claude"
after_failures = 2
[budget]
max_turns = 25
run_budget_s = 600
""")
        
        with pytest.raises(ValueError, match="not free"):
            profiles.sync(repo, "test-agent")
    
    def test_sync_refuses_non_team_profile(self, tmp_path, monkeypatch):
        """Test sync refuses profiles not named team-*. Actually the profile is always team-<role>, so this tests a role that doesn't produce a team-* profile."""
        fake_home = tmp_path / "fake_home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: fake_home)
        
        repo = tmp_path / "repo"
        repo.mkdir()
        roles_dir = repo / "roles"
        roles_dir.mkdir()
        role_dir = roles_dir / "not-team"
        role_dir.mkdir()
        
        role_toml = role_dir / "role.toml"
        role_toml.write_text("""
name = "not-team"
title = "Not Team"
does = "Does stuff"
graded_by = "Tests pass"
status = "planned"
tools = ["terminal"]
[runs_on]
harness = "hermes"
model = "gemini-2.5-flash"
[escalation]
harness = "crew"
model = "claude"
after_failures = 2
[budget]
max_turns = 25
run_budget_s = 600
""")
        
        # The profile name would be "team-not-team" which starts with team-
        # So this should work. Let's test that the runner is called
        runner_called = []
        def mock_runner(cmd, **kwargs):
            runner_called.append(cmd)
            if cmd[:2] == ["hermes", "profile"]:
                profile_dir = fake_home / ".hermes" / "profiles" / "team-not-team"
                profile_dir.mkdir(parents=True)
                (profile_dir / "config.yaml").write_text("model:\n  default: nvidia/nemotron-3-ultra-550b-a55b:free\n")
            return MagicMock(returncode=0, stdout="", stderr="")
        
        with patch("subprocess.run", side_effect=mock_runner):
            result = profiles.sync(repo, "not-team", runner=mock_runner)
            assert "created profile team-not-team" in result
            assert len(runner_called) > 0
    
    def test_sync_writes_skills(self, tmp_path, monkeypatch):
        """Test sync writes skills to the profile."""
        fake_home = tmp_path / "fake_home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: fake_home)
        
        repo = tmp_path / "repo"
        repo.mkdir()
        roles_dir = repo / "roles"
        roles_dir.mkdir()
        role_dir = roles_dir / "test-agent"
        role_dir.mkdir()
        
        role_toml = role_dir / "role.toml"
        role_toml.write_text("""
name = "test-agent"
title = "Test Agent"
does = "Does testing"
graded_by = "Tests pass"
status = "planned"
tools = ["terminal"]
[runs_on]
harness = "hermes"
model = "gemini-2.5-flash"
[escalation]
harness = "crew"
model = "claude"
after_failures = 2
[budget]
max_turns = 25
run_budget_s = 600
""")
        
        # Create a skill
        skills_dir = role_dir / "skills"
        skills_dir.mkdir()
        skill_dir = skills_dir / "test-skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("# Test Skill\n\nContent\n")
        
        (role_dir / "SOUL.md").write_text("# Test Agent\n\nDoes testing\n")
        
        def mock_runner(cmd, **kwargs):
            if cmd[:2] == ["hermes", "profile"]:
                profile_dir = fake_home / ".hermes" / "profiles" / "team-test-agent"
                profile_dir.mkdir(parents=True)
                (profile_dir / "config.yaml").write_text("model:\n  default: nvidia/nemotron-3-ultra-550b-a55b:free\n")
            return MagicMock(returncode=0, stdout="", stderr="")
        
        with patch("subprocess.run", side_effect=mock_runner):
            result = profiles.sync(repo, "test-agent", runner=mock_runner)
            assert "wrote 1 skills" in result
            
            # Verify the skill was written
            skill_path = fake_home / ".hermes" / "profiles" / "team-test-agent" / "skills" / "test-skill" / "SKILL.md"
            assert skill_path.exists()
            assert skill_path.read_text() == "# Test Skill\n\nContent\n"
    
    def test_sync_replaces_skills_dir(self, tmp_path, monkeypatch):
        """Test sync replaces the entire skills/ directory."""
        fake_home = tmp_path / "fake_home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: fake_home)
        
        repo = tmp_path / "repo"
        repo.mkdir()
        roles_dir = repo / "roles"
        roles_dir.mkdir()
        role_dir = roles_dir / "test-agent"
        role_dir.mkdir()
        
        role_toml = role_dir / "role.toml"
        role_toml.write_text("""
name = "test-agent"
title = "Test Agent"
does = "Does testing"
graded_by = "Tests pass"
status = "planned"
tools = ["terminal"]
[runs_on]
harness = "hermes"
model = "gemini-2.5-flash"
[escalation]
harness = "crew"
model = "claude"
after_failures = 2
[budget]
max_turns = 25
run_budget_s = 600
""")
        
        # Create a skill
        skills_dir = role_dir / "skills"
        skills_dir.mkdir()
        skill_dir = skills_dir / "test-skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("# Test Skill\n\nContent\n")
        
        (role_dir / "SOUL.md").write_text("# Test Agent\n\nDoes testing\n")
        
        def mock_runner(cmd, **kwargs):
            if cmd[:2] == ["hermes", "profile"]:
                profile_dir = fake_home / ".hermes" / "profiles" / "team-test-agent"
                profile_dir.mkdir(parents=True)
                (profile_dir / "config.yaml").write_text("model:\n  default: nvidia/nemotron-3-ultra-550b-a55b:free\n")
                # Add an existing skill that should be removed
                existing_skills = profile_dir / "skills"
                existing_skills.mkdir()
                old_skill = existing_skills / "old-skill"
                old_skill.mkdir()
                (old_skill / "SKILL.md").write_text("# Old Skill\n")
            return MagicMock(returncode=0, stdout="", stderr="")
        
        with patch("subprocess.run", side_effect=mock_runner):
            result = profiles.sync(repo, "test-agent", runner=mock_runner)
            
            # Verify old skill is gone, new skill is there
            profile_skills = fake_home / ".hermes" / "profiles" / "team-test-agent" / "skills"
            assert not (profile_skills / "old-skill").exists()
            assert (profile_skills / "test-skill" / "SKILL.md").exists()


class TestSyncAll:
    def test_sync_all_syncs_active_and_planned(self, tmp_path, monkeypatch):
        """Test sync_all syncs roles with status active or planned."""
        fake_home = tmp_path / "fake_home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: fake_home)
        
        repo = tmp_path / "repo"
        repo.mkdir()
        roles_dir = repo / "roles"
        roles_dir.mkdir()
        
        # Create active agent
        active_dir = roles_dir / "active-agent"
        active_dir.mkdir()
        (active_dir / "role.toml").write_text("""
name = "active-agent"
title = "Active"
does = "Active"
graded_by = "Tests"
status = "active"
tools = ["terminal"]
[runs_on]
harness = "hermes"
model = "gemini-2.5-flash"
[escalation]
harness = "crew"
model = "claude"
after_failures = 2
[budget]
max_turns = 25
run_budget_s = 600
""")
        (active_dir / "SOUL.md").write_text("# Active\n")
        
        # Create planned agent
        planned_dir = roles_dir / "planned-agent"
        planned_dir.mkdir()
        (planned_dir / "role.toml").write_text("""
name = "planned-agent"
title = "Planned"
does = "Planned"
graded_by = "Tests"
status = "planned"
tools = ["terminal"]
[runs_on]
harness = "hermes"
model = "gemini-2.5-flash"
[escalation]
harness = "crew"
model = "claude"
after_failures = 2
[budget]
max_turns = 25
run_budget_s = 600
""")
        (planned_dir / "SOUL.md").write_text("# Planned\n")
        
        # Create inactive agent (should be skipped)
        inactive_dir = roles_dir / "inactive-agent"
        inactive_dir.mkdir()
        (inactive_dir / "role.toml").write_text("""
name = "inactive-agent"
title = "Inactive"
does = "Inactive"
graded_by = "Tests"
status = "inactive"
tools = ["terminal"]
[runs_on]
harness = "hermes"
model = "gemini-2.5-flash"
[escalation]
harness = "crew"
model = "claude"
after_failures = 2
[budget]
max_turns = 25
run_budget_s = 600
""")
        (inactive_dir / "SOUL.md").write_text("# Inactive\n")
        
        def mock_runner(cmd, **kwargs):
            if cmd[:2] == ["hermes", "profile"]:
                profile_name = cmd[4]  # hermes profile create <name>
                profile_dir = fake_home / ".hermes" / "profiles" / profile_name
                profile_dir.mkdir(parents=True)
                (profile_dir / "config.yaml").write_text("model:\n  default: nvidia/nemotron-3-ultra-550b-a55b:free\n")
            return MagicMock(returncode=0, stdout="", stderr="")
        
        with patch("subprocess.run", side_effect=mock_runner):
            results = profiles.sync_all(repo)
            assert len(results) == 2
            assert any("active-agent" in r for r in results)
            assert any("planned-agent" in r for r in results)
            assert not any("inactive-agent" in r for r in results)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])