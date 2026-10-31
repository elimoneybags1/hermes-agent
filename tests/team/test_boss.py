"""The boss's side of Team: proposals Eli approves, notes, status, findings, suggestions."""

import importlib.util
import json
import shutil

import pytest

from team import boss, ledger, profiles, roles
from tests.team.test_catalog import CATALOG
from tests.team.test_lead import Answers, part
from tests.team.test_run import REPO, git, world


@pytest.fixture
def team(world):
    for role in ("lead", "boss"):
        shutil.copytree(REPO / "team" / "roles" / role, world["root"] / "team" / "roles" / role)
    git(world["root"], "add", "-A")
    git(world["root"], "commit", "-q", "-m", "lead and boss")
    world["answers"] = Answers(world["hermes"])
    return world


def plan_answer(*parts):
    return {"summary": "the lead's plan", "parts": list(parts) or [part()]}


def propose(w, goals=None, **kw):
    goals = goals or [{"goal": "Set VALUE to 2"}]
    w["answers"].queue += [plan_answer() for _ in goals]
    return boss.propose(
        w["root"],
        w["conn"],
        "team",
        kw.pop("headline", "Make VALUE two."),
        kw.pop("summary", "VALUE is 1; the check wants 2."),
        goals,
        runner=w["hermes"],
        cat=CATALOG,
        log=lambda m: None,
        **kw,
    )


def test_a_proposal_is_drafted_by_the_lead_and_waits_for_eli(team):
    p = propose(team, notes="Eli wants it before Friday.")
    assert p["status"] == "waiting" and boss.proposal_path("team", p["id"]).exists()
    assert "Eli wants it before Friday." in team["answers"].prompts[0]
    d = ledger.get_decision(team["conn"], p["decision_id"])
    assert d["status"] == "open" and d["question"] == "Make VALUE two."
    card = boss.card(p)
    first = card["goals"][0]["parts"][0]
    assert first["toolsets"] == ["file", "terminal"] and first["model"] == "tier-simple"
    assert first["context"] == ["app.py:1-1"] and first["steps"] == 3
    assert ledger.list_goals(team["conn"], all_goals=True) == []  # nothing on the board yet
    assert team["hermes"].tasks == {}


@pytest.mark.parametrize(
    "kw, says",
    [
        ({"headline": "x" * 200}, "one sentence"),
        ({"summary": ""}, "summary must be"),
        ({"goals": [{"goal": "a"}, {"goal": "b", "after": [2]}]}, "earlier goals"),
        ({"goals": [{"goal": str(n)} for n in range(6)]}, "1 to 5 goals"),
        ({"goals": [{"goal": " "}]}, "goal 1 is empty"),
    ],
)
def test_proposal_refusals(team, kw, says):
    with pytest.raises(boss.BossError, match=says):
        boss.propose(
            team["root"], team["conn"], "team",
            kw.get("headline", "h"), kw.get("summary", "s"), kw.get("goals", [{"goal": "g"}]),
            runner=team["hermes"], cat=CATALOG,
        )


def test_a_lead_failure_leaves_no_proposal(team):
    team["answers"].queue.append(RuntimeError("You've hit your usage limit"))
    with pytest.raises(boss.BossError, match="usage limit"):
        boss.propose(team["root"], team["conn"], "team", "h", "s", [{"goal": "g"}],
                     runner=team["hermes"], cat=CATALOG, log=lambda m: None)
    assert boss.proposals("team") == []
    assert ledger.list_plans(team["conn"])[0]["status"] == "done"


def test_approval_releases_in_order_and_later_goals_wait(team):
    (team["repo"] / "lib.py").write_text("X = 0\n")
    git(team["repo"], "add", "-A")
    git(team["repo"], "commit", "-q", "-m", "lib")
    team["answers"].queue += [plan_answer(), plan_answer(part(owned_paths=["lib.py"]))]
    p = boss.propose(
        team["root"], team["conn"], "team", "Two steps.", "First app, then lib.",
        [{"goal": "Set VALUE to 2"}, {"goal": "Then lib", "after": [1]}],
        runner=team["hermes"], cat=CATALOG, log=lambda m: None,
    )
    team["loop"].tick()
    assert boss.load_proposal("team", p["id"])["status"] == "waiting"
    boss.settle(team["conn"], p["decision_id"], "approve")
    team["loop"].tick()
    p = boss.load_proposal("team", p["id"])
    assert p["status"] == "released" and len(p["goal_ids"]) == 2
    first, second = (ledger.get_goal(team["conn"], g) for g in p["goal_ids"])
    assert first["current_task"] and second["current_task"] is None
    assert ledger.get_part(team["conn"], second["part_id"])["depends_on"] == [first["part_id"]]
    team["loop"].tick()  # released once, not twice
    assert len(ledger.list_goals(team["conn"], all_goals=True)) == 2


def test_dismissal_closes_the_plan(team):
    p = propose(team)
    boss.settle(team["conn"], p["decision_id"], None)
    team["loop"].tick()
    assert boss.load_proposal("team", p["id"])["status"] == "dismissed"
    assert ledger.list_goals(team["conn"], all_goals=True) == []


def test_notes_are_capped(team):
    assert boss.read_notes("team") == ""
    assert boss.write_notes(team["root"], "team", "# Purpose\nShip it.") == len("# Purpose\nShip it.\n")
    assert boss.read_notes("team").startswith("# Purpose")
    with pytest.raises(boss.BossError, match="the cap is 6000"):
        boss.write_notes(team["root"], "team", "x" * 7000)


def test_status_and_findings_stay_short_and_real(team):
    p = propose(team)
    boss.settle(team["conn"], p["decision_id"], "approve")
    team["loop"].tick()
    goal = ledger.get_goal(team["conn"], p and boss.load_proposal("team", p["id"])["goal_ids"][0])
    ledger.finish_goal(team["conn"], goal["id"], "fail", "check failed: 1 test")
    text = boss.status(team["conn"], "team")
    assert "proposals waiting for Eli: 0" in text and f"goal {goal['id']} fail" in text
    assert "implement attempts passed: with order 0/0, without 0/0" in text
    assert len(text) < 3000
    ledger.finish_job(team["conn"], goal["job_id"], "fail", "check", "AssertionError in test_x")
    found = boss.findings(team["conn"], "team")
    assert "1 worker attempts failed" in found and "1x AssertionError in test_x" in found


def test_a_suggestion_is_a_decision_not_an_edit(team):
    d = boss.suggest(team["conn"], "team", "Give implement a commit skill", "Add ...", "3 of 5 failed: no commits")
    row = ledger.get_decision(team["conn"], d)
    assert row["source"] == "boss-suggestion" and row["options"] == ["apply", "dismiss"]
    with pytest.raises(boss.BossError):
        boss.suggest(team["conn"], "team", "t", "", "why")


def _tools():
    spec = importlib.util.spec_from_file_location("team_boss_tools", REPO / "plugins" / "team" / "boss_tools.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_tools_register_and_refuse_politely(team, monkeypatch):
    tools = _tools()
    registered = {}

    class Ctx:
        def register_tool(self, **kw):
            registered[kw["name"]] = kw

    tools.register(Ctx())
    assert set(registered) == {
        "team_status", "team_findings", "team_notes_read", "team_notes_write",
        "team_propose", "team_proposals", "team_suggest",
    }
    assert all(r["toolset"] == "team" for r in registered.values())
    assert not any("approve" in n or "release" in n for n in registered)
    handler = registered["team_notes_write"]["handler"]
    monkeypatch.setattr(tools, "_REPO", team["root"])
    assert handler({"text": "Purpose: test"}) == "saved, 14 characters"
    assert registered["team_notes_read"]["handler"]({}) == "Purpose: test\n"
    assert registered["team_status"]["handler"]({"target": "nope"}).startswith("refused:")
    assert len(tools.schemas()) < 4000  # what the tools add to every boss call


def test_boss_profile_is_lean_and_locked(team):
    role = roles.load(team["root"], "boss", CATALOG)
    cfg = profiles.role_config(team["root"], role, CATALOG)
    assert cfg["model"] == {"provider": "team-subscription", "default": "claude-sonnet-5-5"}
    assert cfg["platform_toolsets"]["cli"] == ["clarify", "memory", "session_search", "team"]
    assert cfg["timeouts"]["tools"]["sequential_call"] == 3600
    assert cfg["compression"]["threshold"] == 0.25 and cfg["compression"]["enabled"] is True
    assert cfg["agent"]["max_turns"] == 20
    assert profiles.audit(cfg) == []


def test_the_tab_api(team):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    spec = importlib.util.spec_from_file_location(
        "team_plugin_api", REPO / "plugins" / "team" / "dashboard" / "plugin_api.py"
    )
    api = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(api)
    app = FastAPI()
    app.include_router(api.router, prefix="/api/plugins/team")
    client = TestClient(app)
    p = propose(team)
    other = ledger.create_decision(team["conn"], "team", "Retry goal 9?", "ctx", ["yes", "no"], "yes")
    data = client.get("/api/plugins/team/inbox").json()
    assert [c["headline"] for c in data["plans"]] == ["Make VALUE two."]
    assert [d["id"] for d in data["decisions"]] == [other]
    assert client.post(f"/api/plugins/team/plans/{p['id']}/approve").status_code == 200
    assert client.post(f"/api/plugins/team/plans/{p['id']}/approve").status_code == 409
    assert ledger.get_decision(team["conn"], p["decision_id"])["answer"] == "approve"
    client.post(f"/api/plugins/team/decisions/{other}/answer", json={"answer": "no"})
    assert ledger.get_decision(team["conn"], other)["answer"] == "no"
    assert json.loads((REPO / "plugins" / "team" / "dashboard" / "manifest.json").read_text())["tab"]["path"] == "/team"


def test_flow_moves_each_goal_from_boss_to_landed(team):
    p = propose(team)
    [plan] = boss.flow(team["conn"])
    assert plan["goals"][0]["station"] == "boss"
    boss.settle(team["conn"], p["decision_id"], "approve")
    assert boss.flow(team["conn"])[0]["goals"][0]["station"] == "lead"
    team["loop"].tick()
    goal = boss.flow(team["conn"])[0]["goals"][0]
    assert goal["station"] == "workers" and goal["parts"][0]["state"] == "attempt 1"
    from tests.team.test_run import worker_commits

    worker_commits(team, ledger.get_goal(team["conn"], boss.load_proposal("team", p["id"])["goal_ids"][0]))
    team["loop"].tick()
    goal = boss.flow(team["conn"])[0]["goals"][0]
    assert goal["station"] == "landed" and goal["parts"][0]["state"] == "landed"
