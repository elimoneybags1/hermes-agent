"""Harness changes: the boss rewrites a worker role's file, the replay set grades it, Eli adopts."""

import subprocess

import pytest

from team import boss, grade, ledger, profiles, replay
from tests.team.test_boss import propose, team
from tests.team.test_catalog import CATALOG
from tests.team.test_run import worker_commits, world

SOUL = "team/roles/implement/SOUL.md"


def landed_order(w) -> int:
    """One goal planned by the lead, built by the worker, landed: the first replay item."""
    p = propose(w)
    boss.settle(w["conn"], p["decision_id"], "approve")
    w["loop"].tick()
    gid = boss.load_proposal("team", p["id"])["goal_ids"][0]
    worker_commits(w, ledger.get_goal(w["conn"], gid))
    w["loop"].tick()
    assert ledger.get_goal(w["conn"], gid)["outcome"] == "pass"
    return gid


def edit(text="# implement\n\nRead ranges only.\n", path=SOUL):
    return [{"path": path, "content": text}]


def harness(w, **kw):
    return boss.propose_harness(
        w["root"],
        w["conn"],
        "team",
        kw.pop("headline", "Tell implementers to read ranges only."),
        kw.pop("why", "goal 15 read 49k chars whole; 11/11 passed, tokens are the lever"),
        kw.pop("edits", edit()),
        runner=w["hermes"],
        cat=CATALOG,
        log=lambda m: None,
    )


def show(repo, ref):
    return subprocess.run(
        ["git", "-C", str(repo), "show", ref], capture_output=True, text=True, check=False
    ).stdout


def replay_passes(w, review_id):
    for g in ledger.harness_goals(w["conn"], review_id):
        worker_commits(w, g)
    w["loop"].tick()  # grades every arm and drops its branch
    w["loop"].tick()  # the finished replay becomes a decision


def test_a_harness_change_needs_a_worker_role_file_and_finished_orders(team):
    with pytest.raises(boss.BossError, match="SOUL.md, role.toml"):
        harness(team, edits=edit("x", "team/run.py"))
    with pytest.raises(boss.BossError, match="its own files"):
        harness(team, edits=edit("x", "team/roles/boss/SOUL.md"))
    with pytest.raises(boss.BossError, match="Hermes worker"):
        harness(team, edits=edit("x", "team/roles/lead/SOUL.md"))
    with pytest.raises(boss.BossError, match="no finished worker orders"):
        harness(team)
    assert ledger.list_harness(team["conn"]) == []
    assert replay.load_set("team") is None


def test_a_harness_change_replays_the_set_on_both_arms(team):
    gid = landed_order(team)
    r = harness(team)
    assert r["arms"] == ["branch", "base"] and r["items"] == 1
    review = ledger.get_harness(team["conn"], r["id"])
    assert review["status"] == "evaluating" and review["branch"] == f"team/harness-{r['id']}"
    assert review["roles"] == ["implement"]
    # the edit is on the branch, not on main
    assert "Read ranges only" in show(team["root"], f"{review['branch']}:{SOUL}")
    assert "Read ranges only" not in show(team["root"], f"main:{SOUL}")
    # the branch arm runs on a profile synced from the branch, the base arm on the live one
    cand = profiles.profile_dir(profiles.candidate_profile("implement", r["id"]))
    assert (cand / "SOUL.md").read_text().startswith("# implement\n\nRead ranges only")
    goals = ledger.harness_goals(team["conn"], r["id"])
    assert [g["arm"] for g in goals] == ["branch", "base"]
    tasks = team["hermes"].tasks
    assert {g["arm"]: tasks[g["current_task"]]["assignee"] for g in goals} == {
        "branch": f"implement-h{r['id']}",
        "base": "implement",
    }
    # every replay goal is the frozen order again, from the commit it was written against
    s = replay.load_set("team")
    assert [i["goal_id"] for i in s["items"]] == [gid]
    assert all(g["base"] == s["items"][0]["commit"] for g in goals)
    assert all("Change VALUE to 2" in tasks[g["current_task"]]["body"] for g in goals)
    assert "harness changes: 1 being replayed, 0 waiting for Eli" in boss.status(team["conn"], "team")


def test_replay_results_go_to_eli_and_adopt_merges_the_branch(team):
    landed_order(team)
    r = harness(team)
    replay_passes(team, r["id"])
    goals = ledger.harness_goals(team["conn"], r["id"])
    assert [g["outcome"] for g in goals] == ["pass", "pass"]
    assert all(g["detail"].startswith(f"replay {g['arm']}") for g in goals)
    assert not any(grade.branch_exists(team["repo"], g["branch"]) for g in goals)
    review = ledger.get_harness(team["conn"], r["id"])
    assert review["status"] == "waiting"
    d = ledger.get_decision(team["conn"], review["decision_id"])
    assert d["options"] == ["adopt", "dismiss"]
    assert "passed 1/1 (base 1/1)" in d["context"] and "tokens are the lever" in d["context"]
    assert d["recommendation"] == "dismiss"  # a tie is not a win
    assert replay.load_set("team")["base"]["pass"] == 1
    assert "1 waiting for Eli" in boss.status(team["conn"], "team")

    boss.settle(team["conn"], d["id"], "adopt")
    team["loop"].tick()
    review = ledger.get_harness(team["conn"], r["id"])
    assert review["status"] == "adopted" and review["merge_sha"]
    assert "Read ranges only" in (team["root"] / SOUL).read_text()
    assert "Read ranges only" in (profiles.profile_dir("implement") / "SOUL.md").read_text()
    assert not profiles.profile_dir(profiles.candidate_profile("implement", r["id"])).exists()
    assert not grade.branch_exists(team["root"], review["branch"])
    assert "harness changes" not in boss.status(team["conn"], "team")

    # the adopted version is the new base: a second change replays the branch arm only
    r2 = harness(team, edits=edit("# implement\n\nRead ranges only. Commit.\n"))
    assert r2["arms"] == ["branch"]


def test_a_dismissed_harness_change_keeps_its_branch(team):
    landed_order(team)
    r = harness(team)
    replay_passes(team, r["id"])
    review = ledger.get_harness(team["conn"], r["id"])
    boss.settle(team["conn"], review["decision_id"], None)
    team["loop"].tick()
    review = ledger.get_harness(team["conn"], r["id"])
    assert review["status"] == "dismissed" and review["merge_sha"] is None
    assert grade.branch_exists(team["root"], review["branch"])
    assert "Read ranges only" not in show(team["root"], f"main:{SOUL}")
    assert not profiles.profile_dir(profiles.candidate_profile("implement", r["id"])).exists()


def test_a_bad_role_file_never_reaches_the_board(team):
    landed_order(team)
    with pytest.raises(boss.BossError, match="could not start"):
        harness(team, edits=edit("this is not toml = [", "team/roles/implement/role.toml"))
    review = ledger.list_harness(team["conn"])[0]
    assert review["status"] == "failed" and ledger.harness_goals(team["conn"], review["id"]) == []
    assert not grade.branch_exists(team["root"], f"team/harness-{review['id']}")
