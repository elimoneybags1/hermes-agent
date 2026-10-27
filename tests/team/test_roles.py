import subprocess

import pytest

from team import roles
from tests.team.test_catalog import CATALOG

ROLE = """
name = "implement"
title = "Implement"
does = "Builds one task."
graded_by = "The check passes."
status = "active"
tools = ["terminal", "file"]

[runs_on]
harness = "hermes"

[escalation]
after_failures = 2

[budget]
max_turns = 30
run_budget_s = 600

[models]
candidates = ["tier-simple", "nvidia:z-ai/glm-5.3"]
"""


def write_role(root, name, text=ROLE, skills=()):
    d = root / "team" / "roles" / name
    d.mkdir(parents=True)
    (d / "role.toml").write_text(text.replace('name = "implement"', f'name = "{name}"'))
    (d / "SOUL.md").write_text(f"# {name}\n")
    for s in skills:
        (d / "skills" / s).mkdir(parents=True)
        (d / "skills" / s / "SKILL.md").write_text(f"---\nname: {s}\n---\nDo it.\n")
    return d


def test_names_load_skills_and_soul(tmp_path):
    assert roles.names(tmp_path) == []
    write_role(tmp_path, "implement", skills=("commit-before-done", "merge-main-first"))
    (tmp_path / "team" / "roles" / "notes").mkdir()
    assert roles.names(tmp_path) == ["implement"]
    role = roles.load(tmp_path, "implement", CATALOG)
    assert role["budget"] == {"max_turns": 30, "run_budget_s": 600}
    assert role["escalation"]["after_failures"] == 2
    assert role["skills"] == ["commit-before-done", "merge-main-first"]
    assert roles.soul(tmp_path, "implement") == "# implement\n"
    assert roles.soul(tmp_path, "nope") is None
    assert [r["name"] for r in roles.load_all(tmp_path, CATALOG)] == ["implement"]


def test_defaults_fill_in(tmp_path):
    text = ROLE.replace("[budget]\nmax_turns = 30\nrun_budget_s = 600\n", "").replace(
        "[escalation]\nafter_failures = 2\n", ""
    )
    write_role(tmp_path, "implement", text)
    role = roles.load(tmp_path, "implement")
    assert role["budget"] == {"max_turns": 40, "run_budget_s": 900}
    assert role["escalation"]["after_failures"] == 0


@pytest.mark.parametrize(
    "edit, message",
    [
        (('does = "Builds one task."', 'does = ""'), "needs 'does'"),
        (('name = "implement"', 'name = "other"'), "says name"),
        (('status = "active"', 'status = "live"'), "status must be"),
        (('harness = "hermes"', 'harness = "crew"'), "runs_on.harness"),
        (('tools = ["terminal", "file"]', 'tools = ["laser"]'), "unknown toolset"),
        (("max_turns = 30", "max_turns = 0"), "budget.max_turns"),
        (("after_failures = 2", "after_failures = -1"), "after_failures"),
        (
            ('candidates = ["tier-simple", "nvidia:z-ai/glm-5.3"]', "candidates = []"),
            "needs models.candidates",
        ),
        (
            ('"nvidia:z-ai/glm-5.3"', '"anthropic:claude-x"'),
            "refused: provider 'anthropic' is blocked",
        ),
    ],
)
def test_load_refusals(tmp_path, edit, message):
    old, new = edit
    assert old in ROLE
    write_role(tmp_path, "implement", ROLE.replace(old, new))
    with pytest.raises(roles.RoleError, match=message):
        roles.load(tmp_path, "implement", CATALOG)
    with pytest.raises(roles.RoleError, match="unknown role"):
        roles.load(tmp_path, "ghost")


def test_candidate_per_attempt():
    role = {"models": {"candidates": ["a", "b"]}}
    assert [roles.candidate(role, n) for n in (1, 2, 3, 9)] == ["a", "b", "b", "b"]
    assert roles.candidate({"models": {"candidates": ["a"], "pinned": "p"}}, 2) == "p"
    assert roles.candidate({}, 1) is None


def test_version_is_the_last_commit_touching_the_role(tmp_path):
    write_role(tmp_path, "implement")
    subprocess.run(["git", "-C", str(tmp_path), "init", "-q", "-b", "main"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "T"], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.email", "t@t"], check=True
    )
    assert roles.version(tmp_path, "implement") == "uncommitted"
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "commit", "-q", "-m", "role"], check=True
    )
    sha = subprocess.run(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    assert roles.version(tmp_path, "implement") == sha
    assert roles.version(tmp_path, "other") == "uncommitted"
