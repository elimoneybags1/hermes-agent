import shutil
import subprocess
from pathlib import Path

import hermes_yaml as yaml
import pytest

from team import profiles
from tests.team.test_catalog import CATALOG
from tests.team.test_roles import write_role

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def root(tmp_path):
    r = tmp_path / "repo"
    (r / "team" / "roles" / "_base").mkdir(parents=True)
    shutil.copy(
        REPO / "team" / "roles" / "_base" / "config.yaml",
        r / "team" / "roles" / "_base" / "config.yaml",
    )
    write_role(r, "implement", skills=("commit-before-done",))
    return r


class CreatingRunner:
    """Pretends to be `team-hermes profile create`: makes the directory."""

    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def __call__(self, cmd, **kwargs):
        self.calls.append(cmd)
        if self.fail:
            return subprocess.CompletedProcess(
                cmd, 1, stdout="", stderr="no such source profile"
            )
        (profiles.profile_dir(cmd[3])).mkdir(parents=True)
        return subprocess.CompletedProcess(cmd, 0, stdout="created", stderr="")


def test_paths_under_team_state(team_state):
    assert profiles.hermes_home() == team_state / "hermes"
    assert (
        profiles.profile_dir("implement")
        == team_state / "hermes" / "profiles" / "implement"
    )


def test_role_config_applies_overrides(root):
    role = {
        "name": "implement",
        "runs_on": {"harness": "hermes"},
        "tools": ["terminal", "file"],
        "budget": {"max_turns": 25},
        "models": {"candidates": ["nvidia:z-ai/glm-5.3"]},
    }
    cfg = profiles.role_config(root, role, CATALOG)
    assert cfg["model"] == {"provider": "litellm", "default": "z-ai/glm-5.3"}
    assert cfg["platform_toolsets"]["cli"] == [
        "file",
        "memory",
        "session_search",
        "skills",
        "terminal",
        "todo",
    ]
    assert cfg["agent"]["max_turns"] == 25
    assert profiles.audit(cfg) == []
    boss = {
        "name": "boss",
        "runs_on": {"harness": "hermes-subscription", "model": "claude-opus-5-5"},
        "tools": ["kanban", "team"],
        "budget": {"max_turns": 40},
        "models": {"candidates": []},
    }
    cfg = profiles.role_config(root, boss)
    assert cfg["model"] == {"provider": "team-subscription", "default": "claude-opus-5-5"}
    assert cfg["providers"]["team-subscription"]["key_env"] == "LITELLM_MASTER_KEY"
    assert cfg["providers"]["team-subscription"]["base_url"] == cfg["providers"]["litellm"]["base_url"]
    assert cfg["platform_toolsets"]["cli"] == ["kanban", "team"]
    assert cfg["auxiliary"]["vision"]["provider"] == "litellm"
    assert profiles.audit(cfg) == []


def test_sync_creates_then_overwrites_versioned_files_only(root, team_state):
    runner = CreatingRunner()
    changes = profiles.sync(root, "implement", CATALOG, runner=runner)
    assert changes == [
        "root config.yaml",
        "created profile implement",
        "config.yaml",
        "SOUL.md",
        "skill commit-before-done",
    ]
    cmd = runner.calls[0]
    assert cmd[1:] == [
        "profile",
        "create",
        "implement",
        "--clone-from",
        "default",
        "--no-alias",
        "--description",
        "Builds one task.",
    ]
    pdir = profiles.profile_dir("implement")
    cfg = yaml.safe_load((pdir / "config.yaml").read_text())
    assert cfg["model"]["default"] == "tier-simple" and cfg["agent"]["max_turns"] == 30
    assert (pdir / "SOUL.md").read_text() == "# implement\n"
    assert (pdir / "skills" / "commit-before-done" / "SKILL.md").exists()
    root_cfg = yaml.safe_load((team_state / "hermes" / "config.yaml").read_text())
    assert (
        root_cfg["model"]["provider"] == "litellm"
        and "kanban" in root_cfg["platform_toolsets"]["cli"]
    )

    # Learned files survive; versioned files are rewritten when they drift.
    (pdir / "memories").mkdir()
    (pdir / "memories" / "MEMORY.md").write_text("learned\n")
    (pdir / "skills" / "learned-skill").mkdir()
    (pdir / "skills" / "learned-skill" / "SKILL.md").write_text("mine\n")
    (pdir / "SOUL.md").write_text("drifted\n")
    (pdir / "skills" / "commit-before-done" / "SKILL.md").write_text("drifted\n")
    assert profiles.sync(root, "implement", CATALOG, runner=runner) == [
        "SOUL.md",
        "skill commit-before-done",
    ]
    assert (pdir / "memories" / "MEMORY.md").read_text() == "learned\n"
    assert (pdir / "skills" / "learned-skill" / "SKILL.md").read_text() == "mine\n"
    assert (pdir / "SOUL.md").read_text() == "# implement\n"
    assert len(runner.calls) == 1
    assert profiles.sync(root, "implement", CATALOG, runner=runner) == []
    assert profiles.sync_all(root, CATALOG, runner=runner) == {"implement": []}


def test_sync_refuses_when_create_fails_or_role_is_bad(root, team_state):
    with pytest.raises(profiles.ProfileError, match="profile create implement failed"):
        profiles.sync(root, "implement", CATALOG, runner=CreatingRunner(fail=True))
    with pytest.raises(Exception, match="unknown role"):
        profiles.sync(root, "ghost", CATALOG, runner=CreatingRunner())


def test_sync_writes_the_master_key_env(root, team_state):
    (team_state / "litellm").mkdir()
    (team_state / "litellm" / ".env").write_text(
        "LITELLM_MASTER_KEY=sk-team-1\nOTHER=x\n"
    )
    runner = CreatingRunner()
    changes = profiles.sync(root, "implement", CATALOG, runner=runner)
    assert ".env" in changes
    pdir = profiles.profile_dir("implement")
    assert (pdir / ".env").read_text() == "LITELLM_MASTER_KEY=sk-team-1\n"
    assert (
        team_state / "hermes" / ".env"
    ).read_text() == "LITELLM_MASTER_KEY=sk-team-1\n"
    assert oct((pdir / ".env").stat().st_mode)[-3:] == "600"
    (pdir / ".env").write_text("SUBSCRIPTION_TOKEN=tok\nLITELLM_MASTER_KEY=old\n")
    assert ".env" in profiles.sync(root, "implement", CATALOG, runner=runner)
    assert (
        pdir / ".env"
    ).read_text() == "SUBSCRIPTION_TOKEN=tok\nLITELLM_MASTER_KEY=sk-team-1\n"
    assert profiles.audit_profiles(root) == {}


def test_audit_profiles_reads_every_home(root, team_state):
    assert profiles.audit_profiles(root) == {"default": ["config.yaml is missing"]}
    profiles.sync(root, "implement", CATALOG, runner=CreatingRunner())
    assert profiles.audit_profiles(root) == {}
    pdir = profiles.profile_dir("implement")
    (pdir / ".env").write_text("OPENROUTER_API_KEY=x\n")
    cfg = yaml.safe_load((pdir / "config.yaml").read_text())
    cfg["fallback_providers"] = [{"provider": "openrouter"}]
    (pdir / "config.yaml").write_text(yaml.safe_dump(cfg))
    found = profiles.audit_profiles(root)
    assert set(found) == {"implement"}
    assert "fallback_providers is not empty" in found["implement"]
    assert (
        ".env holds OPENROUTER_API_KEY, which no Team profile may have"
        in found["implement"]
    )
    (pdir / "config.yaml").write_text("- not: [a mapping")
    assert "unreadable" in profiles.audit_profiles(root)["implement"][0]
