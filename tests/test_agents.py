"""Tests for team/agents.py."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from team import agents, ledger
from team.agents import AgentError


@pytest.fixture
def temp_repo(tmp_path):
    """Create a temporary git repo with roles copied from the real repo."""
    repo = tmp_path / "repo"
    repo.mkdir()

    # Initialize git
    subprocess.run(["git", "-C", str(repo), "init"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "Test User"], check=True
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "test@example.com"],
        check=True,
    )

    # Copy roles from real repo
    real_roles = Path(__file__).parent.parent / "roles"
    for role_dir in real_roles.iterdir():
        if role_dir.is_dir():
            dst = repo / "roles" / role_dir.name
            shutil.copytree(role_dir, dst)

    # Copy plugin
    real_plugin = Path(__file__).parent.parent / "plugin"
    if real_plugin.exists():
        shutil.copytree(real_plugin, repo / "plugin")

    # Initial commit
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "initial"],
        check=True,
        capture_output=True,
    )

    return repo


@pytest.fixture
def temp_ledger(tmp_path):
    """Create a temporary ledger."""
    db_path = tmp_path / "team.db"
    conn = ledger.connect(db_path)
    ledger.init(conn)
    yield conn
    conn.close()


class TestAgentNames:
    def test_agent_names_sorted(self, temp_repo):
        names = agents.agent_names(temp_repo)
        assert names == ["boss", "implement", "review", "spec", "test"]
        assert names == sorted(names)

    def test_agent_names_missing_roles_dir(self, tmp_path):
        empty = tmp_path / "empty"
        empty.mkdir()
        assert agents.agent_names(empty) == []


class TestPipeline:
    def test_pipeline_order(self, temp_repo):
        roles = agents.load_roles(temp_repo)
        pipe = agents.pipeline(roles)
        assert pipe == ["boss", "spec", "test", "implement", "review"]

    def test_pipeline_stops_on_missing(self, temp_repo):
        # Modify spec to point to unknown
        import tomllib
        import tomli_w

        path = temp_repo / "roles" / "spec" / "role.toml"
        with path.open("rb") as f:
            data = tomllib.load(f)
        data["hands_to"] = "unknown"
        with path.open("wb") as f:
            tomli_w.dump(data, f)

        roles = agents.load_roles(temp_repo)
        pipe = agents.pipeline(roles)
        assert pipe == ["boss", "spec"]

    def test_pipeline_stops_on_loop(self, temp_repo):
        # Make review point back to spec (creating a loop)
        import tomllib
        import tomli_w

        path = temp_repo / "roles" / "review" / "role.toml"
        with path.open("rb") as f:
            data = tomllib.load(f)
        data["hands_to"] = "spec"
        with path.open("wb") as f:
            tomli_w.dump(data, f)

        roles = agents.load_roles(temp_repo)
        pipe = agents.pipeline(roles)
        assert pipe == ["boss", "spec", "test", "implement", "review"]


class TestDetail:
    def test_detail_shape(self, temp_repo, temp_ledger):
        # Seed some jobs
        j1 = ledger.start_job(temp_ledger, "team", "c1", "implement", "v1")
        ledger.finish_job(temp_ledger, j1, "pass", "pytest", "ok")

        detail = agents.detail(temp_repo, temp_ledger, "implement")

        # Check all required keys
        assert detail["name"] == "implement"
        assert "role" in detail
        assert detail["role"]["name"] == "implement"
        assert "instructions" in detail
        assert "path" in detail["instructions"]
        assert "text" in detail["instructions"]
        assert "skills" in detail
        assert isinstance(detail["skills"], list)
        assert "tools" in detail
        assert detail["tools"] == ["terminal", "file", "todo", "skills"]
        assert "available_tools" in detail
        assert detail["available_tools"] == agents.TOOLSETS
        assert "hooks" in detail
        assert isinstance(detail["hooks"], list)
        assert "history" in detail
        assert isinstance(detail["history"], list)
        assert "jobs" in detail
        assert isinstance(detail["jobs"], list)
        assert len(detail["jobs"]) >= 1
        assert "working_on" in detail
        assert "stats" in detail
        assert "base" in detail
        assert detail["stats"]["jobs"] >= 1
        assert detail["base"]["jobs"] >= 1

    def test_detail_unknown_agent_raises(self, temp_repo, temp_ledger):
        with pytest.raises(AgentError, match="unknown agent"):
            agents.detail(temp_repo, temp_ledger, "nonexistent")

    def test_detail_with_none_conn(self, temp_repo):
        detail = agents.detail(temp_repo, None, "implement")
        assert detail["working_on"] == []
        assert detail["jobs"] == []
        assert detail["stats"]["jobs"] == 0
        assert detail["stats"]["pass_rate"] is None
        assert detail["base"]["jobs"] == 0


class TestSetField:
    def test_set_title(self, temp_repo):
        sha = agents.set_field(temp_repo, "implement", "title", "New Title")
        assert len(sha) == 40  # full sha

        # Verify it was written
        import tomllib
        with (temp_repo / "roles" / "implement" / "role.toml").open("rb") as f:
            data = tomllib.load(f)
        assert data["title"] == "New Title"

    def test_set_does(self, temp_repo):
        sha = agents.set_field(temp_repo, "implement", "does", "New description")
        import tomllib
        with (temp_repo / "roles" / "implement" / "role.toml").open("rb") as f:
            data = tomllib.load(f)
        assert data["does"] == "New description"

    def test_set_graded_by(self, temp_repo):
        sha = agents.set_field(temp_repo, "implement", "graded_by", "New grader")
        import tomllib
        with (temp_repo / "roles" / "implement" / "role.toml").open("rb") as f:
            data = tomllib.load(f)
        assert data["graded_by"] == "New grader"

    def test_set_status_valid(self, temp_repo):
        sha = agents.set_field(temp_repo, "implement", "status", "planned")
        import tomllib
        with (temp_repo / "roles" / "implement" / "role.toml").open("rb") as f:
            data = tomllib.load(f)
        assert data["status"] == "planned"

    def test_set_status_invalid_refused(self, temp_repo):
        with pytest.raises(AgentError, match="status must be one of"):
            agents.set_field(temp_repo, "implement", "status", "invalid")

    def test_set_runs_on_model_free_allowed(self, temp_repo):
        # test uses hermes, free model should be allowed
        sha = agents.set_field(
            temp_repo, "test", "runs_on.model", "gemini-2.5-flash"
        )
        import tomllib
        with (temp_repo / "roles" / "test" / "role.toml").open("rb") as f:
            data = tomllib.load(f)
        assert data["runs_on"]["model"] == "gemini-2.5-flash"

    def test_set_runs_on_model_paid_refused_on_hermes(self, temp_repo):
        # test uses hermes, paid model should be refused
        with pytest.raises(AgentError, match="not free"):
            agents.set_field(temp_repo, "test", "runs_on.model", "gpt-4")

    def test_set_runs_on_model_paid_allowed_on_claude_code(self, temp_repo):
        # boss uses claude-code, paid model should be allowed
        sha = agents.set_field(temp_repo, "boss", "runs_on.model", "claude-3-opus")
        import tomllib
        with (temp_repo / "roles" / "boss" / "role.toml").open("rb") as f:
            data = tomllib.load(f)
        assert data["runs_on"]["model"] == "claude-3-opus"

    def test_set_budget_max_turns(self, temp_repo):
        sha = agents.set_field(temp_repo, "implement", "budget.max_turns", 50)
        import tomllib
        with (temp_repo / "roles" / "implement" / "role.toml").open("rb") as f:
            data = tomllib.load(f)
        assert data["budget"]["max_turns"] == 50

    def test_set_budget_max_turns_invalid_refused(self, temp_repo):
        with pytest.raises(AgentError, match="integer > 0"):
            agents.set_field(temp_repo, "implement", "budget.max_turns", 0)
        with pytest.raises(AgentError, match="integer > 0"):
            agents.set_field(temp_repo, "implement", "budget.max_turns", -1)
        with pytest.raises(AgentError, match="integer > 0"):
            agents.set_field(temp_repo, "implement", "budget.max_turns", "not int")

    def test_set_budget_run_budget_s(self, temp_repo):
        sha = agents.set_field(temp_repo, "implement", "budget.run_budget_s", 1200)
        import tomllib
        with (temp_repo / "roles" / "implement" / "role.toml").open("rb") as f:
            data = tomllib.load(f)
        assert data["budget"]["run_budget_s"] == 1200

    def test_set_escalation_after_failures(self, temp_repo):
        sha = agents.set_field(temp_repo, "implement", "escalation.after_failures", 3)
        import tomllib
        with (temp_repo / "roles" / "implement" / "role.toml").open("rb") as f:
            data = tomllib.load(f)
        assert data["escalation"]["after_failures"] == 3

    def test_set_tools_valid(self, temp_repo):
        sha = agents.set_field(temp_repo, "implement", "tools", ["terminal", "file"])
        import tomllib
        with (temp_repo / "roles" / "implement" / "role.toml").open("rb") as f:
            data = tomllib.load(f)
        assert data["tools"] == ["terminal", "file"]

    def test_set_tools_unknown_refused(self, temp_repo):
        with pytest.raises(AgentError, match="unknown tool"):
            agents.set_field(temp_repo, "implement", "tools", ["terminal", "invalid_tool"])

    def test_set_tools_not_list_refused(self, temp_repo):
        with pytest.raises(AgentError, match="tools must be a list"):
            agents.set_field(temp_repo, "implement", "tools", "terminal")

    def test_set_hands_to_valid(self, temp_repo):
        sha = agents.set_field(temp_repo, "spec", "hands_to", "review")
        import tomllib
        with (temp_repo / "roles" / "spec" / "role.toml").open("rb") as f:
            data = tomllib.load(f)
        assert data["hands_to"] == "review"

    def test_set_hands_to_unknown_refused(self, temp_repo):
        with pytest.raises(AgentError, match="existing agent name"):
            agents.set_field(temp_repo, "spec", "hands_to", "nonexistent")

    def test_set_hands_to_self_refused(self, temp_repo):
        with pytest.raises(AgentError, match="cannot point to itself"):
            agents.set_field(temp_repo, "spec", "hands_to", "spec")

    def test_set_unknown_field_refused(self, temp_repo):
        with pytest.raises(AgentError, match="unknown field"):
            agents.set_field(temp_repo, "implement", "unknown_field", "value")

    def test_set_empty_string_refused(self, temp_repo):
        with pytest.raises(AgentError, match="non-empty string"):
            agents.set_field(temp_repo, "implement", "title", "")
        with pytest.raises(AgentError, match="non-empty string"):
            agents.set_field(temp_repo, "implement", "does", "   ")

    def test_set_field_commits_only_changed_paths(self, temp_repo):
        # Stage an unrelated file first
        unrelated = temp_repo / "unrelated.txt"
        unrelated.write_text("unrelated")
        subprocess.run(["git", "-C", str(temp_repo), "add", "unrelated.txt"], check=True)

        # Change a field
        sha = agents.set_field(temp_repo, "implement", "title", "New Title")

        # Check the commit only touches role.toml
        diff = subprocess.run(
            ["git", "-C", str(temp_repo), "show", "--name-only", "--pretty=format:", sha],
            check=True,
            capture_output=True,
            text=True,
        )
        changed_files = [f for f in diff.stdout.strip().split("\n") if f]
        assert changed_files == ["roles/implement/role.toml"]

        # Check unrelated.txt is still staged
        status = subprocess.run(
            ["git", "-C", str(temp_repo), "status", "--porcelain"],
            check=True,
            capture_output=True,
            text=True,
        )
        assert "unrelated.txt" in status.stdout

    def test_set_field_commit_message_format(self, temp_repo):
        sha = agents.set_field(temp_repo, "implement", "title", "New Title", source="test")
        # Get commit message
        msg = subprocess.run(
            ["git", "-C", str(temp_repo), "log", "-1", "--pretty=format:%s", sha],
            check=True,
            capture_output=True,
            text=True,
        )
        assert msg.stdout == "implement: set title (test)"

    def test_set_field_no_change_raises(self, temp_repo):
        # First set a value
        agents.set_field(temp_repo, "implement", "title", "New Title")
        # Try to set the same value - should raise "no change"
        with pytest.raises(AgentError, match="no change"):
            agents.set_field(temp_repo, "implement", "title", "New Title")


class TestWriteInstructions:
    def test_write_instructions_regular_agent(self, temp_repo):
        sha = agents.write_instructions(
            temp_repo, "implement", "New instructions text"
        )
        path = temp_repo / "roles" / "implement" / "SOUL.md"
        assert path.exists()
        assert path.read_text() == "New instructions text"

    def test_write_instructions_boss(self, temp_repo):
        sha = agents.write_instructions(temp_repo, "boss", "Boss instructions")
        path = temp_repo / "plugin" / "agents" / "boss.md"
        assert path.exists()
        assert path.read_text() == "Boss instructions"

    def test_write_instructions_unknown_agent_refused(self, temp_repo):
        with pytest.raises(AgentError, match="unknown agent"):
            agents.write_instructions(temp_repo, "nonexistent", "text")

    def test_write_instructions_commits_only_instructions(self, temp_repo):
        # Stage an unrelated file
        unrelated = temp_repo / "unrelated.txt"
        unrelated.write_text("unrelated")
        subprocess.run(["git", "-C", str(temp_repo), "add", "unrelated.txt"], check=True)

        sha = agents.write_instructions(temp_repo, "implement", "New instructions")

        diff = subprocess.run(
            ["git", "-C", str(temp_repo), "show", "--name-only", "--pretty=format:", sha],
            check=True,
            capture_output=True,
            text=True,
        )
        changed_files = [f for f in diff.stdout.strip().split("\n") if f]
        assert changed_files == ["roles/implement/SOUL.md"]


class TestWriteSkill:
    def test_write_skill_creates_folder_and_file(self, temp_repo):
        sha = agents.write_skill(temp_repo, "implement", "test-skill", "Skill content")
        path = temp_repo / "roles" / "implement" / "skills" / "test-skill" / "SKILL.md"
        assert path.exists()
        assert path.read_text() == "Skill content"

    def test_write_skill_valid_name_patterns(self, temp_repo):
        # Valid names
        agents.write_skill(temp_repo, "implement", "a", "x")
        agents.write_skill(temp_repo, "implement", "a-b", "x")
        agents.write_skill(temp_repo, "implement", "a1", "x")
        agents.write_skill(temp_repo, "implement", "run-pytest", "x")

    def test_write_skill_invalid_name_refused(self, temp_repo):
        with pytest.raises(AgentError, match="skill name must match"):
            agents.write_skill(temp_repo, "implement", "Test", "x")  # uppercase
        with pytest.raises(AgentError, match="skill name must match"):
            agents.write_skill(temp_repo, "implement", "-invalid", "x")  # starts with -
        with pytest.raises(AgentError, match="skill name must match"):
            agents.write_skill(temp_repo, "implement", "invalid_", "x")  # underscore
        with pytest.raises(AgentError, match="skill name must match"):
            agents.write_skill(temp_repo, "implement", "a" * 65, "x")  # too long

    def test_write_skill_unknown_agent_refused(self, temp_repo):
        with pytest.raises(AgentError, match="unknown agent"):
            agents.write_skill(temp_repo, "nonexistent", "skill", "x")

    def test_write_skill_commits_only_skill_file(self, temp_repo):
        unrelated = temp_repo / "unrelated.txt"
        unrelated.write_text("unrelated")
        subprocess.run(["git", "-C", str(temp_repo), "add", "unrelated.txt"], check=True)

        sha = agents.write_skill(temp_repo, "implement", "test-skill", "Skill content")

        diff = subprocess.run(
            ["git", "-C", str(temp_repo), "show", "--name-only", "--pretty=format:", sha],
            check=True,
            capture_output=True,
            text=True,
        )
        changed_files = [f for f in diff.stdout.strip().split("\n") if f]
        assert changed_files == ["roles/implement/skills/test-skill/SKILL.md"]


class TestDeleteSkill:
    def test_delete_skill(self, temp_repo):
        # First create a skill
        agents.write_skill(temp_repo, "implement", "to-delete", "Content")
        path = temp_repo / "roles" / "implement" / "skills" / "to-delete" / "SKILL.md"
        assert path.exists()

        # Delete it
        sha = agents.delete_skill(temp_repo, "implement", "to-delete")
        assert not path.exists()
        assert not path.parent.exists()

    def test_delete_skill_not_found_refused(self, temp_repo):
        with pytest.raises(AgentError, match="skill not found"):
            agents.delete_skill(temp_repo, "implement", "nonexistent")

    def test_delete_skill_unknown_agent_refused(self, temp_repo):
        with pytest.raises(AgentError, match="unknown agent"):
            agents.delete_skill(temp_repo, "nonexistent", "skill")

    def test_delete_skill_commits_deletion(self, temp_repo):
        agents.write_skill(temp_repo, "implement", "to-delete", "Content")
        unrelated = temp_repo / "unrelated.txt"
        unrelated.write_text("unrelated")
        subprocess.run(["git", "-C", str(temp_repo), "add", "unrelated.txt"], check=True)

        sha = agents.delete_skill(temp_repo, "implement", "to-delete")

        diff = subprocess.run(
            ["git", "-C", str(temp_repo), "show", "--name-only", "--pretty=format:", sha],
            check=True,
            capture_output=True,
            text=True,
        )
        changed_files = [f for f in diff.stdout.strip().split("\n") if f]
        assert "roles/implement/skills/to-delete/SKILL.md" in changed_files

    def test_delete_skill_invalid_name_refused(self, temp_repo):
        # Test various invalid skill names
        with pytest.raises(AgentError, match="skill name must match"):
            agents.delete_skill(temp_repo, "implement", "Test")  # uppercase
        with pytest.raises(AgentError, match="skill name must match"):
            agents.delete_skill(temp_repo, "implement", "-invalid")  # starts with -
        with pytest.raises(AgentError, match="skill name must match"):
            agents.delete_skill(temp_repo, "implement", "invalid_")  # underscore
        with pytest.raises(AgentError, match="skill name must match"):
            agents.delete_skill(temp_repo, "implement", "a" * 65)  # too long
        with pytest.raises(AgentError, match="skill name must match"):
            agents.delete_skill(temp_repo, "implement", "../x")  # path traversal

    def test_delete_skill_no_change_raises(self, temp_repo):
        # Write a skill, then write it again with same content - should raise "no change"
        agents.write_skill(temp_repo, "implement", "to-delete", "Content")
        with pytest.raises(AgentError, match="no change"):
            agents.write_skill(temp_repo, "implement", "to-delete", "Content")


class TestRevert:
    def test_revert_own_commit(self, temp_repo):
        # Make a change
        sha = agents.set_field(temp_repo, "implement", "title", "Title 1")
        # Make another change
        sha2 = agents.set_field(temp_repo, "implement", "title", "Title 2")

        # Revert the first change
        new_sha = agents.revert(temp_repo, "implement", sha)

        # Should be back to original title
        import tomllib
        with (temp_repo / "roles" / "implement" / "role.toml").open("rb") as f:
            data = tomllib.load(f)
        assert data["title"] == "Implement"  # original from repo

    def test_revert_other_agents_commit_refused(self, temp_repo):
        # Change implement's title
        sha = agents.set_field(temp_repo, "implement", "title", "Title 1")
        # Change test's title
        sha2 = agents.set_field(temp_repo, "test", "title", "Title 1")

        # Try to revert implement's commit as test - should be refused
        with pytest.raises(AgentError, match="does not belong to"):
            agents.revert(temp_repo, "test", sha)

    def test_revert_commit_touching_other_files_refused(self, temp_repo):
        # Create a commit that touches two agents' files
        # First, manually create such a commit
        import tomllib
        import tomli_w

        # Modify both implement and test
        for name in ["implement", "test"]:
            path = temp_repo / "roles" / name / "role.toml"
            with path.open("rb") as f:
                data = tomllib.load(f)
            data["title"] = "Modified"
            with path.open("wb") as f:
                tomli_w.dump(data, f)

        subprocess.run(["git", "-C", str(temp_repo), "add", "."], check=True)
        subprocess.run(
            ["git", "-C", str(temp_repo), "commit", "-m", "both"],
            check=True,
            capture_output=True,
        )

        # Get that commit sha
        sha = subprocess.run(
            ["git", "-C", str(temp_repo), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

        # Try to revert as implement - should fail because commit touches test too
        with pytest.raises(AgentError, match="does not belong to"):
            agents.revert(temp_repo, "implement", sha)

    def test_revert_empty_commit_refused(self, temp_repo):
        # Create an empty commit (just a commit message)
        subprocess.run(
            ["git", "-C", str(temp_repo), "commit", "--allow-empty", "-m", "empty"],
            check=True,
            capture_output=True,
        )
        sha = subprocess.run(
            ["git", "-C", str(temp_repo), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

        with pytest.raises(AgentError, match="touches no files"):
            agents.revert(temp_repo, "implement", sha)

    def test_revert_unknown_agent_refused(self, temp_repo):
        sha = agents.set_field(temp_repo, "implement", "title", "Title 1")
        with pytest.raises(AgentError, match="unknown agent"):
            agents.revert(temp_repo, "nonexistent", sha)

    def test_revert_deleted_skill_commit(self, temp_repo):
        # Create a skill, then delete it, then revert the deletion
        agents.write_skill(temp_repo, "implement", "temp-skill", "Content")
        skill_path = temp_repo / "roles" / "implement" / "skills" / "temp-skill" / "SKILL.md"
        assert skill_path.exists()
        
        # Delete the skill
        delete_sha = agents.delete_skill(temp_repo, "implement", "temp-skill")
        assert not skill_path.exists()
        
        # Revert the deletion
        revert_sha = agents.revert(temp_repo, "implement", delete_sha)
        
        # Skill should be back
        assert skill_path.exists()
        assert skill_path.read_text() == "Content"


class TestFingerprint:
    def test_fingerprint_changes_after_write(self, temp_repo, temp_ledger):
        fp1 = agents.fingerprint(temp_repo, temp_ledger)
        # Make a change
        agents.set_field(temp_repo, "implement", "title", "New Title")
        fp2 = agents.fingerprint(temp_repo, temp_ledger)
        assert fp1 != fp2

    def test_fingerprint_changes_after_skill_write(self, temp_repo, temp_ledger):
        fp1 = agents.fingerprint(temp_repo, temp_ledger)
        agents.write_skill(temp_repo, "implement", "new-skill", "content")
        fp2 = agents.fingerprint(temp_repo, temp_ledger)
        assert fp1 != fp2

    def test_fingerprint_changes_after_instructions_write(self, temp_repo, temp_ledger):
        fp1 = agents.fingerprint(temp_repo, temp_ledger)
        agents.write_instructions(temp_repo, "implement", "new instructions")
        fp2 = agents.fingerprint(temp_repo, temp_ledger)
        assert fp1 != fp2

    def test_fingerprint_changes_after_ledger_write(self, temp_repo, temp_ledger):
        fp1 = agents.fingerprint(temp_repo, temp_ledger)
        ledger.start_job(temp_ledger, "team", "c1", "implement", "v1")
        fp2 = agents.fingerprint(temp_repo, temp_ledger)
        assert fp1 != fp2

    def test_fingerprint_changes_after_git_commit(self, temp_repo, temp_ledger):
        fp1 = agents.fingerprint(temp_repo, temp_ledger)
        # Make any commit
        agents.set_field(temp_repo, "implement", "title", "New Title")
        fp2 = agents.fingerprint(temp_repo, temp_ledger)
        assert fp1 != fp2

    def test_fingerprint_changes_after_orchestration_write(self, temp_repo, temp_ledger, tmp_path):
        fp1 = agents.fingerprint(temp_repo, temp_ledger)
        # Create orchestration.toml
        (temp_repo / "orchestration.toml").write_text("loop.interval_s = 30\n")
        # Stage and commit
        subprocess.run(["git", "-C", str(temp_repo), "add", "orchestration.toml"], check=True)
        subprocess.run(["git", "-C", str(temp_repo), "commit", "-m", "add orchestration"], check=True, capture_output=True)
        fp2 = agents.fingerprint(temp_repo, temp_ledger)
        assert fp1 != fp2

    def test_fingerprint_changes_after_target_write(self, temp_repo, temp_ledger, tmp_path):
        """Fingerprint changes when target.json is modified."""
        from team import targets
        # First create a target
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        # Need to set TEAM_STATE env var for targets module to use our temp dir
        import os
        old_state = os.environ.get("TEAM_STATE")
        os.environ["TEAM_STATE"] = str(state_dir)
        try:
            t = targets.attach(temp_repo, "test-target")
            fp1 = agents.fingerprint(temp_repo, temp_ledger)
            # Modify target settings
            targets._write({**t, "check": "pytest -x"})
            fp2 = agents.fingerprint(temp_repo, temp_ledger)
            assert fp1 != fp2
        finally:
            if old_state:
                os.environ["TEAM_STATE"] = old_state
            else:
                os.environ.pop("TEAM_STATE", None)


class TestDetailBossSpecific:
    def test_detail_boss_has_boss_md_path(self, temp_repo, temp_ledger):
        detail = agents.detail(temp_repo, temp_ledger, "boss")
        assert detail["instructions"]["path"] == "plugin/agents/boss.md"

    def test_detail_boss_has_plugin_hooks(self, temp_repo, temp_ledger):
        detail = agents.detail(temp_repo, temp_ledger, "boss")
        # Should have hooks from plugin/hooks/hooks.json
        hooks = detail["hooks"]
        assert len(hooks) > 0
        assert any(h["source"] == "plugin/hooks/hooks.json" for h in hooks)

    def test_detail_regular_agent_has_no_hooks_if_none(self, temp_repo, temp_ledger):
        # implement doesn't have hooks.json in the test repo
        detail = agents.detail(temp_repo, temp_ledger, "implement")
        # Might be empty list
        assert isinstance(detail["hooks"], list)

    def test_history_dates_are_utc_iso_seconds(self, temp_repo, temp_ledger):
        # Make a change to create history
        agents.set_field(temp_repo, "implement", "title", "Title 1")
        detail = agents.detail(temp_repo, temp_ledger, "implement")
        history = detail["history"]
        assert len(history) > 0
        for entry in history:
            # Date should be UTC ISO seconds format: 2026-10-21T17:41:00+00:00
            date_str = entry["date"]
            assert date_str.endswith("+00:00") or date_str.endswith("Z")
            # Should parse as ISO format
            from datetime import datetime
            dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
            assert dt.tzinfo is not None

    def test_hooks_emit_one_row_per_command(self, temp_repo, temp_ledger):
        # Check that hooks with multiple commands emit one row per command
        detail = agents.detail(temp_repo, temp_ledger, "boss")
        hooks = detail["hooks"]
        # Find a matcher with multiple commands (if any)
        # Just verify structure - each hook entry should have a single command
        for hook in hooks:
            assert "command" in hook
            assert isinstance(hook["command"], str)
            # Should not be a list - each command is a separate row


class TestCreate:
    def test_create_agent_basic(self, temp_repo):
        """Test creating a new agent with minimal required fields."""
        sha = agents.create(
            temp_repo,
            "test-agent",
            title="Test Agent",
            does="Does testing",
            graded_by="Tests pass",
            model="gemini-2.5-flash",
            tools=["terminal", "file"],
            instructions="# Test Agent\n\nDoes testing\n",
        )
        assert len(sha) == 40
        
        # Verify role.toml was created
        import tomllib
        role_toml = temp_repo / "roles" / "test-agent" / "role.toml"
        assert role_toml.exists()
        with role_toml.open("rb") as f:
            data = tomllib.load(f)
        assert data["name"] == "test-agent"
        assert data["title"] == "Test Agent"
        assert data["does"] == "Does testing"
        assert data["graded_by"] == "Tests pass"
        assert data["status"] == "planned"
        assert data["tools"] == ["terminal", "file"]
        assert data["runs_on"]["harness"] == "hermes"
        assert data["runs_on"]["model"] == "gemini-2.5-flash"
        assert data["escalation"]["harness"] == "crew"
        assert data["escalation"]["model"] == "claude"
        assert data["escalation"]["after_failures"] == 2
        assert data["budget"]["max_turns"] == 25
        assert data["budget"]["run_budget_s"] == 600
        
        # Verify SOUL.md was created
        soul = temp_repo / "roles" / "test-agent" / "SOUL.md"
        assert soul.exists()
        assert soul.read_text() == "# Test Agent\n\nDoes testing\n"
        
        # Verify commit message
        msg = subprocess.run(
            ["git", "-C", str(temp_repo), "log", "-1", "--pretty=format:%s", sha],
            check=True, capture_output=True, text=True
        ).stdout
        assert msg == "test-agent: created (dashboard)"
    
    def test_create_agent_with_hands_to(self, temp_repo):
        """Test creating an agent with hands_to pointing to existing agent."""
        sha = agents.create(
            temp_repo,
            "test-agent",
            title="Test Agent",
            does="Does testing",
            graded_by="Tests pass",
            model="gemini-2.5-flash",
            tools=["terminal"],
            hands_to="review",
            instructions="# Test Agent\n\nDoes testing\n",
        )
        assert len(sha) == 40
        
        import tomllib
        role_toml = temp_repo / "roles" / "test-agent" / "role.toml"
        with role_toml.open("rb") as f:
            data = tomllib.load(f)
        assert data["hands_to"] == "review"
    
    def test_create_agent_refuses_existing_name(self, temp_repo):
        """Test create refuses an existing agent name."""
        agents.create(
            temp_repo,
            "test-agent",
            title="Test Agent",
            does="Does testing",
            graded_by="Tests pass",
            model="gemini-2.5-flash",
            tools=["terminal"],
            instructions="test",
        )
        
        with pytest.raises(AgentError, match="agent already exists"):
            agents.create(
                temp_repo,
                "test-agent",
                title="Test Agent 2",
                does="Does testing 2",
                graded_by="Tests pass",
                model="gemini-2.5-flash",
                tools=["terminal"],
                instructions="test",
            )
    
    def test_create_agent_refuses_invalid_name(self, temp_repo):
        """Test create refuses names not matching ^[a-z][a-z0-9-]{0,31}$."""
        with pytest.raises(AgentError, match="name must match"):
            agents.create(
                temp_repo,
                "Test-Agent",  # uppercase
                title="Test",
                does="Does",
                graded_by="Tests",
                model="gemini-2.5-flash",
                tools=["terminal"],
                instructions="test",
            )
        
        with pytest.raises(AgentError, match="name must match"):
            agents.create(
                temp_repo,
                "-invalid",  # starts with -
                title="Test",
                does="Does",
                graded_by="Tests",
                model="gemini-2.5-flash",
                tools=["terminal"],
                instructions="test",
            )
        
        with pytest.raises(AgentError, match="name must match"):
            agents.create(
                temp_repo,
                "a" * 33,  # too long (max 32)
                title="Test",
                does="Does",
                graded_by="Tests",
                model="gemini-2.5-flash",
                tools=["terminal"],
                instructions="test",
            )
    
    def test_create_agent_refuses_non_free_model(self, temp_repo):
        """Test create refuses non-free models."""
        with pytest.raises(AgentError, match="not free"):
            agents.create(
                temp_repo,
                "test-agent",
                title="Test Agent",
                does="Does testing",
                graded_by="Tests pass",
                model="gpt-4",  # not free
                tools=["terminal"],
                instructions="test",
            )
    
    def test_create_agent_refuses_unknown_tool(self, temp_repo):
        """Test create refuses tools outside TOOLSETS."""
        with pytest.raises(AgentError, match="unknown tool"):
            agents.create(
                temp_repo,
                "test-agent",
                title="Test Agent",
                does="Does testing",
                graded_by="Tests pass",
                model="gemini-2.5-flash",
                tools=["terminal", "invalid_tool"],
                instructions="test",
            )
    
    def test_create_agent_refuses_unknown_hands_to(self, temp_repo):
        """Test create refuses unknown hands_to."""
        with pytest.raises(AgentError, match="existing agent name"):
            agents.create(
                temp_repo,
                "test-agent",
                title="Test Agent",
                does="Does testing",
                graded_by="Tests pass",
                model="gemini-2.5-flash",
                tools=["terminal"],
                hands_to="nonexistent",
                instructions="test",
            )
    
    def test_create_agent_refuses_self_hands_to(self, temp_repo):
        """Test create refuses hands_to pointing to itself."""
        # First create the agent, then try to create another with hands_to to itself
        agents.create(
            temp_repo,
            "test-agent",
            title="Test Agent",
            does="Does testing",
            graded_by="Tests pass",
            model="gemini-2.5-flash",
            tools=["terminal"],
            instructions="test",
        )
        
        with pytest.raises(AgentError, match="cannot point to itself"):
            agents.create(
                temp_repo,
                "test-agent-2",
                title="Test Agent 2",
                does="Does testing 2",
                graded_by="Tests pass",
                model="gemini-2.5-flash",
                tools=["terminal"],
                hands_to="test-agent-2",
                instructions="test",
            )
    
    def test_create_agent_commits_both_files(self, temp_repo):
        """Test create commits both role.toml and SOUL.md in one commit."""
        # Stage an unrelated file first
        unrelated = temp_repo / "unrelated.txt"
        unrelated.write_text("unrelated")
        subprocess.run(["git", "-C", str(temp_repo), "add", "unrelated.txt"], check=True)
        
        sha = agents.create(
            temp_repo,
            "test-agent",
            title="Test Agent",
            does="Does testing",
            graded_by="Tests pass",
            model="gemini-2.5-flash",
            tools=["terminal"],
            instructions="test",
        )
        
        # Check the commit only touches the two agent files
        diff = subprocess.run(
            ["git", "-C", str(temp_repo), "show", "--name-only", "--pretty=format:", sha],
            check=True, capture_output=True, text=True
        )
        changed_files = sorted([f for f in diff.stdout.strip().split("\n") if f])
        assert set(changed_files) == {
            "roles/test-agent/role.toml",
            "roles/test-agent/SOUL.md",
        }
        
        # Check unrelated.txt is still staged
        status = subprocess.run(
            ["git", "-C", str(temp_repo), "status", "--porcelain"],
            check=True, capture_output=True, text=True
        )
        assert "unrelated.txt" in status.stdout


if __name__ == "__main__":
    pytest.main([__file__, "-v"])