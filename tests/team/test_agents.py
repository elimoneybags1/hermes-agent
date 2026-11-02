"""New agents: proposed on a branch, approved from the inbox, on trial in the roster. And the
retrospective that counts recurring causes across projects."""

from __future__ import annotations

import pytest

from team import boss, grade, ledger, profiles, roles
from tests.team.test_boss import team  # fixture
from tests.team.test_catalog import CATALOG
from tests.team.test_run import world  # fixture team builds on

SOUL = "# ui-build\n\nYou build React components; run the browser test before you finish.\n"


def agent(w, **over):
    kw = dict(
        name="ui-build", title="UI builder", does="Builds React components and their browser tests.",
        graded_by="The check and the part's acceptance pass; only owned files changed.", gate="check",
        tools=["terminal", "file", "kanban"], candidates=["tier-simple"], soul=SOUL,
        why="3 UI parts in 2 projects failed on implement with cause 'skill' (3/3 vs base 2/14)",
        skills=[{"name": "browser-first", "content": "---\nname: browser-first\n---\nOpen the page before you commit.\n"}],
        extends="implement", runner=w["hermes"], cat=CATALOG, log=lambda m: None,
    )
    kw.update(over)
    return boss.propose_agent(w["root"], w["conn"], "team", **kw)


def test_a_proposed_agent_is_a_branch_and_a_decision_and_approval_puts_it_on_trial(team):
    r = agent(team)
    assert r["branch"] == "team/agent-ui-build" and r["name"] == "ui-build"
    assert "ui-build" not in roles.names(team["root"])  # nothing on main yet
    d = ledger.get_decision(team["conn"], r["decision_id"])
    assert d["options"] == ["approve", "dismiss"] and d["source"] == "boss-agent:ui-build"
    assert "gate `check`" in d["context"] and "browser-first" in d["context"] and "plus implement's" in d["context"]
    assert "ui-build" in boss.status(team["conn"], "team")
    assert [a["status"] for a in boss.agents()] == ["waiting"]
    with pytest.raises(boss.BossError, match="already waiting"):
        agent(team)
    # Eli approves: the merge puts the role in the roster on trial, with its profile synced
    ledger.answer_decision(team["conn"], d["id"], "approve")
    [line] = boss.settle_agents(team["root"], team["conn"], runner=team["hermes"], cat=CATALOG, log=lambda m: None)
    assert line.startswith("agent ui-build joined the roster on trial")
    role = roles.load(team["root"], "ui-build", CATALOG)
    assert role["status"] == "trial" and role["gate"] == "check" and role["extends"] == "implement"
    assert role["skills"] == ["browser-first", "commit-before-done"]  # its own plus implement's
    assert profiles.profile_dir("ui-build").exists()
    assert not grade.branch_exists(team["root"], r["branch"])
    assert boss.agents()[0]["status"] == "adopted"
    assert boss.settle_agents(team["root"], team["conn"], runner=team["hermes"], cat=CATALOG, log=lambda m: None) == []
    # a trial role is a builder the lead may assign, and a station on the board
    from team import board, lead

    menu = lead.menu(team["root"], CATALOG)
    assert "ui-build" in menu and "on trial" in menu["ui-build"]["does"]
    assert "ui-build" in board.stations(team["root"])
    assert "ui-build" in team["loop"]._active_roles()


def test_a_dismissed_agent_keeps_its_branch(team):
    r = agent(team, name="backend-build", extends=None, skills=[])
    ledger.dismiss_decision(team["conn"], r["decision_id"])
    [line] = boss.settle_agents(team["root"], team["conn"], runner=team["hermes"], cat=CATALOG, log=lambda m: None)
    assert "dismissed" in line and grade.branch_exists(team["root"], r["branch"])
    assert "backend-build" not in roles.names(team["root"])


@pytest.mark.parametrize(
    "over, says",
    [
        ({"name": "Implement!"}, "lowercase letters"),
        ({"name": "implement"}, "exists"),
        ({"gate": "vibes"}, "gate must be one of"),
        ({"tools": ["browser-magic"]}, "not toolsets"),
        ({"candidates": ["gpt-9"]}, "refused by the catalog"),
        ({"soul": ""}, "SOUL.md"),
        ({"extends": "nobody"}, "extends names no existing role"),
        ({"skills": [{"name": "Bad Name", "content": "x"}]}, "each skill is"),
        ({"why": ""}, "why cannot be empty"),
    ],
)
def test_an_agent_proposal_is_refused_when_it_names_what_does_not_exist(team, over, says):
    with pytest.raises(boss.BossError, match=says):
        agent(team, **over)
    assert boss.agents() == []


def test_the_retrospective_counts_a_cause_only_when_it_recurs_across_projects(team):
    conn = team["conn"]
    assert boss.retrospective(conn, "team") == ["no cause recurred across projects in 30 days (diagnosed goals by cause: none)"]
    plan_a = ledger.create_plan(conn, "team", "A", status="active")
    plan_b = ledger.create_plan(conn, "team", "B", status="active")
    goals = []
    for n, plan in enumerate((plan_a, plan_a, plan_b), 1):
        part = ledger.create_part(conn, plan, "team", f"part {n}", stage="implement")
        goals.append(ledger.create_goal(conn, "team", f"g{n}", "implement", f"team/g{n}", f"/wt{n}", "./check.sh", part_id=part))
    for g in goals[:2]:
        ledger.add_diagnosis(conn, g, None, "no tests in the order", "add them", "retry", cause_tag="order")
    lines = boss.retrospective(conn, "team")
    assert lines[0].startswith("no cause recurred") and "order 2" in lines[0]
    ledger.add_diagnosis(conn, goals[2], None, "again no tests", "add them", "retry", cause_tag="order")
    lines = boss.retrospective(conn, "team")
    assert lines == [
        "recurring causes, at least 3 goals in 2 projects over 30 days:",
        "  order: 3 goals in 2 projects",
    ]
    assert ledger.cause_counts(conn, "team") == {"order": {"goals": 3, "plans": 2}}
    note = boss.review_note({"id": 1, "headline": "H.", "target": "team", "goals": [{"status": "landed"}]}, lines)
    assert "Retrospective: recurring causes" in note and "team_propose_agent" in note
