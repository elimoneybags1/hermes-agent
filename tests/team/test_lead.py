"""The lead: plans validated in code, orders rendered with real excerpts, and the loop steered by it."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from team import grade, ledger, lead, run
from tests.team.test_catalog import CATALOG
from tests.team.test_run import REPO, git, world, worker_commits

MENU = {
    "implement": {
        "does": "Builds one task.",
        "toolsets": ["file", "terminal"],
        "skills": {"commit-before-done": ""},
        "models": ["tier-simple", "nvidia:z-ai/glm-5.3"],
    }
}


def part(key="p1", **over):
    p = {
        "key": key,
        "title": f"Part {key}",
        "role": "implement",
        "depends_on": [],
        "owned_paths": ["app.py"],
        "steps": ["Change VALUE to 2 in app.py", "Run ./check.sh", "Commit"],
        "context": [{"path": "app.py", "start": 1, "end": 1, "why": "the line to change"}],
        "interfaces": [],
        "pitfalls": ["Do not touch check.sh"],
        "toolsets": ["file", "terminal"],
        "skills": ["commit-before-done"],
        "model": "tier-simple",
    }
    p.update(over)
    return p


@pytest.fixture
def tree(tmp_path):
    (tmp_path / "app.py").write_text("VALUE = 1\n")
    (tmp_path / "lib.py").write_text("".join(f"line {n}\n" for n in range(1, 11)))
    return tmp_path


# --- validation -----------------------------------------------------------------


def test_a_good_plan_comes_back_in_dependency_order(tree):
    later = part("p2", owned_paths=["lib.py"], depends_on=["p1"])
    parts, errors = lead.validate_plan(
        {"summary": "s", "parts": [later, part()]}, tree, MENU, 6, CATALOG
    )
    assert errors == [] and [p["key"] for p in parts] == ["p1", "p2"]


def test_a_range_past_the_end_is_clipped_and_one_past_the_start_refused(tree):
    p = part(context=[{"path": "lib.py", "start": 8, "end": 99, "why": ""}])
    parts, errors = lead.validate_plan({"parts": [p]}, tree, MENU, 6, CATALOG)
    assert errors == [] and parts[0]["context"][0]["end"] == 10
    p = part(context=[{"path": "lib.py", "start": 11, "end": 12, "why": ""}])
    assert "not a range" in " ".join(lead.validate_plan({"parts": [p]}, tree, MENU, 6, CATALOG)[1])


@pytest.mark.parametrize(
    "over, says",
    [
        ({"toolsets": ["web"]}, "toolsets ['web'] not allowed"),
        ({"toolsets": []}, "toolsets [] not allowed"),
        ({"skills": ["fly"]}, "skills ['fly'] do not exist"),
        ({"model": "tier-claude"}, "model 'tier-claude' is not one of"),
        ({"role": "lead"}, "role 'lead' is not one of"),
        ({"steps": []}, "no steps"),
        ({"owned_paths": ["../etc/passwd"]}, "must be relative inside the repo"),
        ({"context": [{"path": "nope.py", "start": 1, "end": 2, "why": ""}]}, "does not exist"),
        ({"depends_on": ["ghost"]}, "unknown part 'ghost'"),
    ],
)
def test_refusals(tree, over, says):
    parts, errors = lead.validate_plan({"parts": [part(**over)]}, tree, MENU, 6, CATALOG)
    assert parts == [] and says in " ".join(errors)


def test_cycles_parallel_overlaps_and_too_many_parts_are_refused(tree):
    a, b = part("a", depends_on=["b"]), part("b", depends_on=["a"], owned_paths=["lib.py"])
    assert lead.validate_plan({"parts": [a, b]}, tree, MENU, 6, CATALOG)[1] == ["depends_on has a cycle"]
    a, b = part("a"), part("b")
    assert "can run in parallel but both own ['app.py']" in lead.validate_plan(
        {"parts": [a, b]}, tree, MENU, 6, CATALOG
    )[1][0]
    b = part("b", depends_on=["a"])  # one after the other may share a file
    assert lead.validate_plan({"parts": [a, b]}, tree, MENU, 6, CATALOG)[1] == []
    many = [part(f"p{n}", owned_paths=[f"f{n}.py"]) for n in range(3)]
    assert "3 parts; at most 2" in lead.validate_plan({"parts": many}, tree, MENU, 2, CATALOG)[1]


def test_diagnosis_validation(tree):
    found, errors = lead.validate_diagnosis(
        {"cause": "c", "note": "n", "action": "retry", "context": [{"path": "app.py", "start": 1, "end": 1}]},
        tree,
    )
    assert errors == [] and found["context"][0]["path"] == "app.py"
    assert lead.validate_diagnosis({"action": "ask", "cause": "c", "note": "n"}, tree)[1] == [
        "action ask needs a question for Eli"
    ]
    assert lead.validate_diagnosis({"action": "shrug"}, tree)[0] is None


# --- rendering --------------------------------------------------------------------


GOAL = {"goal": "Set VALUE to 2", "worktree": "/w", "branch": "team/x", "check_cmd": "./check.sh"}


def test_render_carries_the_order_and_the_code(tree):
    order = lead.validate_plan({"parts": [part()]}, tree, MENU, 6, CATALOG)[0][0]
    body = lead.render(order, GOAL, tree, 24000)
    assert "1. Change VALUE to 2 in app.py" in body and "- app.py" in body
    assert "### app.py:1-1 (the line to change)\n```\nVALUE = 1\n```" in body
    assert "Do not touch check.sh" in body and "kanban_request_review" in body
    assert "do not search the repo" in body
    assert "kanban_request_review" not in lead.render(order, GOAL, tree, 24000, claude=True)
    noted = lead.render(dict(order, lead_note="You forgot to commit."), GOAL, tree, 24000, failure="boom", attempt=2)
    assert "## From the lead, after the last attempt\nYou forgot to commit." in noted
    assert "## Attempt 1 failed. What it left\nboom" in noted


def test_render_keeps_excerpts_inside_the_budget(tree):
    refs = [{"path": "lib.py", "start": 1, "end": 10, "why": ""}, {"path": "app.py", "start": 1, "end": 1, "why": ""}]
    text, skipped = lead.excerpts(refs, tree, 60)
    assert skipped == 1 and "VALUE = 1" in text and "line 1" not in text
    order = dict(part(), context=refs)
    assert "1 more references did not fit" in lead.render(order, GOAL, tree, 60)


# --- the loop, with the lead --------------------------------------------------------


class Answers:
    """Queued lead answers for the fake `claude`: a dict is structured output, an Exception a failure."""

    def __init__(self, hermes):
        self.queue, self.prompts = [], []
        inner = hermes.claude

        def claude(cmd, kwargs):
            if "--json-schema" not in cmd:
                return inner(cmd, kwargs)
            self.prompts.append(cmd[2])
            answer = self.queue.pop(0)
            if isinstance(answer, Exception):
                out = {"is_error": True, "result": str(answer), "api_error_status": 429}
            else:
                out = {
                    "is_error": False,
                    "result": "",
                    "structured_output": answer,
                    "session_id": f"lead-{len(self.prompts)}",
                    "usage": {"input_tokens": 10, "cache_read_input_tokens": 990, "output_tokens": 100},
                }
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(out), stderr="")

        hermes.claude = claude


@pytest.fixture
def led(world):
    shutil.copytree(REPO / "team" / "roles" / "lead", world["root"] / "team" / "roles" / "lead")
    git(world["root"], "add", "-A")
    git(world["root"], "commit", "-q", "-m", "lead")
    world["answers"] = Answers(world["hermes"])
    return world


def plan(w, *parts):
    w["answers"].queue.append({"summary": "the plan", "parts": list(parts)})
    return run.plan_goal(w["root"], w["conn"], "team", "Set VALUE to 2", runner=w["hermes"], cat=CATALOG)


def create_args(hermes, task_id_index=0):
    creates = [c for c in hermes.calls if c[1:2] == ["kanban"] and "create" in c]
    return creates[task_id_index]


def test_plan_goal_dispatches_the_order(led):
    goals = plan(led, part())
    assert len(goals) == 1
    goal = goals[0]
    task = led["hermes"].tasks[goal["current_task"]]
    assert "### app.py:1-1" in task["body"] and "VALUE = 1" in task["body"]
    assert task["model"] == "tier-simple" and task["assignee"] == "implement"
    args = create_args(led["hermes"])
    assert args.count("--toolset") == 2 and "terminal" in args and "--skill" in args
    job = ledger.get_job(led["conn"], goal["job_id"])
    order = ledger.latest_order(led["conn"], goal["id"])
    assert job["order_id"] == order["id"] and order["order"]["goal"] == "Set VALUE to 2"
    lead_job = ledger.list_jobs(led["conn"], role="lead")[0]
    assert lead_job["outcome"] == "pass" and lead_job["task_ref"] == "claude:lead-1"
    assert ledger.calls_for(led["conn"], job_id=lead_job["id"])[0]["tokens_in"] == 1000
    assert ledger.get_part(led["conn"], goal["part_id"])["owned_paths"] == ["app.py"]
    assert not list((ledger.state_dir() / "worktrees" / "team" / "_lead").iterdir())


def test_a_refused_plan_is_sent_back_once_with_the_reasons(led):
    led["answers"].queue.append({"summary": "bad", "parts": [part(toolsets=["web"])]})
    goals = plan(led, part())
    assert len(goals) == 1 and len(led["answers"].prompts) == 2
    assert "Team refused your previous plan" in led["answers"].prompts[1]
    assert "toolsets ['web'] not allowed" in led["answers"].prompts[1]


def test_two_refusals_or_a_usage_limit_stop_the_goal(led):
    led["answers"].queue += [{"parts": []}, {"parts": []}]
    with pytest.raises(run.RunError, match="refused the lead's answer twice"):
        run.plan_goal(led["root"], led["conn"], "team", "x", runner=led["hermes"], cat=CATALOG)
    led["answers"].queue.append(RuntimeError("You've hit your usage limit"))
    with pytest.raises(run.RunError, match="usage limit"):
        run.plan_goal(led["root"], led["conn"], "team", "x", runner=led["hermes"], cat=CATALOG)
    assert [j["outcome"] for j in ledger.list_jobs(led["conn"], role="lead")] == ["error", "error"]
    assert ledger.list_goals(led["conn"], all_goals=True) == []


def test_a_dependent_part_starts_from_the_landed_base(led):
    (led["repo"] / "lib.py").write_text("FROM = 0\n")
    git(led["repo"], "add", "-A")
    git(led["repo"], "commit", "-q", "-m", "lib")
    first, second = plan(led, part(), part("p2", owned_paths=["lib.py"], depends_on=["p1"]))
    assert second["current_task"] is None and not Path(second["worktree"]).exists()
    led["loop"].tick()
    assert ledger.get_goal(led["conn"], second["id"])["current_task"] is None
    worker_commits(led, ledger.get_goal(led["conn"], first["id"]))
    led["loop"].tick()
    assert ledger.get_goal(led["conn"], first["id"])["outcome"] == "pass"
    second = ledger.get_goal(led["conn"], second["id"])
    assert second["current_task"] and (Path(second["worktree"]) / "app.py").read_text().startswith("VALUE = 2")


def test_a_dependent_part_fails_when_its_dependency_did(led):
    first, second = plan(led, part(), part("p2", owned_paths=["lib.py"], depends_on=["p1"]))
    ledger.finish_goal(led["conn"], first["id"], "fail", "gave up")
    led["loop"].tick()
    g = ledger.get_goal(led["conn"], second["id"])
    assert g["outcome"] == "fail" and "did not land: gave up" in g["detail"]


def test_a_failed_check_gets_the_leads_note_before_the_retry(led):
    goal = plan(led, part())[0]
    worker_commits(led, goal, text="VALUE = 3\n")
    led["answers"].queue.append(
        {
            "cause": "Set 3, not 2.",
            "note": "Use 2.",
            "action": "retry",
            "steps": ["Write VALUE = 2"],
            "context": [{"path": "app.py", "start": 1, "end": 1, "why": "now"}],
        }
    )
    led["loop"].tick()
    g = ledger.get_goal(led["conn"], goal["id"])
    assert g["attempt"] == 2 and g["outcome"] is None
    comments = led["hermes"].comments[goal["current_task"]]
    assert comments[-1].startswith("LEAD: Set 3, not 2.") and "1. Write VALUE = 2" in comments[-1]
    assert "VALUE = 3" in comments[-1]  # the excerpt is read from the worker's branch
    order = ledger.latest_order(led["conn"], goal["id"])
    assert order["version"] == 2 and order["order"]["lead_note"] == "Use 2."
    assert ledger.list_diagnoses(led["conn"], goal["id"])[0]["action"] == "retry"
    assert "VALUE = 3" in led["answers"].prompts[-1]  # the lead saw the diff


def test_the_lead_can_send_a_part_straight_to_claude(led):
    goal = plan(led, part())[0]
    worker_commits(led, goal, text="VALUE = 3\n")
    led["answers"].queue.append({"cause": "too hard", "note": "", "action": "escalate"})

    def fix(wt, env):
        (wt / "app.py").write_text("VALUE = 2\n")
        git(wt, "commit", "-qam", "claude fixed it")

    led["hermes"].claude_behaviour = fix
    led["loop"].tick()
    g = ledger.get_goal(led["conn"], goal["id"])
    assert g["outcome"] == "pass" and g["attempt"] == 2


def test_no_diagnosis_still_retries(led):
    goal = plan(led, part())[0]
    worker_commits(led, goal, text="VALUE = 3\n")
    led["answers"].queue.append(RuntimeError("You've hit your usage limit"))
    led["loop"].tick()
    g = ledger.get_goal(led["conn"], goal["id"])
    assert g["attempt"] == 2 and ledger.list_diagnoses(led["conn"], goal["id"]) == []


def test_menu_lists_only_active_hermes_roles(led):
    m = lead.menu(led["root"], CATALOG)
    assert list(m) == ["implement"] and m["implement"]["toolsets"] == ["file", "terminal"]


def test_lead_has_no_hermes_profile(led):
    from team import profiles

    assert profiles.sync(led["root"], "lead", CATALOG, runner=led["hermes"]) == []
    assert not profiles.profile_dir("lead").exists()


def test_the_lead_reads_a_detached_clean_checkout(led):
    with lead.workspace(led["repo"], "team", "probe", "main") as tree:
        assert (tree / "app.py").read_text() == "VALUE = 1\n"
        assert grade.is_clean(tree)
    assert not tree.exists()
