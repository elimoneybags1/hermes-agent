"""The boss's side of Team: projects Eli approves goal by goal, questions, notes, status."""

import importlib.util
import json
import shutil

import pytest

from team import boss, ledger, profiles, roles
from tests.team.test_catalog import CATALOG
from tests.team.test_lead import Answers, part
from tests.team.test_run import REPO, git, worker_commits, world


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
    """A project with one goal (or ``goals``); the lead's answer for each goal is queued."""
    goals = goals or [{"goal": "Set VALUE to 2"}]
    if kw.pop("draft", True):
        w["answers"].queue += [plan_answer() for _ in goals]
    return boss.propose(
        w["root"],
        w["conn"],
        "team",
        kw.pop("headline", "Make VALUE two."),
        kw.pop("summary", "VALUE is 1; the check wants 2."),
        goals,
        **kw,
    )


def approve(w, p, n=1):
    boss.settle(w["conn"], boss.goal_of(p, n)["decision_id"], "approve")


def skip(w, p, n=1):
    boss.settle(w["conn"], boss.goal_of(p, n)["decision_id"], None)


def saved(w, p):
    return boss.load_proposal("team", p["id"])


def test_a_goal_is_a_card_the_moment_the_boss_writes_it(team):
    p = propose(team, notes="Eli wants it before Friday.")
    assert p["status"] == "open" and boss.proposal_path("team", p["id"]).exists()
    g = p["goals"][0]
    assert g["n"] == 1 and g["status"] == "proposed" and g["notes"] == "Eli wants it before Friday."
    d = ledger.get_decision(team["conn"], g["decision_id"])
    assert d["status"] == "open" and d["question"] == "Set VALUE to 2"
    assert d["options"] == ["approve", "skip"] and d["source"] == f"boss-goal:{p['id']}:1"
    assert boss.plan_of(d) == p["id"]
    assert team["answers"].prompts == []  # the lead is not asked until Eli approves
    c = boss.card(p)
    assert c["goals"][0]["parts"] == [] and c["goals"][0]["plan"] is None
    assert c["counts"] == {"proposed": 1} and c["goal_ids"] == []
    assert ledger.list_goals(team["conn"], all_goals=True) == []
    assert team["hermes"].tasks == {}
    assert boss.waiting("team") == [{"plan_id": p["id"], "headline": "Make VALUE two.", "n": 1, "goal": "Set VALUE to 2"}]
    assert ledger.list_plans(team["conn"])[0]["status"] == "draft"


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
        )


def test_approval_has_the_lead_draft_the_goal_and_releases_it(team):
    p = propose(team, notes="Eli wants it before Friday.")
    team["loop"].tick()
    assert saved(team, p)["goals"][0]["status"] == "proposed"  # nothing moves without Eli
    approve(team, p)
    team["loop"].tick()
    g = saved(team, p)["goals"][0]
    assert g["status"] == "released" and len(g["goal_ids"]) == 1 and g["part_ids"]
    assert "Eli wants it before Friday." in team["answers"].prompts[0]
    first = boss.card(saved(team, p))["goals"][0]["parts"][0]
    assert first["toolsets"] == ["file", "terminal"] and first["model"] == "tier-simple"
    assert first["context"] == ["app.py:1-1"] and first["steps"] == 3
    assert ledger.list_plans(team["conn"])[0]["status"] == "active"
    assert ledger.get_goal(team["conn"], g["goal_ids"][0])["current_task"]
    team["loop"].tick()  # released once, not twice
    assert len(ledger.list_goals(team["conn"], all_goals=True)) == 1


def test_a_goal_after_another_waits_for_it_and_the_rest_build_alongside(team):
    for name in ("lib.py", "c.py"):
        (team["repo"] / name).write_text("X = 0\n")
    git(team["repo"], "add", "-A")
    git(team["repo"], "commit", "-q", "-m", "more files")
    team["answers"].queue += [
        plan_answer(),
        plan_answer(part(owned_paths=["lib.py"])),
        plan_answer(part(owned_paths=["c.py"])),
    ]
    p = propose(
        team, draft=False,
        goals=[{"goal": "Set VALUE to 2"}, {"goal": "Then lib", "after": [1]}, {"goal": "And c"}],
    )
    assert boss.settle_goals(team["conn"], p, "approve") == [1, 2, 3]
    team["loop"].tick()  # one draft per tick: goal 1
    g1, g2, g3 = saved(team, p)["goals"]
    assert [g["status"] for g in (g1, g2, g3)] == ["released", "approved", "approved"]
    team["loop"].tick()  # goal 2 now that goal 1 has parts to wait on
    team["loop"].tick()  # goal 3
    g1, g2, g3 = saved(team, p)["goals"]
    assert [g["status"] for g in (g1, g2, g3)] == ["released"] * 3
    first, second, third = (ledger.get_goal(team["conn"], g["goal_ids"][0]) for g in (g1, g2, g3))
    assert first["current_task"] and third["current_task"]  # build at the same time
    assert second["current_task"] is None  # waits for goal 1's part to land
    assert ledger.get_part(team["conn"], second["part_id"])["depends_on"] == [first["part_id"]]
    assert [g["station"] for g in boss.flow(team["conn"])[0]["goals"]] == ["workers"] * 3


def test_goals_are_approved_and_skipped_one_by_one(team):
    p = propose(team, draft=False, goals=[{"goal": "a"}, {"goal": "b", "after": [1]}])
    team["answers"].queue.append(plan_answer())
    skip(team, p, 1)
    approve(team, p, 2)
    team["loop"].tick()
    g1, g2 = saved(team, p)["goals"]
    assert g1["status"] == "skipped" and g2["status"] == "released"
    # after a skipped goal there is nothing to wait for: it starts right away
    assert ledger.get_goal(team["conn"], g2["goal_ids"][0])["current_task"]
    assert boss.card(saved(team, p))["counts"] == {"skipped": 1, "released": 1}
    assert [g["station"] for g in boss.flow(team["conn"])[0]["goals"]] == ["dismissed", "workers"]
    with pytest.raises(boss.BossError, match="no goal waiting"):
        boss.settle_goals(team["conn"], saved(team, p), "approve")


def test_a_goal_sent_back_closes_its_card_and_the_project_takes_more_goals(team):
    p = propose(team, draft=False)
    with pytest.raises(boss.BossError, match="say what"):
        boss.send_back(team["conn"], "team", p["id"], 1, "  ")
    g = boss.send_back(team["conn"], "team", p["id"], 1, "make it two goals")
    assert g["status"] == "sent_back" and g["note"] == "make it two goals"
    assert ledger.get_decision(team["conn"], g["decision_id"])["status"] == "dismissed"
    with pytest.raises(boss.BossError, match="is sent_back"):
        boss.send_back(team["conn"], "team", p["id"], 1, "again")
    text = boss.status(team["conn"], "team")
    assert f"sent back: project {p['id']} goal 1 - make it two goals" in text
    p2 = boss.propose(
        team["root"], team["conn"], "team", "", "",
        [{"goal": "Set VALUE"}, {"goal": "Then check", "after": [2]}], project=p["id"],
    )
    assert p2["id"] == p["id"] and p2["headline"] == "Make VALUE two."
    assert [g["n"] for g in p2["goals"]] == [1, 2, 3]
    assert [g["status"] for g in p2["goals"]] == ["sent_back", "proposed", "proposed"]
    assert boss.record_closed(team["conn"], "team") == []  # goals wait for Eli; still open
    with pytest.raises(boss.BossError, match="has no goal 9"):
        boss.goal_of(p2, 9)
    with pytest.raises(boss.BossError, match="after may only name earlier goals \\(1 to 3\\)"):
        boss.propose(team["root"], team["conn"], "team", "", "", [{"goal": "x", "after": [4]}], project=p["id"])


def test_a_project_closes_itself_when_no_goal_is_open(team):
    p = propose(team)
    approve(team, p)
    team["loop"].tick()
    gid = saved(team, p)["goals"][0]["goal_ids"][0]
    team["loop"].tick()
    assert saved(team, p)["status"] == "open"
    worker_commits(team, ledger.get_goal(team["conn"], gid))
    team["loop"].tick()  # grades, lands, and closes in the same tick
    s = saved(team, p)
    assert s["status"] == "closed" and s["closed"] == ledger._now()[:10]
    assert s["goals"][0]["status"] == "landed"
    assert next(x for x in ledger.list_plans(team["conn"], "team") if x["id"] == p["id"])["status"] == "done"
    assert boss.record_closed(team["conn"], "team") == []
    assert boss.flow(team["conn"])[0]["goals"][0]["station"] == "landed"
    with pytest.raises(boss.BossError, match="is closed"):
        boss.propose(team["root"], team["conn"], "team", "", "", [{"goal": "x"}], project=p["id"])

    # a project whose goals were all skipped was never built: dismissed, not closed
    p2 = propose(team, draft=False, headline="Never mind.")
    skip(team, p2)
    team["loop"].tick()
    assert saved(team, p2)["status"] == "dismissed"
    text = boss.status(team["conn"], "team")
    assert "closed: Make VALUE two., " in text and "Never mind." not in text


def test_a_stopped_goal_closes_the_project_with_its_state_kept(team):
    p = propose(team)
    approve(team, p)
    team["loop"].tick()
    gid = saved(team, p)["goals"][0]["goal_ids"][0]
    ledger.finish_goal(team["conn"], gid, "fail", "x")
    [closed] = boss.record_closed(team["conn"], "team")
    assert closed["status"] == "closed" and closed["goals"][0]["status"] == "stopped"
    assert boss.flow(team["conn"])[0]["goals"][0]["station"] == "stopped"


def test_a_goal_the_lead_cannot_plan_is_marked_and_the_rest_go_on(team):
    p = propose(team, draft=False, goals=[{"goal": "a"}, {"goal": "b"}])
    team["answers"].queue += [RuntimeError("You've hit your usage limit"), plan_answer()]
    boss.settle_goals(team["conn"], p, "approve")
    team["loop"].tick()
    team["loop"].tick()
    g1, g2 = saved(team, p)["goals"]
    assert g1["status"] == "failed" and "usage limit" in g1["error"]
    assert g2["status"] == "released"
    assert boss.card(saved(team, p))["goals"][0]["error"] == g1["error"]
    assert boss.flow(team["conn"])[0]["goals"][0]["station"] == "stopped"


def test_a_question_is_a_card_whose_answer_goes_back_to_the_boss(team):
    p = propose(team, draft=False)
    d = boss.ask(team["conn"], "team", "Which limit first?", ["calls", "tokens", "both"], "calls",
                 context="Calls is simpler.", project=p["id"])
    row = ledger.get_decision(team["conn"], d)
    assert row["source"] == f"boss-ask:{p['id']}" and boss.plan_of(row) == p["id"]
    assert row["options"] == ["calls", "tokens", "both"] and row["recommendation"] == "calls"
    loose = boss.ask(team["conn"], "team", "Ship today?", ["yes", "no"], "no")
    assert boss.plan_of(ledger.get_decision(team["conn"], loose)) is None
    text = boss.status(team["conn"], "team")
    assert "your questions waiting for Eli: 2" in text and "Which limit first?" in text
    assert "other open decisions: 0" in text
    for kw, says in [
        ({"options": ["one"]}, "2 to 4 distinct options"),
        ({"options": ["a", "a"]}, "distinct"),
        ({"recommendation": "zzz"}, "one of the options"),
        ({"question": ""}, "one sentence"),
    ]:
        args = {"question": "Q?", "options": ["a", "b"], "recommendation": "a"} | kw
        with pytest.raises(boss.BossError, match=says):
            boss.ask(team["conn"], "team", args["question"], args["options"], args["recommendation"])


def test_an_old_proposal_reads_as_a_project(team):
    old = {
        "id": 4, "target": "team", "headline": "Old.", "summary": "s", "status": "closed", "decision_id": 3,
        "goals": [{"goal": "a", "after": [], "goal_ids": [6]}, {"goal": "b", "after": [1], "goal_ids": [7]}],
        "goal_ids": [6, 7], "created": "2026-10-30T10:00:00+00:00", "closed": "2026-10-30",
    }
    boss._save(old)
    p = boss.load_proposal("team", 4)
    assert p["status"] == "closed" and [g["status"] for g in p["goals"]] == ["landed", "landed"]
    assert [g["n"] for g in p["goals"]] == [1, 2] and boss.card(p)["goal_ids"] == [6, 7]


def test_notes_are_capped(team):
    assert boss.read_notes("team") == ""
    assert boss.write_notes(team["root"], "team", "# Purpose\nShip it.") == len("# Purpose\nShip it.\n")
    assert boss.read_notes("team").startswith("# Purpose")
    with pytest.raises(boss.BossError, match="the cap is 6000"):
        boss.write_notes(team["root"], "team", "x" * 7000)


def test_status_lists_open_projects_and_what_waits(team):
    p1 = propose(team, draft=False, headline="Open one.")
    p2 = propose(team, draft=False, headline="Shut one.")
    p2 = saved(team, p2)
    p2["status"], p2["closed"] = "closed", "2026-10-01"
    boss._save(p2)
    text = boss.status(team["conn"], "team")
    assert "open projects: 1" in text and f"  project {p1['id']}: Open one. - 1 proposed" in text
    assert "goals waiting for Eli: 1" in text and f"  project {p1['id']} goal 1: Set VALUE to 2" in text
    assert "closed: Shut one., 2026-10-01" in text and text.count("Shut one.") == 1


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
        "team_propose", "team_ask", "team_proposals", "team_suggest",
        "team_harness_read", "team_propose_harness",
    }
    assert all(r["toolset"] == "team" for r in registered.values())
    assert not any("approve" in n or "release" in n for n in registered)
    handler = registered["team_notes_write"]["handler"]
    monkeypatch.setattr(tools, "_REPO", team["root"])
    assert handler({"text": "Purpose: test"}) == "saved, 14 characters"
    assert registered["team_notes_read"]["handler"]({}) == "Purpose: test\n"
    assert registered["team_status"]["handler"]({"target": "nope"}).startswith("refused:")
    out = registered["team_propose"]["handler"]({"headline": "H.", "summary": "S", "goals": [{"goal": "g1"}, {"goal": "g2"}]})
    assert out.startswith("project 1: goals 1, 2 are cards in Eli's inbox now.") and "project=1" in out
    out = registered["team_propose"]["handler"]({"project": 1, "goals": [{"goal": "g3", "after": [1]}]})
    assert out.startswith("project 1: goal 3 are cards")
    out = registered["team_ask"]["handler"]({"question": "Q?", "options": ["a", "b"], "recommendation": "b", "project": 1})
    assert out.startswith("question 4 is a card") and "End your turn" in out
    listing = registered["team_proposals"]["handler"]({})
    assert "project 1 open: H." in listing and "  goal 3 proposed: g3" in listing
    assert len(tools.schemas()) < 5000  # what the tools add to every boss call


def test_boss_profile_is_lean_and_locked(team):
    role = roles.load(team["root"], "boss", CATALOG)
    cfg = profiles.role_config(team["root"], role, CATALOG)
    assert cfg["model"] == {"provider": "team-subscription", "default": "claude-sonnet-5-5"}
    assert cfg["platform_toolsets"]["cli"] == ["clarify", "memory", "session_search", "team"]
    assert cfg["timeouts"]["tools"]["sequential_call"] == 3600
    assert cfg["compression"]["threshold"] == 0.25 and cfg["compression"]["enabled"] is True
    assert cfg["agent"]["max_turns"] == 20
    assert profiles.audit(cfg) == []


def test_the_aide_is_a_free_read_only_role_the_lead_never_assigns(team):
    shutil.copytree(REPO / "team" / "roles" / "aide", team["root"] / "team" / "roles" / "aide")
    toml = team["root"] / "team" / "roles" / "aide" / "role.toml"
    toml.write_text(toml.read_text().replace('"tier-medium"', '"tier-simple"'))  # the test catalog's medium tier is empty
    role = roles.load(team["root"], "aide", CATALOG)
    assert role["tools"] == ["team-read"] and role["assignable"] is False
    cfg = profiles.role_config(team["root"], role, CATALOG)
    assert cfg["model"]["provider"] == "litellm" and cfg["agent"]["max_turns"] == 6
    assert "team-read" in cfg["platform_toolsets"]["cli"] and "file" not in cfg["platform_toolsets"]["cli"]
    assert profiles.audit(cfg) == []
    from team import lead

    assert "aide" not in lead.menu(team["root"], CATALOG) and "implement" in lead.menu(team["root"], CATALOG)
    soul = (REPO / "team" / "roles" / "aide" / "SOUL.md").read_text()
    assert "HANDOFF:" in soul


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
    p = propose(team, draft=False, goals=[{"goal": "a"}, {"goal": "b"}])
    other = ledger.create_decision(team["conn"], "team", "Retry goal 9?", "ctx", ["yes", "no"], "yes")
    data = client.get("/api/plugins/team/inbox").json()
    assert [c["headline"] for c in data["plans"]] == ["Make VALUE two."]
    assert [d["id"] for d in data["decisions"]] == [other]  # goal cards are in the plan, not here
    assert client.post(f"/api/plugins/team/plans/{p['id']}/goals/2/skip").status_code == 200
    assert client.post(f"/api/plugins/team/plans/{p['id']}/approve").json()["goals"] == [1]
    assert client.post(f"/api/plugins/team/plans/{p['id']}/approve").status_code == 409
    assert ledger.get_decision(team["conn"], p["goals"][0]["decision_id"])["answer"] == "approve"
    assert ledger.get_decision(team["conn"], p["goals"][1]["decision_id"])["status"] == "dismissed"
    client.post(f"/api/plugins/team/decisions/{other}/answer", json={"answer": "no"})
    assert ledger.get_decision(team["conn"], other)["answer"] == "no"
    assert json.loads((REPO / "plugins" / "team" / "dashboard" / "manifest.json").read_text())["tab"]["path"] == "/team"


def test_flow_moves_each_goal_from_boss_to_landed(team):
    p = propose(team)
    [plan] = boss.flow(team["conn"])
    assert plan["goals"][0]["station"] == "boss"
    approve(team, p)
    assert boss.flow(team["conn"])[0]["goals"][0]["station"] == "boss"  # until the loop records the answer
    team["loop"].tick()
    goal = boss.flow(team["conn"])[0]["goals"][0]
    assert goal["station"] == "workers" and goal["parts"][0]["state"] == "attempt 1"
    worker_commits(team, ledger.get_goal(team["conn"], saved(team, p)["goals"][0]["goal_ids"][0]))
    team["loop"].tick()
    goal = boss.flow(team["conn"])[0]["goals"][0]
    assert goal["station"] == "landed" and goal["parts"][0]["state"] == "landed"


def test_a_later_goal_is_planned_knowing_what_the_earlier_ones_will_have_done(team):
    (team["repo"] / "lib.py").write_text("X = 0\n")
    git(team["repo"], "add", "-A")
    git(team["repo"], "commit", "-q", "-m", "lib")
    team["answers"].queue += [
        plan_answer(part(title="Add files to board", owned_paths=["app.py"], interfaces=["goal_card()['files']"])),
        plan_answer(part(title="Show the files", owned_paths=["lib.py"])),
        plan_answer(part(title="Unrelated", owned_paths=["app.py"])),
    ]
    p = propose(
        team, draft=False, headline="Files on cards.", summary="Board, then the page; a third alongside.",
        goals=[{"goal": "board"}, {"goal": "page", "after": [1]}, {"goal": "other"}],
    )
    boss.settle_goals(team["conn"], p, "approve")
    for _ in range(3):
        team["loop"].tick()
    first, second, third = team["answers"].prompts
    assert "Earlier goals in the same plan" not in first and "run at the same time" in first  # goals 2 and 3, by text
    assert "Do not plan any of it again" in second and "Add files to board" in second
    assert "goal_card()['files']" in second
    assert "run at the same time" in third and '"app.py"' in third and '"lib.py"' in third  # goals 1 and 2 both
    c = boss.card(saved(team, p))
    assert c["warnings"] == ["goals 1 and 3 run at the same time and both change app.py: they will conflict"]


def test_a_goal_that_changes_an_earlier_goals_file_is_flagged_for_a_look(team):
    team["answers"].queue += [plan_answer(), plan_answer()]
    p = propose(team, draft=False, goals=[{"goal": "a"}, {"goal": "b", "after": [1]}])
    boss.settle_goals(team["conn"], p, "approve")
    team["loop"].tick()
    team["loop"].tick()
    assert boss.card(saved(team, p))["warnings"] == [
        "goal 2 changes app.py again after goal 1: check it builds on that work instead of redoing it"
    ]
