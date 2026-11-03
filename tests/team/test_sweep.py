"""One-sweep attempts: the packet, the answer contract, and the loop running them."""

import json
import threading

import pytest

from team import grade, ledger, run, sweep
from tests.team.test_catalog import CATALOG
from tests.team.test_lead import led, part, plan  # fixtures
from tests.team.test_run import git, worker_commits, world  # fixtures

GOAL = {"goal": "Set VALUE to 2", "check_cmd": "./check.sh", "acceptance_cmd": None}


@pytest.fixture
def tree(tmp_path):
    (tmp_path / "app.py").write_text("VALUE = 1\n")
    (tmp_path / "lib.py").write_text("".join(f"line {n}\n" for n in range(1, 11)))
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "a.py").write_text("A = 1\n")
    (tmp_path / "pkg" / "b.py").write_text("B = 1\n")
    return tmp_path


# --- the packet ---------------------------------------------------------------------


def test_packet_carries_the_order_the_owned_files_and_the_cited_code(tree):
    order = part(owned_paths=["app.py", "pkg", "new.py"], context=[
        {"path": "app.py", "start": 1, "end": 1, "why": "owned, so not repeated"},
        {"path": "lib.py", "start": 2, "end": 3, "why": "the helper"},
    ])
    text = sweep.packet(order, GOAL, tree, order["owned_paths"], 20000, attempt=2, failure="attempt 1 broke it")
    assert "## Steps\n1. Change VALUE to 2" in text and "Do not touch check.sh" in text
    assert "### app.py (1 lines)\n```\nVALUE = 1\n```" in text
    assert "### pkg/a.py" in text and "### pkg/b.py" in text
    assert "### new.py\n(does not exist yet" in text
    assert "### lib.py:2-3 (the helper)\n```\nline 2\nline 3\n```" in text
    assert text.count("VALUE = 1") == 1  # an owned file is shown once, not again as an excerpt
    assert "## Attempt 1 failed. What it left\nattempt 1 broke it" in text
    assert "`./check.sh`" in text and "What the reviewer checks" in text


def test_packet_keeps_owned_files_inside_the_budget_and_shows_the_call_failure(tree):
    (tree / "big.py").write_text("x = 1\n" * 2000)
    order = part(owned_paths=["app.py", "big.py"])
    text = sweep.packet(order, GOAL, tree, order["owned_paths"], 2000, call=2, call_failure="FAILED: grep found nothing")
    assert "### app.py" in text and "### big.py" not in text
    assert "1 owned files were too large for this packet: big.py" in text
    assert "Call 1 of this attempt: Team applied your answer and the check failed" in text
    assert "FAILED: grep found nothing" in text


def test_instructions_carry_the_soul_the_skills_and_the_answer_rules(tmp_path):
    (tmp_path / "s").mkdir()
    (tmp_path / "s" / "SKILL.md").write_text("---\nname: s\n---\nCommit first.\n")
    role = {"soul": "# implement\nBuild it.", "skills": ["s"], "skill_dirs": {"s": tmp_path / "s"}}
    text = sweep.instructions(role)
    assert text.startswith("# implement\nBuild it.") and "Commit first." in text
    assert "You have no tools" in text and '"edits"' in text
    (tmp_path / "SOUL.md").write_text("from the branch\n")
    (tmp_path / "skills" / "t").mkdir(parents=True)
    (tmp_path / "skills" / "t" / "SKILL.md").write_text("branch skill\n")
    arm = sweep.role_from_profile("implement", tmp_path)
    assert arm["soul"] == "from the branch\n" and arm["skills"] == ["t"]
    assert "branch skill" in sweep.instructions(arm)


# --- the answer ---------------------------------------------------------------------


def test_plan_changes_applies_files_edits_and_deletes_in_order(tree):
    answer = json.dumps({
        "summary": "done",
        "files": {"pkg/c.py": "C = 1\n"},
        "edits": [{"path": "app.py", "find": "VALUE = 1", "replace": "VALUE = 2"},
                  {"path": "pkg/c.py", "find": "C = 1", "replace": "C = 2"}],
        "delete": ["pkg/b.py"],
    })
    changes, refused, cannot = sweep.plan_changes(answer, ["app.py", "pkg"], tree)
    assert refused == [] and cannot is None
    assert changes == {"pkg/c.py": "C = 2\n", "app.py": "VALUE = 2\n", "pkg/b.py": None}
    assert sorted(sweep.apply(tree, changes)) == ["app.py", "pkg/b.py", "pkg/c.py"]
    assert (tree / "app.py").read_text() == "VALUE = 2\n" and not (tree / "pkg" / "b.py").exists()
    assert (tree / "pkg" / "c.py").read_text() == "C = 2\n"


@pytest.mark.parametrize(
    "answer, says",
    [
        ("I would change VALUE to 2.", "not a JSON object"),
        ({"files": {"lib.py": "x"}}, "not a file you own"),
        ({"files": {"../etc/passwd": "x"}}, "must be relative"),
        ({"edits": [{"path": "app.py", "find": "nope", "replace": "x"}]}, "occurs 0 times"),
        ({"edits": [{"path": "lib.py", "find": "line", "replace": "x"}]}, "not a file you own"),
        ({"edits": [{"path": "app.py", "find": "", "replace": "x"}]}, "find must be non-empty"),
        ({"delete": ["pkg/zzz.py"]}, "does not exist"),
        ({"files": {"app.py": "VALUE = 1\n"}}, "changes nothing"),
        ({"summary": "nothing to do"}, "changes nothing"),
    ],
)
def test_plan_changes_refusals(tree, answer, says):
    text = answer if isinstance(answer, str) else json.dumps(answer)
    changes, refused, cannot = sweep.plan_changes(text, ["app.py", "pkg"], tree)
    assert changes is None and cannot is None and any(says in r for r in refused), refused


def test_plan_changes_reads_a_fenced_answer_and_a_cannot(tree):
    fenced = '```json\n{"files": {"app.py": "VALUE = 2\\n"}}\n```'
    changes, refused, cannot = sweep.plan_changes(fenced, ["app.py"], tree)
    assert changes == {"app.py": "VALUE = 2\n"} and not refused
    changes, refused, cannot = sweep.plan_changes(json.dumps({"cannot": "check.sh must change too"}), ["app.py"], tree)
    assert changes is None and refused == [] and cannot == "check.sh must change too"


# --- the loop -----------------------------------------------------------------------


class Model:
    """The fake gateway: queued answers (a dict is JSON, a str is raw text, an Exception a
    gateway failure, a list is one answer per parallel sample)."""

    def __init__(self):
        self.queue, self.prompts, self.systems, self.specs, self.sessions = [], [], [], [], []
        self.lock = threading.Lock()
        self.taken: set[int] = set()

    def __call__(self, spec, system, prompt, *, max_tokens, session):
        with self.lock:
            self.specs.append(spec)
            self.systems.append(system)
            self.prompts.append(prompt)
            self.sessions.append(session)
            answer = self.queue[0]
            if isinstance(answer, list):  # one answer per parallel sample, by its index
                i = int(session.rsplit("-", 1)[1])
                answer, self.taken = answer[i], self.taken | {i}
                if len(self.taken) == len(self.queue[0]):
                    self.queue.pop(0)
                    self.taken = set()
            else:
                self.queue.pop(0)
        if isinstance(answer, Exception):
            raise answer
        text = answer if isinstance(answer, str) else json.dumps(answer)
        return {"text": text, "usage": {"prompt_tokens": 1200, "completion_tokens": 40}, "model": "nemotron-3.5"}


def settings_toml(root, text):
    (root / "team" / "orchestration.toml").write_text(text)
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "settings")


@pytest.fixture
def swept(led):
    settings_toml(led["root"], "[sweep]\nenabled = true\n")
    led["model"] = Model()
    led["loop"].complete = led["model"]
    return led


def sweep_jobs(w):
    return [j for j in ledger.list_jobs(w["conn"]) if (j["task_ref"] or "").startswith("sweep:")]


def test_a_sweep_attempt_is_queued_by_the_plan_and_run_by_the_tick(swept):
    goal = plan(swept, part())[0]
    assert goal["current_task"] is None and goal["detail"].startswith("sweep attempt queued")
    [job] = sweep_jobs(swept)
    assert job["task_ref"] == f"sweep:{goal['id']}-1" and job["ended"] is None
    assert job["model_choice"]["spec"] == "tier-simple"
    assert not any(c[1:2] == ["kanban"] and "create" in c for c in swept["hermes"].calls)

    swept["model"].queue.append({"summary": "set VALUE to 2", "files": {"app.py": "VALUE = 2\n"}})
    swept["loop"].tick()
    g = ledger.get_goal(swept["conn"], goal["id"])
    assert g["outcome"] == "pass" and g["detail"].startswith("landed ")
    assert (swept["repo"] / "app.py").read_text() == "VALUE = 2\n"
    [job] = sweep_jobs(swept)
    assert job["outcome"] == "pass" and job["grader"] == "check"
    [call] = ledger.calls_for(swept["conn"], job_id=job["id"])
    assert call["tokens_in"] == 1200 and call["profile"] == "implement" and call["goal_id"] == goal["id"]
    assert call["response_model"] == "nemotron-3.5" and call["task_ref"] == job["task_ref"]
    assert swept["model"].specs == ["tier-simple"]
    prompt, system = swept["model"].prompts[0], swept["model"].systems[0]
    assert "## Steps" in prompt and "### app.py (1 lines)\n```\nVALUE = 1\n```" in prompt
    assert system.startswith("# implement") and "Do it." in system and "You have no tools" in system
    log = git(swept["repo"], "log", "--format=%s", "main").stdout
    assert "team sweep 1.1: set VALUE to 2" in log


def test_the_second_call_sees_the_check_output_and_the_files_as_left(swept):
    goal = plan(swept, part())[0]
    swept["model"].queue += [
        {"files": {"app.py": "VALUE = 3\n"}},
        {"edits": [{"path": "app.py", "find": "VALUE = 3", "replace": "VALUE = 2"}]},
    ]
    swept["loop"].tick()
    g = ledger.get_goal(swept["conn"], goal["id"])
    assert g["outcome"] == "pass"
    second = swept["model"].prompts[1]
    assert "Call 1 of this attempt: Team applied your answer and the check failed" in second
    assert "### app.py (1 lines)\n```\nVALUE = 3\n```" in second
    [job] = sweep_jobs(swept)
    assert len(ledger.calls_for(swept["conn"], job_id=job["id"])) == 2
    log = git(swept["repo"], "log", "--format=%s", "main").stdout
    assert "team sweep 1.1" in log and "team sweep 1.2" in log


def test_parallel_samples_are_tried_in_turn_from_the_same_start(swept):
    settings_toml(swept["root"], "[sweep]\nenabled = true\nsamples = 2\n")
    goal = plan(swept, part())[0]
    swept["model"].queue.append([{"files": {"app.py": "VALUE = 3\n"}}, {"files": {"app.py": "VALUE = 2\n"}}])
    swept["loop"].tick()
    g = ledger.get_goal(swept["conn"], goal["id"])
    assert g["outcome"] == "pass"
    assert sorted(swept["model"].sessions) == [f"sweep-{goal['id']}-1-1-0", f"sweep-{goal['id']}-1-1-1"]
    [job] = sweep_jobs(swept)
    assert len(ledger.calls_for(swept["conn"], job_id=job["id"])) == 2
    log = git(swept["repo"], "log", "--format=%s", "main").stdout
    assert log.count("team sweep") == 1  # the failed sample was reset away before the next


def test_exhausted_calls_fail_the_attempt_then_the_lead_may_switch_to_turns(swept):
    goal = plan(swept, part())[0]
    swept["model"].queue += [
        "I think VALUE should be 2.",
        {"files": {"lib.py": "x\n"}},
        {"files": {"app.py": "VALUE = 5\n"}},
    ]
    swept["answers"].queue.append({
        "cause": "it needs to run the check to see", "cause_tag": "model", "note": "Run ./check.sh yourself.",
        "action": "retry", "mode": "turns",
    })
    swept["loop"].tick()
    [job] = sweep_jobs(swept)
    assert job["outcome"] == "fail" and job["grader"] == "check" and job["failure_kind"] == "work"
    assert len(ledger.calls_for(swept["conn"], job_id=job["id"])) == 3
    assert "VALUE = 5" in swept["answers"].prompts[-1]  # the lead saw the sweep's diff
    g = ledger.get_goal(swept["conn"], goal["id"])
    assert g["attempt"] == 2 and g["current_task"] == "t_1"
    task = swept["hermes"].tasks["t_1"]
    assert task["model"] == "z-ai/glm-5.3" and "## From the lead, after the last attempt\nRun ./check.sh yourself." in task["body"]
    assert "Attempt 1 failed. What it left" in task["body"] and "VALUE = 5" in task["body"]
    assert ledger.latest_order(swept["conn"], goal["id"])["order"]["mode"] == "turns"
    worker_commits(swept, g)
    swept["loop"].tick()
    assert ledger.get_goal(swept["conn"], goal["id"])["outcome"] == "pass"


def test_a_cannot_answer_fails_the_attempt_at_once(swept):
    goal = plan(swept, part())[0]
    swept["model"].queue.append({"cannot": "check.sh must change as well"})
    swept["answers"].queue.append({"cause": "scope", "cause_tag": "scope", "note": "Own check.sh too.", "action": "retry"})
    swept["loop"].tick()
    first, second = sweep_jobs(swept)
    assert second["outcome"] == "fail" and "cannot be done in its files: check.sh must change as well" in second["grader_detail"]
    assert len(swept["model"].prompts) == 1
    assert first["ended"] is None and first["attempt"] == 2  # the next sweep is queued, with the lead's note
    g = ledger.get_goal(swept["conn"], goal["id"])
    assert g["attempt"] == 2 and g["outcome"] is None
    assert ledger.latest_order(swept["conn"], goal["id"])["order"]["lead_note"] == "Own check.sh too."


def test_a_gateway_failure_is_an_infra_death_not_a_work_failure(swept):
    from team.review.backend import ScoutError

    goal = plan(swept, part())[0]
    swept["model"].queue += [ScoutError("scout gateway returned HTTP 429")] * 3
    swept["answers"].queue.append({"cause": "quota", "cause_tag": "env", "note": "", "action": "retry"})
    swept["loop"].tick()
    first, second = sweep_jobs(swept)
    assert second["outcome"] == "fail" and second["grader"] == "sweep" and second["failure_kind"] == "infra"
    calls = ledger.calls_for(swept["conn"], job_id=second["id"])
    assert len(calls) == 3 and not any(c["ok"] for c in calls) and "HTTP 429" in calls[0]["error"]
    assert first["ended"] is None and ledger.get_goal(swept["conn"], goal["id"])["attempt"] == 2


def test_the_lead_orders_turns_mode_and_gets_a_kanban_worker(swept):
    goal = plan(swept, part(mode="turns"))[0]
    assert goal["current_task"] == "t_1" and sweep_jobs(swept) == []
    assert ledger.latest_order(swept["conn"], goal["id"])["order"]["mode"] == "turns"


def test_over_budget_stops_the_goal_and_asks_eli(swept):
    settings_toml(swept["root"], "[sweep]\nenabled = true\n[worker]\nmax_calls_per_goal = 2\n[lead]\ndiagnose = false\n")
    goal = plan(swept, part())[0]
    swept["model"].queue += ["prose"] * 3
    swept["loop"].tick()
    swept["loop"].tick()
    g = ledger.get_goal(swept["conn"], goal["id"])
    assert g["outcome"] == "fail" and "over its budget after 3 worker calls (limit 2)" in g["detail"]
    [decision] = ledger.list_decisions(swept["conn"], "team")
    assert decision["goal_id"] == goal["id"] and "over its budget" in decision["question"]
    assert grade.branch_exists(swept["repo"], goal["branch"])


def test_goal_usage_counts_worker_calls_only(swept):
    goal = plan(swept, part())[0]
    swept["model"].queue.append({"files": {"app.py": "VALUE = 2\n"}})
    swept["loop"].tick()
    used = ledger.goal_usage(swept["conn"], goal["id"])
    assert used == {"calls": 1, "tokens_in": 1200}  # the lead's planning call is not a worker's


def test_sweep_needs_owned_files_a_plain_goal_keeps_the_kanban_worker(swept):
    goal = run.start_goal(swept["root"], swept["conn"], "team", "Set VALUE to 2", runner=swept["hermes"], cat=CATALOG)
    assert goal["current_task"] == "t_1" and sweep_jobs(swept) == []


def test_a_sweep_in_flight_survives_a_restart(swept):
    """The job stays open until a tick runs it; a loop that died mid-sweep runs it again."""
    goal = plan(swept, part())[0]
    swept["model"].queue.append(RuntimeError("the loop died here"))
    swept["loop"].tick()  # the tick logs the goal's error and leaves the job open
    [job] = sweep_jobs(swept)
    assert job["ended"] is None
    swept["model"].queue.append({"files": {"app.py": "VALUE = 2\n"}})
    swept["loop"].tick()
    assert ledger.get_goal(swept["conn"], goal["id"])["outcome"] == "pass"


def test_backend_complete_sends_system_max_tokens_and_session(monkeypatch):
    from team.review import backend

    seen = {}

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps({"choices": [{"message": {"content": "{}"}}], "usage": {}, "model": "m"}).encode()

    def urlopen(req, timeout):
        seen["body"] = json.loads(req.data)
        seen["headers"] = dict(req.header_items())
        seen["timeout"] = timeout
        return Resp()

    monkeypatch.setattr(backend.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(backend.catalog, "load", lambda: {**CATALOG, "litellm": {"host": "127.0.0.1", "port": 4000}})
    monkeypatch.setenv("LITELLM_MASTER_KEY", "k")
    out = backend.complete("tier-simple", "do it", system="rules", max_tokens=16000, session="sweep-1-1-1-0")
    assert out["text"] == "{}" and seen["body"]["max_tokens"] == 16000 and seen["body"]["stream"] is False
    assert seen["body"]["messages"][0] == {"role": "system", "content": "rules"}
    assert seen["headers"]["X-litellm-session-id"] == "sweep-1-1-1-0" and seen["timeout"] == 600
