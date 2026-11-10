"""integrations/bin/hermes-project: which Hermes profile a directory gets."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "integrations" / "bin" / "hermes-project"


def profile_for(path: Path, home: Path) -> str:
    env = {**os.environ, "HOME": str(home)}
    out = subprocess.run([str(SCRIPT), str(path)], env=env, capture_output=True, text=True, check=True)
    return out.stdout.strip()


def git(*args: str) -> None:
    subprocess.run(["git", *args], check=True, capture_output=True,
                   env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})


@pytest.fixture
def home(tmp_path):
    return tmp_path.resolve()


def test_team_repo_and_its_subdirs_are_default(home):
    assert profile_for(ROOT, home) == "default"
    assert profile_for(ROOT / "integrations", home) == "default"


def test_home_and_root_are_default(home):
    assert profile_for(home, home) == "default"
    assert profile_for(Path("/"), home) == "default"


def test_git_root_under_work_names_the_profile(home):
    repo = home / "work" / "signal" / "exp"
    (repo / "src").mkdir(parents=True)
    git("-C", str(repo), "init", "-q")
    assert profile_for(repo / "src", home) == "signal-exp"


def test_worktree_shares_its_repo_profile(home):
    repo = home / "work" / "markets"
    repo.mkdir(parents=True)
    git("-C", str(repo), "init", "-q")
    git("-C", str(repo), "commit", "-q", "--allow-empty", "-m", "x")
    git("-C", str(repo), "worktree", "add", "-q", str(home / "wt"))
    assert profile_for(home / "wt", home) == "markets"


def test_non_git_folder_outside_work_uses_its_name(home):
    d = home / "other" / "My App"
    d.mkdir(parents=True)
    assert profile_for(d, home) == "my-app"


@pytest.mark.parametrize("name", ["Chat", "config", "default"])
def test_reserved_names_get_a_prefix(home, name):
    d = home / "work" / name
    d.mkdir(parents=True)
    assert profile_for(d, home) == f"p-{name.lower()}"
