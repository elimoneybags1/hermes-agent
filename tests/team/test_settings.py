import subprocess
import tomllib

import pytest

from team import settings, targets


def git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    )


@pytest.fixture
def root(tmp_path):
    r = tmp_path / "root"
    (r / "team").mkdir(parents=True)
    git(r, "init", "-q", "-b", "main")
    git(r, "config", "user.name", "T")
    git(r, "config", "user.email", "t@t")
    (r / "README.md").write_text("x\n")
    git(r, "add", "-A")
    git(r, "commit", "-q", "-m", "one")
    return r


def test_defaults_without_a_file(root):
    cfg = settings.load(root)
    assert cfg["loop.interval_s"] == 60
    assert cfg["grading.default_check"] == "pytest -q"
    assert cfg["escalation.claude"] is True
    assert {f["key"] for f in settings.fields()} == set(settings.FIELDS)
    assert settings.fields()[0]["type"] == "int"


def test_set_value_writes_toml_and_commits(root):
    sha = settings.set_value(root, "loop.interval_s", 30, source="test")
    assert sha == git(root, "rev-parse", "HEAD").stdout.strip()
    assert (
        "settings: loop.interval_s = 30 (test)"
        in git(root, "log", "-1", "--format=%s").stdout
    )
    settings.set_value(root, "grading.default_check", '  pytest "a b" -q ')
    settings.set_value(root, "escalation.claude", False)
    text = (root / "team" / "orchestration.toml").read_text()
    data = tomllib.loads(text)
    assert data == {
        "loop": {"interval_s": 30},
        "grading": {"default_check": 'pytest "a b" -q'},
        "escalation": {"claude": False},
    }
    cfg = settings.load(root)
    assert cfg["loop.interval_s"] == 30 and cfg["escalation.claude"] is False
    assert cfg["loop.max_new_per_tick"] == 3
    assert git(root, "status", "--porcelain").stdout == ""


@pytest.mark.parametrize(
    "key, value, message",
    [
        ("loop.speed", 1, "unknown setting"),
        ("loop.interval_s", 0, "above 0"),
        ("loop.interval_s", True, "above 0"),
        ("escalation.claude", "yes", "true or false"),
        ("grading.default_check", "  ", "non-empty"),
    ],
)
def test_set_value_refusals(root, key, value, message):
    with pytest.raises(settings.SettingsError, match=message):
        settings.set_value(root, key, value)


def test_load_refuses_a_bad_value_in_the_file(root):
    (root / "team" / "orchestration.toml").write_text("[loop]\ninterval_s = -5\n")
    with pytest.raises(settings.SettingsError, match="above 0"):
        settings.load(root)


def test_check_for_prefers_the_target_check(root, team_state):
    targets.attach(root, name="t")
    assert settings.check_for(root, "t") == "pytest -q"
    targets.set_field("t", "check", "make test")
    assert settings.check_for(root, "t") == "make test"
    with pytest.raises(targets.TargetError):
        settings.check_for(root, "nope")
