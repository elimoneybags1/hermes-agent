"""Per-target overlays narrow a role, add to it, and are committed; they never widen it."""

from __future__ import annotations

import pytest

from team import overlay, roles
from tests.team.test_catalog import CATALOG
from tests.team.test_roles import write_role


@pytest.fixture
def root(tmp_path):
    write_role(tmp_path, "implement", skills=("commit-before-done",))
    return tmp_path


def _overlay(target, role, toml="", soul=None, skills=()):
    d = overlay.role_dir(target, role)
    d.mkdir(parents=True, exist_ok=True)
    if toml:
        (d / "role.toml").write_text(toml)
    if soul:
        (d / "SOUL.md").write_text(soul)
    for s in skills:
        (d / "skills" / s).mkdir(parents=True)
        (d / "skills" / s / "SKILL.md").write_text(f"---\nname: {s}\ndescription: for this repo\n---\nDo it here.\n")
    return d


def test_no_overlay_means_the_role_as_is(root):
    assert overlay.roles("studyapp") == []
    role = roles.load(root, "implement", CATALOG, target="studyapp")
    assert "overlay" not in role and "profile" not in role and role["soul"] == "# implement\n"


def test_an_overlay_narrows_tools_models_and_budget_and_adds_skills_and_soul(root):
    _overlay(
        "studyapp", "implement",
        toml='tools = ["file"]\n[models]\ncandidates = ["nvidia:z-ai/glm-5.3"]\n[budget]\nmax_turns = 20\n[hermes.compression]\nthreshold = 0.2\n',
        soul="Run `npm test -- {path}` for one file.",
        skills=("prisma-migrations",),
    )
    assert overlay.roles("studyapp") == ["implement"]
    role = roles.load(root, "implement", CATALOG, target="studyapp")
    assert role["tools"] == ["file"] and role["models"]["candidates"] == ["nvidia:z-ai/glm-5.3"]
    assert role["budget"]["max_turns"] == 20 and role["budget"]["run_budget_s"] == 600
    assert role["hermes"]["compression"]["threshold"] == 0.2
    assert role["skills"] == ["commit-before-done", "prisma-migrations"]
    assert role["skill_dirs"]["prisma-migrations"] == overlay.role_dir("studyapp", "implement") / "skills" / "prisma-migrations"
    assert role["soul"].startswith("# implement") and "# For target studyapp" in role["soul"] and "npm test" in role["soul"]
    assert role["overlay"] == "studyapp" and role["profile"] == "implement--studyapp"
    summaries = roles.skill_summaries(root, "implement", role)
    assert summaries["prisma-migrations"] == "for this repo"
    # the shared roster is untouched
    base = roles.load(root, "implement", CATALOG)
    assert base["tools"] == ["terminal", "file"] and base["skills"] == ["commit-before-done"]


@pytest.mark.parametrize(
    "toml, says",
    [
        ('tools = ["terminal", "file", "browser"]\n', "not the role's to give"),
        ('[models]\ncandidates = ["tier-complex"]\n', "not among the role's"),
        ('[models]\npinned = "tier-complex"\n', "is not a candidate"),
        ("[budget]\nmax_turns = 99\n", "from 1 to the role's 30"),
        ('status = "planned"\n', "may only narrow"),
        ('[hermes]\nmodel = {provider = "openai"}\n', "the lock owns them"),
    ],
)
def test_an_overlay_can_never_widen(root, toml, says):
    _overlay("studyapp", "implement", toml=toml)
    with pytest.raises(roles.RoleError, match=says):
        roles.load(root, "implement", CATALOG, target="studyapp")


def test_the_overlay_is_a_git_repository_with_one_commit_per_change(root):
    _overlay("studyapp", "implement", soul="first")
    sha = overlay.commit("studyapp", "implement: first soul")
    assert sha and len(sha) == 40
    assert overlay.commit("studyapp", "nothing") is None
    (overlay.role_dir("studyapp", "implement") / "SOUL.md").write_text("second")
    assert overlay.commit("studyapp", "implement: second") != sha
