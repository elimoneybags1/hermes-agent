import json
import subprocess
import tomllib

import pytest

from team import settings, targets
from team.agents import AgentError


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    git(r, "config", "user.name", "t")
    git(r, "config", "user.email", "t@t")
    (r / "README").write_text("x\n")
    git(r, "add", "README")
    git(r, "commit", "-qm", "init")
    return r


@pytest.fixture
def state(monkeypatch, tmp_path):
    monkeypatch.setenv("TEAM_STATE", str(tmp_path / "state"))


def test_load_gives_defaults_without_a_file(repo):
    data = settings.load(repo)
    assert data["grading.default_check"] == "uv run pytest -q"
    assert data["escalation.claude"] is True
    assert set(data) == set(settings.FIELDS)


def test_set_value_writes_toml_and_commits_only_that_file(repo):
    (repo / "README").write_text("staged elsewhere\n")
    git(repo, "add", "README")
    sha = settings.set_value(repo, "loop.interval_s", 30)
    assert git(repo, "rev-parse", "HEAD") == sha
    assert git(repo, "show", "--name-only", "--format=", sha) == "orchestration.toml"
    assert "settings: loop.interval_s = 30 (dashboard)" in git(repo, "log", "-1", "--format=%s")
    assert tomllib.loads((repo / "orchestration.toml").read_text())["loop"]["interval_s"] == 30
    assert settings.load(repo)["loop.interval_s"] == 30
    assert "README" in git(repo, "diff", "--cached", "--name-only")


@pytest.mark.parametrize("key,value", [
    ("loop.interval_s", 0),
    ("loop.interval_s", "60"),
    ("loop.interval_s", True),
    ("escalation.claude", "yes"),
    ("grading.default_check", "  "),
    ("nope.key", 1),
])
def test_set_value_refuses_bad_values(repo, key, value):
    with pytest.raises(AgentError):
        settings.set_value(repo, key, value)


def test_set_value_same_value_twice_is_no_change(repo):
    settings.set_value(repo, "escalation.claude", False)
    with pytest.raises(AgentError, match="no change"):
        settings.set_value(repo, "escalation.claude", False)


def test_fields_lists_every_setting():
    keys = [f["key"] for f in settings.fields()]
    assert keys == list(settings.FIELDS)
    assert {f["type"] for f in settings.fields()} <= {"int", "str", "bool"}


def test_target_settings_and_set_target(repo, state):
    targets.attach(repo, name="demo")
    assert settings.target_settings("demo")["check"] is None
    assert settings.target_settings("demo")["enabled"] is True
    assert settings.check_for(repo, "demo") == "uv run pytest -q"

    out = settings.set_target("demo", "check", "make test")
    assert out["check"] == "make test"
    assert settings.check_for(repo, "demo") == "make test"
    saved = json.loads((targets.targets_dir() / "demo" / "target.json").read_text())
    assert saved["check"] == "make test" and saved["name"] == "demo"

    assert settings.set_target("demo", "check", "")["check"] is None
    assert settings.set_target("demo", "enabled", False)["enabled"] is False


@pytest.mark.parametrize("name,key,value", [
    ("ghost", "check", "x"),
    ("../demo", "check", "x"),
    ("demo", "path", "/tmp"),
    ("demo", "enabled", "no"),
])
def test_set_target_refusals(repo, state, name, key, value):
    targets.attach(repo, name="demo")
    with pytest.raises(AgentError):
        settings.set_target(name, key, value)
