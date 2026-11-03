"""The loop, against real git and /bin/sh, with a fake kanban and a fake Claude."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from team import grade, ledger, profiles, run, targets
from tests.team.test_catalog import CATALOG
from tests.team.test_roles import ROLE, write_role

REPO = Path(__file__).resolve().parents[2]


def git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    )


class FakeHermes:
    """Answers `team-hermes kanban ...` from an in-memory board; runs everything else for real.

    The `claude` command is faked too: `claude_behaviour` decides what it does.
    """

    def __init__(self):
        self.tasks: dict[str, dict] = {}
        self.comments: dict[str, list[str]] = {}
        self.runs: dict[str, list[dict]] = {}
        self.events: dict[str, list[dict]] = {}
        self.boards = ["team"]
        self.logs: dict[str, str] = {}
        self.calls: list[list[str]] = []
        self.summaries: dict[str, str] = {}  # what a worker handed to kanban_request_review
        self.claude_behaviour = None

    def __call__(self, cmd, **kwargs):
        self.calls.append(list(cmd))
        if cmd[0].endswith("team-hermes") and cmd[1] == "kanban":
            return self.kanban(cmd[2:])
        if cmd[0].endswith("team-hermes") and cmd[1:3] == ["profile", "create"]:
            profiles.profile_dir(cmd[3]).mkdir(parents=True, exist_ok=True)
            return subprocess.CompletedProcess(cmd, 0, stdout="created", stderr="")
        if cmd[1:4] == ["-P", "-m", "team.hermes_run"]:
            request = json.loads(cmd[4])
            kwargs["env"]["TEAM_CHECK"] = request.get("check") or ""
            fake = ["gateway-worker", "-p", request["brief"]]
            if request.get("schema"):
                fake += ["--json-schema", json.dumps(request["schema"])]
            return self.claude(fake, kwargs)
        if cmd[0] == "claude":
            return self.claude(cmd, kwargs)
        return subprocess.run(cmd, **kwargs)  # noqa: PLW1510 - the caller decides

    # -- kanban ----------------------------------------------------------------

    def _ok(self, cmd, payload=None):
        out = "\x1b[0;32m✓\x1b[0m Tools installed\n" + (
            json.dumps(payload) if payload is not None else "ok"
        )
        return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")

    def kanban(self, args):
        if args[:2] == ["boards", "list"]:
            return self._ok(args, [{"slug": b} for b in self.boards])
        if args[:2] == ["boards", "create"]:
            self.boards.append(args[2])
            return self._ok(args)
        verb, rest = args[2], args[3:]
        if verb == "create":
            tid = f"t_{len(self.tasks) + 1}"
            task = {
                "id": tid,
                "status": "ready",
                "title": rest[0],
                "assignee": rest[rest.index("--assignee") + 1],
                "model": rest[rest.index("--model") + 1] if "--model" in rest else None,
                "provider": rest[rest.index("--provider") + 1]
                if "--provider" in rest
                else None,
                "body": rest[rest.index("--body") + 1],
            }
            self.tasks[tid] = task
            self.comments[tid] = []
            self.runs[tid] = []
            self.events[tid] = []
            return self._ok(args, task)
        if verb == "show":
            tid = rest[0]
            return self._ok(
                args,
                {
                    "task": self.tasks[tid],
                    "runs": self.runs[tid],
                    "events": self.events[tid],
                    "latest_summary": self.summaries.get(tid),
                },
            )
        if verb == "list":
            return self._ok(args, list(self.tasks.values()))
        if verb == "dispatch":
            spawned = []
            for t in self.tasks.values():
                if t["status"] == "ready":
                    t["status"] = "running"
                    self.runs[t["id"]].append(
                        {
                            "id": f"r{len(self.runs[t['id']]) + 1}",
                            "worker_pid": 4242,
                            "started_at": 1900.0,
                        }
                    )
                    spawned.append(t["id"])
            return self._ok(args, {"spawned": spawned})
        if verb == "comment":
            self.comments[rest[0]].append(rest[1])
            return self._ok(args)
        if verb == "block":
            if self.tasks[rest[0]]["status"] not in {"ready", "running"}:
                return subprocess.CompletedProcess(
                    args, 1, stdout="", stderr="cannot block"
                )
            self.tasks[rest[0]]["status"] = "blocked"
            self.events[rest[0]].append(
                {"kind": "blocked", "payload": {"reason": rest[1]}}
            )
            return self._ok(args)
        if verb == "unblock":
            self.tasks[rest[0]]["status"] = "ready"
            return self._ok(args)
        if verb == "set-model":
            self.tasks[rest[0]]["model"] = rest[1]
            self.tasks[rest[0]]["provider"] = rest[rest.index("--provider") + 1]
            return self._ok(args)
        if verb == "reopen-review":
            self.tasks[rest[0]]["status"] = "ready"
            return self._ok(args)
        if verb == "complete":
            self.tasks[rest[0]]["status"] = "done"
            return self._ok(args)
        if verb == "log":
            return subprocess.CompletedProcess(
                args, 0, stdout=self.logs.get(rest[0], ""), stderr=""
            )
        raise AssertionError(f"unexpected kanban call: {args}")

    # -- claude ----------------------------------------------------------------

    def claude(self, cmd, kwargs):
        if cmd[1:3] == ["auth", "status"]:
            return subprocess.CompletedProcess(
                cmd,
                0,
                stdout=json.dumps({"loggedIn": True, "authMethod": "claude.ai"}),
                stderr="",
            )
        if getattr(self, "claude_json", None):
            return subprocess.CompletedProcess(
                cmd, 0, stdout=json.dumps(self.claude_json), stderr=""
            )
        behaviour = self.claude_behaviour or (lambda wt, env: None)
        behaviour(Path(kwargs["cwd"]), kwargs["env"])
        out = {
            "is_error": False,
            "result": "done",
            "session_id": "sess-9",
            "num_turns": 3,
            "usage": {"input_tokens": 50, "output_tokens": 5},
        }
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(out), stderr="")


@pytest.fixture
def world(tmp_path, team_state, monkeypatch):
    """A Team root with the implement role, an attached target repo, a synced profile, a ledger."""
    root = tmp_path / "root"
    (root / "team" / "roles" / "_base").mkdir(parents=True)
    shutil.copy(
        REPO / "team" / "roles" / "_base" / "config.yaml",
        root / "team" / "roles" / "_base" / "config.yaml",
    )
    write_role(root, "implement", skills=("commit-before-done",))
    # These tests exercise the kanban worker path; tests/team/test_sweep.py turns sweeps on.
    (root / "team" / "orchestration.toml").write_text("[sweep]\nenabled = false\n")
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.name", "T")
    git(root, "config", "user.email", "t@t")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "role")

    repo = tmp_path / "target"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.name", "T")
    git(repo, "config", "user.email", "t@t")
    (repo / "app.py").write_text("VALUE = 1\n")
    (repo / "check.sh").write_text("#!/bin/sh\ngrep -q 'VALUE = 2' app.py\n")
    (repo / "check.sh").chmod(0o755)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "initial")
    targets.attach(repo, name="team", check="./check.sh")

    profiles.profile_dir("implement").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    conn = ledger.connect(team_state / "team.db")
    ledger.init(conn)
    hermes = FakeHermes()
    loop = run.Loop(
        root,
        conn,
        runner=hermes,
        pid_alive=lambda pid: True,
        now=lambda: 2000.0,
        log=lambda m: None,
        preflight=None,
        cat=CATALOG,
        out=lambda line: None,
    )
    return {"root": root, "repo": repo, "conn": conn, "hermes": hermes, "loop": loop}


_ROUND = [0]


def worker_commits(world, goal, text="VALUE = 2\n"):
    """Pretend the worker did the task: edit, commit on the branch, request review."""
    _ROUND[0] += 1
    wt = Path(goal["worktree"])
    grade.ensure_worktree(world["repo"], wt, goal["branch"])
    (wt / "app.py").write_text(f"{text}# round {_ROUND[0]}\n")
    git(wt, "add", "-A")
    git(wt, "commit", "-q", "-m", "worker did it")
    world["hermes"].tasks[goal["current_task"]]["status"] = "review"


def test_start_goal_creates_everything(world):
    goal = run.start_goal(
        world["root"],
        world["conn"],
        "team",
        "Set VALUE to 2",
        runner=world["hermes"],
        cat=CATALOG,
    )
    assert goal["branch"] == "team/set-value-to-2" and Path(goal["worktree"]).exists()
    assert (
        goal["check_cmd"] == "./check.sh"
        and goal["attempt"] == 1
        and goal["current_task"] == "t_1"
    )
    task = world["hermes"].tasks["t_1"]
    assert (
        task["assignee"] == "implement"
        and task["provider"] == "litellm"
        and task["model"] == "tier-simple"
    )
    assert "kanban_request_review" in task["body"] and "./check.sh" in task["body"]
    job = ledger.get_job(world["conn"], goal["job_id"])
    assert job["task_ref"] == "t_1" and job["model_choice"]["spec"] == "tier-simple"
    assert job["target_commit"] == grade.head(world["repo"])
    second = run.start_goal(
        world["root"],
        world["conn"],
        "team",
        "Set VALUE to 2",
        runner=world["hermes"],
        cat=CATALOG,
    )
    assert second["branch"] == "team/set-value-to-2-2"


def test_start_goal_refusals(world):
    with pytest.raises(targets.TargetError):
        run.start_goal(
            world["root"],
            world["conn"],
            "nope",
            "x",
            runner=world["hermes"],
            cat=CATALOG,
        )
    with pytest.raises(Exception, match="unknown role"):
        run.start_goal(
            world["root"],
            world["conn"],
            "team",
            "x",
            role="ghost",
            runner=world["hermes"],
            cat=CATALOG,
        )
    shutil.rmtree(profiles.profile_dir("implement"))
    with pytest.raises(run.RunError, match="missing Hermes profile"):
        run.start_goal(
            world["root"],
            world["conn"],
            "team",
            "x",
            runner=world["hermes"],
            cat=CATALOG,
        )


def test_pass_waits_to_land_while_the_checkout_edits_its_files(world):
    goal = run.start_goal(
        world["root"],
        world["conn"],
        "team",
        "Set VALUE to 2",
        runner=world["hermes"],
        cat=CATALOG,
    )
    worker_commits(world, goal)
    (world["repo"] / "app.py").write_text("VALUE = 1  # someone is editing\n")
    world["loop"].tick()
    g = ledger.get_goal(world["conn"], goal["id"])
    assert g["outcome"] is None and g["finished"] is None
    assert g["detail"].startswith("waiting to land") and "app.py" in g["detail"]
    assert (world["repo"] / "app.py").read_text() == "VALUE = 1  # someone is editing\n"
    world["loop"].tick()  # still waiting; nothing breaks on the retry
    assert ledger.get_goal(world["conn"], goal["id"])["outcome"] is None
    git(world["repo"], "restore", "app.py")
    world["loop"].tick()
    g = ledger.get_goal(world["conn"], goal["id"])
    assert g["outcome"] == "pass" and g["detail"].startswith("landed ")
    assert (world["repo"] / "app.py").read_text().startswith("VALUE = 2\n")


def test_pass_lands_and_records(world):
    goal = run.start_goal(
        world["root"],
        world["conn"],
        "team",
        "Set VALUE to 2",
        runner=world["hermes"],
        cat=CATALOG,
    )
    ledger.record_call(
        world["conn"],
        None,
        "litellm",
        "tier-simple",
        "implement",
        10,
        2,
        100,
        True,
        task_ref="t_1",
        response_model="glm-5.3",
    )
    worker_commits(world, goal)
    world["loop"].tick()
    g = ledger.get_goal(world["conn"], goal["id"])
    assert g["outcome"] == "pass" and g["detail"].startswith("landed ")
    assert (world["repo"] / "app.py").read_text().startswith("VALUE = 2\n")
    assert (
        "Merge team/set-value-to-2"
        in git(world["repo"], "log", "-1", "--format=%s").stdout
    )
    assert world["hermes"].tasks["t_1"]["status"] == "done"
    verify, job = ledger.list_jobs(world["conn"])[:2]  # the code-only verify stage, then the worker's
    assert job["outcome"] == "pass" and job["grader"] == "check" and job["task_ref"] == "t_1"
    assert verify["outcome"] == "pass" and verify["grader"] == "verify" and verify["task_ref"] == "team-run:verify"
    assert g["job_id"] == verify["id"] and g["stage_idx"] == 1 and g["type"] == "bugfix"
    assert (
        ledger.calls_for(world["conn"], job_id=job["id"])[0]["response_model"]
        == "glm-5.3"
    )
    assert not Path(goal["worktree"]).exists() and not grade.branch_exists(
        world["repo"], goal["branch"]
    )
    assert ledger.pass_rate(world["conn"], "team") == (1, 1)


def test_fail_reopens_on_next_candidate_then_claude_then_eli(world):
    goal = run.start_goal(
        world["root"],
        world["conn"],
        "team",
        "Set VALUE to 2",
        runner=world["hermes"],
        cat=CATALOG,
    )
    worker_commits(world, goal, "VALUE = 3\n")
    world["loop"].tick()
    g = ledger.get_goal(world["conn"], goal["id"])
    assert g["attempt"] == 2 and g["current_task"] == "t_1" and g["outcome"] is None
    assert world["hermes"].tasks["t_1"]["status"] == "ready"
    assert world["hermes"].comments["t_1"][0].startswith("FAILED:")
    assert world["hermes"].tasks["t_1"]["model"] == "z-ai/glm-5.3"
    jobs = ledger.list_jobs(world["conn"])
    assert [j["attempt"] for j in jobs] == [2, 1]
    assert (
        jobs[1]["outcome"] == "fail"
        and jobs[0]["model_choice"]["spec"] == "nvidia:z-ai/glm-5.3"
    )

    # Attempt 2 fails too: the role allows 2 free attempts, so Claude is next.
    world["hermes"].tasks["t_1"]["status"] = "review"
    wt = Path(goal["worktree"])
    (wt / "app.py").write_text("VALUE = 4\n")
    git(wt, "commit", "-q", "-am", "still wrong")

    def claude_fixes(worktree, env):
        assert "ANTHROPIC_API_KEY" not in env and env["TEAM_CHECK"] == "./check.sh"
        (worktree / "app.py").write_text("VALUE = 2\n")
        git(worktree, "commit", "-q", "-am", "claude fixed it")

    world["hermes"].claude_behaviour = claude_fixes
    world["loop"].tick()
    g = ledger.get_goal(world["conn"], goal["id"])
    assert g["attempt"] == 3 and g["outcome"] == "pass"
    jobs = ledger.list_jobs(world["conn"])
    assert jobs[0]["task_ref"] == "team-run:verify"  # the code-only stage after Claude's pass
    jobs = jobs[1:]
    assert (
        jobs[0]["task_ref"] == "claude:sess-9"
        and jobs[0]["model_choice"]["provider"] == "team-subscription"
    )
    call = ledger.calls_for(world["conn"], job_id=jobs[0]["id"])[0]
    assert call["provider"] == "team-subscription" and call["tokens_in"] == 50
    assert (world["repo"] / "app.py").read_text() == "VALUE = 2\n"


def test_claude_fails_then_eli_decides(world):
    goal = run.start_goal(
        world["root"],
        world["conn"],
        "team",
        "Set VALUE to 2",
        runner=world["hermes"],
        cat=CATALOG,
    )
    for _ in range(2):
        world["hermes"].tasks["t_1"]["status"] = "review"
        worker_commits(world, goal, "VALUE = 9\n")
        world["loop"].tick()
    g = ledger.get_goal(world["conn"], goal["id"])
    assert (
        g["attempt"] == 3
        and g["outcome"] == "fail"
        and "Claude attempt failed too" in g["detail"]
    )
    assert world["hermes"].tasks["t_1"]["status"] == "blocked"
    [decision] = ledger.list_decisions(world["conn"], "team")
    assert decision["goal_id"] == goal["id"] and decision["source"] == "run"
    assert grade.branch_exists(world["repo"], goal["branch"])


def test_claude_usage_limit_pauses_instead_of_giving_up(world):
    goal = run.start_goal(
        world["root"],
        world["conn"],
        "team",
        "Set VALUE to 2",
        runner=world["hermes"],
        cat=CATALOG,
    )
    hermes = world["hermes"]
    limited = {
        "is_error": True,
        "result": "You've hit your session limit · resets 4:20pm",
        "session_id": "s-429",
        "num_turns": 1,
        "api_error": "usage_limit_reached",
        "api_error_status": 429,
        "usage": {},
    }
    hermes.claude_json = limited
    for n in range(2):
        hermes.tasks["t_1"]["status"] = "review"
        worker_commits(world, goal, f"VALUE = {7 + n}\n")
        world["loop"].tick()
    g = ledger.get_goal(world["conn"], goal["id"])
    assert g["outcome"] is None and g["attempt"] == 3
    job = ledger.get_job(world["conn"], g["job_id"])
    assert job["outcome"] == "error" and job["grader_detail"].startswith(
        "claude usage limit"
    )
    assert ledger.list_decisions(world["conn"], "team") == []
    pause = ledger.active_pause(world["conn"], "team", 2000.0)
    assert pause and "claude usage limit" in pause["reason"]
    world["loop"].tick()  # still paused: nothing happens
    assert ledger.get_goal(world["conn"], goal["id"])["job_id"] == job["id"]

    hermes.claude_json = None

    def claude_fixes(worktree, env):
        (worktree / "app.py").write_text("VALUE = 2\n")
        git(worktree, "commit", "-q", "-am", "claude fixed it")

    hermes.claude_behaviour = claude_fixes
    world["loop"].now = lambda: 2000.0 + 3601
    world["loop"].tick()
    g = ledger.get_goal(world["conn"], goal["id"])
    assert g["outcome"] == "pass" and g["attempt"] == 3
    assert [j["attempt"] for j in ledger.list_jobs(world["conn"])][:2] == [3, 3]


def _last_brief(world) -> str:
    """The brief of the Claude attempt FakeHermes is running right now."""
    return json.loads(world["hermes"].calls[-1][4])["brief"]


def ask_eli(world):
    """Two free attempts fail, then the Claude attempt asks Eli; returns (goal, decision, briefs)."""
    goal = run.start_goal(
        world["root"],
        world["conn"],
        "team",
        "Set VALUE to 2",
        runner=world["hermes"],
        cat=CATALOG,
    )
    briefs = []

    def claude_asks(worktree, env):
        briefs.append(_last_brief(world))
        Path(env["TEAM_DECISION_FILE"]).write_text(
            json.dumps(
                {
                    "question": "Which value?",
                    "context": "ambiguous",
                    "options": ["2", "3"],
                    "recommendation": "2",
                }
            )
        )

    world["hermes"].claude_behaviour = claude_asks
    for n in range(2):
        world["hermes"].tasks["t_1"]["status"] = "review"
        worker_commits(world, goal, f"VALUE = {9 + n}\n")
        world["loop"].tick()
    [decision] = [
        d
        for d in ledger.list_decisions(world["conn"], "team")
        if d["source"] == "claude"
    ]
    return goal, decision, briefs


def test_claude_writes_a_decision_for_eli_and_the_goal_waits_for_the_answer(world):
    goal, decision, briefs = ask_eli(world)
    assert decision["question"] == "Which value?" and decision["options"] == ["2", "3"]
    # the question is the only card: no "goal stopped" card on top of it
    assert ledger.list_decisions(world["conn"], "team") == [decision]
    g = ledger.get_goal(world["conn"], goal["id"])
    assert g["outcome"] is None and g["detail"] == f"waiting for Eli: decision {decision['id']}"
    job = ledger.get_job(world["conn"], g["job_id"])
    assert job["outcome"] == "error" and job["grader_detail"] == f"asked Eli: decision {decision['id']}"
    world["loop"].tick()  # still waiting: nothing runs
    assert ledger.get_goal(world["conn"], goal["id"])["job_id"] == job["id"] and len(briefs) == 1

    def claude_fixes(worktree, env):
        briefs.append(_last_brief(world))
        (worktree / "app.py").write_text("VALUE = 2\n")
        git(worktree, "commit", "-q", "-am", "claude fixed it")

    world["hermes"].claude_behaviour = claude_fixes
    ledger.answer_decision(world["conn"], decision["id"], "2")
    world["loop"].tick()
    g = ledger.get_goal(world["conn"], goal["id"])
    assert g["outcome"] == "pass" and g["attempt"] == 3
    assert len(briefs) == 2
    assert "Eli answered" in briefs[-1] and "Which value?" in briefs[-1] and "Answer: 2" in briefs[-1]


def test_an_answered_question_lands_the_branch_when_it_already_passes(world):
    goal, decision, briefs = ask_eli(world)
    wt = Path(goal["worktree"])
    (wt / "app.py").write_text("VALUE = 2\n")
    git(wt, "commit", "-q", "-am", "Eli fixed it by hand")
    ledger.answer_decision(world["conn"], decision["id"], "2, and I did it")
    world["loop"].tick()
    g = ledger.get_goal(world["conn"], goal["id"])
    assert g["outcome"] == "pass" and g["detail"].startswith("landed")
    assert len(briefs) == 1  # Claude was not run again
    [job] = [j for j in ledger.list_jobs(world["conn"]) if j["task_ref"] == "team-run:regrade"]
    assert job["outcome"] == "pass"
    assert job["model_choice"]["why"] == f"re-graded after decision {decision['id']}"
    # then the code-only verify stage ran and passed before the land
    assert ledger.get_job(world["conn"], g["job_id"])["task_ref"] == "team-run:verify"


def test_a_dismissed_question_stops_the_goal(world):
    goal, decision, _ = ask_eli(world)
    ledger.dismiss_decision(world["conn"], decision["id"])
    world["loop"].tick()
    g = ledger.get_goal(world["conn"], goal["id"])
    assert g["outcome"] == "fail" and f"dismissed its question (decision {decision['id']})" in g["detail"]
    [stopped] = [d for d in ledger.list_decisions(world["conn"], "team") if d["source"] == "run"]
    assert stopped["question"].startswith(f"Goal {goal['id']} stopped")


def test_violation_blocks_the_goal(world):
    goal = run.start_goal(
        world["root"],
        world["conn"],
        "team",
        "Set VALUE to 2",
        runner=world["hermes"],
        cat=CATALOG,
    )
    ledger.record_violation(
        world["conn"],
        "non-catalog",
        "model gpt-5 is not in the catalog",
        target="team",
        task_ref="t_1",
    )
    worker_commits(world, goal)
    world["loop"].tick()
    g = ledger.get_goal(world["conn"], goal["id"])
    assert g["outcome"] == "error" and "free gate" in g["detail"]
    assert world["hermes"].tasks["t_1"]["status"] == "blocked"
    assert (world["repo"] / "app.py").read_text() == "VALUE = 1\n"


def test_no_commits_and_outside_owned_fail_the_attempt(world):
    plan = ledger.create_plan(world["conn"], "team", "p")
    part = ledger.create_part(
        world["conn"], plan, "team", "part", owned_paths=["app.py"]
    )
    goal = run.start_goal(
        world["root"],
        world["conn"],
        "team",
        "Set VALUE to 2",
        runner=world["hermes"],
        cat=CATALOG,
        part_id=part,
    )
    assert "Files you own" in world["hermes"].tasks["t_1"]["body"]
    world["hermes"].tasks["t_1"]["status"] = "review"
    world["loop"].tick()
    assert ledger.list_jobs(world["conn"])[1]["grader_detail"] == "no commits"
    wt = Path(goal["worktree"])
    world["hermes"].tasks["t_1"]["status"] = "review"
    (wt / "app.py").write_text("VALUE = 2\n")
    (wt / "extra.py").write_text("x\n")
    git(wt, "add", "-A")
    git(wt, "commit", "-q", "-m", "too much")
    world["loop"].tick()
    jobs = ledger.list_jobs(world["conn"])
    assert jobs[1]["grader_detail"] == "changes outside owned files: extra.py"


def test_watchdog_dead_worker_and_quota_pause(world):
    goal = run.start_goal(
        world["root"],
        world["conn"],
        "team",
        "Set VALUE to 2",
        runner=world["hermes"],
        cat=CATALOG,
    )
    world["loop"].tick()  # dispatch: running with pid 4242
    assert world["hermes"].tasks["t_1"]["status"] == "running"
    world["loop"].pid_alive = lambda pid: False
    world["loop"].tick()
    g = ledger.get_goal(world["conn"], goal["id"])
    assert g["attempt"] == 2 and world["hermes"].tasks["t_2"]["status"] == "ready"
    assert (
        "worker pid 4242 is gone" in ledger.list_jobs(world["conn"])[1]["grader_detail"]
    )

    world["hermes"].logs["t_2"] = (
        "...HTTP 429: Rate limit exceeded: free-models-per-day"
    )
    world["hermes"].tasks["t_2"]["status"] = "blocked"
    world["loop"].tick()
    pause = ledger.active_pause(world["conn"], "team", 2000.0)
    assert pause and "free quota" in pause["reason"]
    g = ledger.get_goal(world["conn"], goal["id"])
    assert g["outcome"] is None and g["attempt"] == 2


def test_open_decision_pauses_the_goal_and_preflight_blocks_dispatch(world):
    goal = run.start_goal(
        world["root"],
        world["conn"],
        "team",
        "Set VALUE to 2",
        runner=world["hermes"],
        cat=CATALOG,
    )
    ledger.create_decision(
        world["conn"], "team", "q", "c", ["a"], "a", goal_id=goal["id"]
    )
    worker_commits(world, goal)
    world["loop"].tick()
    assert ledger.get_goal(world["conn"], goal["id"])["outcome"] is None
    logged = []
    world["loop"].log = logged.append
    world["loop"].preflight = lambda root, runner: [
        "profile implement: fallback_providers is not empty"
    ]
    world["hermes"].tasks["t_1"]["status"] = "ready"
    world["loop"].tick()
    assert world["hermes"].tasks["t_1"]["status"] == "ready"
    assert logged == [
        "not dispatching: profile implement: fallback_providers is not empty"
    ]


def test_quota_reset_parsing():
    assert run.quota_reset("fine", 1000.0) is None
    assert (
        run.quota_reset(
            "x 'X-RateLimit-Reset': '1700000000000' ... free-models-per-day", 0
        )
        == 1700000000.0
    )
    assert run.quota_reset("free-models-per-day", 86400 * 3 + 5) == 86400 * 4


@pytest.mark.parametrize("status", ["done", "archived", "blocked"])
def test_block_leaves_completed_task_done(world, status):
    goal = run.start_goal(
        world["root"],
        world["conn"],
        "team",
        "finish",
        runner=world["hermes"],
        cat=CATALOG,
    )
    world["hermes"].tasks[goal["current_task"]]["status"] = status
    world["loop"]._block(goal, "landing needs attention")
    assert world["hermes"].tasks[goal["current_task"]]["status"] == status


def test_subscription_role_dispatches_through_proxy_profile(world):
    catalog = json.loads((REPO / "integrations/portkey/catalog.json").read_text())
    (world["root"] / "team/roles/implement/role.toml").write_text(
        ROLE.replace(
            'harness = "hermes"',
            'harness = "hermes-subscription"\nmodel = "claude-opus-5-5"',
        ).replace(
            'candidates = ["tier-simple", "nvidia:z-ai/glm-5.3"]', "candidates = []"
        )
    )
    profiles.sync(world["root"], "implement", catalog, runner=world["hermes"])
    goal = run.start_goal(
        world["root"],
        world["conn"],
        "team",
        "Use subscription",
        runner=world["hermes"],
        cat=catalog,
    )
    task = world["hermes"].tasks[goal["current_task"]]
    assert task["provider"] == "team-subscription"
    assert task["model"] == "claude-opus-5-5"
    assert (
        profiles.profile_dir("implement")
        / "plugins/model-providers/team-subscription/__init__.py"
    ).is_file()


def test_the_loop_restarts_itself_between_ticks_when_team_code_changed(tmp_path, monkeypatch):
    from team import gateway

    class FakeLoop:
        cfg = {"loop.interval_s": 0}
        ticks, logged = 0, []

        def tick(self, **kwargs):
            self.ticks += 1

        def log(self, msg):
            self.logged.append(msg)

    changes = iter([None, "a Team source file changed after it started", None])
    monkeypatch.setattr(gateway, "changed_since", lambda root, born, sha: next(changes))
    monkeypatch.setattr(gateway, "checkout_sha", lambda root: "abc")
    restarts, loop = [], FakeLoop()

    def sleep(_s):
        if loop.ticks == 3:
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        run.run_loop(tmp_path, None, interval=0, sleep=sleep, loop=loop, restart=lambda: restarts.append(1))
    assert loop.ticks == 3 and restarts == [1]
    assert loop.logged == ["team run a Team source file changed after it started; restarting with the checkout's code"]


def test_uncommitted_work_is_committed_and_graded_instead_of_failed(world):
    goal = run.start_goal(
        world["root"], world["conn"], "team", "Set VALUE to 2", runner=world["hermes"], cat=CATALOG
    )
    wt = Path(goal["worktree"])
    grade.ensure_worktree(world["repo"], wt, goal["branch"])
    (wt / "app.py").write_text("VALUE = 2\n")  # the worker edited but never committed
    world["hermes"].tasks[goal["current_task"]]["status"] = "review"
    world["loop"].tick()
    job = ledger.get_job(world["conn"], goal["job_id"])
    assert job["outcome"] == "pass"
    assert job["grader_detail"].startswith("Team committed the worker's uncommitted changes before grading.")
    assert ledger.get_goal(world["conn"], goal["id"])["outcome"] == "pass"  # landed on main
    assert (world["repo"] / "app.py").read_text() == "VALUE = 2\n"


def test_commit_leftovers_takes_tracked_edits_and_owned_new_files_only(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.name", "T")
    git(repo, "config", "user.email", "t@t")
    (repo / "app.py").write_text("1\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "base")
    hook = repo / ".git" / "hooks" / "pre-commit"  # the target's lint hook must not stop the rescue
    hook.write_text("#!/bin/sh\necho lint failed >&2\nexit 1\n")
    hook.chmod(0o755)
    assert grade.commit_leftovers(repo, "nothing", ["team/"]) is None
    (repo / "app.py").write_text("2\n")
    (repo / "team").mkdir()
    (repo / "team" / "new.py").write_text("x\n")
    (repo / "scratch.txt").write_text("junk\n")
    sha = grade.commit_leftovers(repo, "leftovers", ["team/"])
    assert sha == grade.head(repo)
    assert grade.changed_files(repo, "HEAD", "HEAD~1") == ["app.py", "team/new.py"]
    assert (repo / "scratch.txt").exists() and "scratch.txt" in subprocess.check_output(
        ["git", "-C", str(repo), "ls-files", "--others"], text=True
    )


def test_left_work_sees_commits_edits_and_new_files(world):
    goal = run.start_goal(
        world["root"], world["conn"], "team", "Set VALUE to 2", runner=world["hermes"], cat=CATALOG
    )
    wt = Path(goal["worktree"])
    grade.ensure_worktree(world["repo"], wt, goal["branch"])
    assert world["loop"]._left_work(goal) is False
    (wt / "note.txt").write_text("new\n")
    assert world["loop"]._left_work(goal) is True
    (wt / "note.txt").unlink()
    (wt / "app.py").write_text("VALUE = 2\n")
    assert world["loop"]._left_work(goal) is True
    git(wt, "add", "-A")
    git(wt, "commit", "-q", "-m", "done")
    assert world["loop"]._left_work(goal) is True


# --- the pipeline: stages, gates, landing policy (team.gates) ----------------------


def _goal(world, text="Set VALUE to 2", **kw):
    return run.start_goal(world["root"], world["conn"], "team", text, runner=world["hermes"], cat=CATALOG, **kw)


def test_a_commit_that_changes_nothing_is_refused(world):
    goal = _goal(world)
    wt = Path(goal["worktree"])
    grade.ensure_worktree(world["repo"], wt, goal["branch"])
    git(wt, "commit", "-q", "--allow-empty", "-m", "done, honest")
    world["hermes"].tasks[goal["current_task"]]["status"] = "review"
    world["loop"].tick()
    g = ledger.get_goal(world["conn"], goal["id"])
    assert g["outcome"] is None and g["attempt"] == 2
    [comment] = world["hermes"].comments["t_1"][:1]
    assert comment.startswith("FAILED:") and "no changes against base" in comment
    job = next(j for j in ledger.list_jobs(world["conn"]) if j["attempt"] == 1)
    assert job["outcome"] == "fail" and job["failure_kind"] == "work"


def test_the_acceptance_command_must_pass_too(world):
    goal = _goal(world, acceptance="grep -q 'VALUE = 3' app.py")
    assert "fails now and must pass" not in world["hermes"].tasks["t_1"]["body"]  # a plain brief, no order
    worker_commits(world, goal)  # VALUE = 2: the check passes, the acceptance does not
    world["loop"].tick()
    g = ledger.get_goal(world["conn"], goal["id"])
    assert g["outcome"] is None and g["attempt"] == 2
    assert "acceptance command failed" in world["hermes"].comments["t_1"][0]
    assert (world["repo"] / "app.py").read_text() == "VALUE = 1\n"  # nothing landed


def test_verify_runs_the_target_e2e_and_sends_a_failure_back_to_the_builder(world):
    targets.set_field("team", "e2e", "test -f e2e-ok")
    goal = _goal(world)
    worker_commits(world, goal)
    world["loop"].tick()
    g = ledger.get_goal(world["conn"], goal["id"])
    jobs = ledger.list_jobs(world["conn"])
    verify = next(j for j in jobs if j["task_ref"] == "team-run:verify")
    assert verify["role"] == "team" and verify["outcome"] == "fail" and "e2e FAILED" in verify["grader_detail"]
    assert verify["failure_kind"] == "work" and verify["role_version"] == "code"
    # back to the builder: a second attempt on a new task, told what failed
    assert g["outcome"] is None and g["stage_idx"] == 0 and g["attempt"] == 2 and g["current_task"] == "t_2"
    assert "e2e FAILED" in world["hermes"].tasks["t_2"]["body"]
    assert ledger.pass_rate(world["conn"], "team") == (1, 1)  # the code gate's row is not an agent attempt
    # the builder makes e2e pass: verify passes, the goal lands
    (Path(g["worktree"]) / "e2e-ok").write_text("")
    git(Path(g["worktree"]), "add", "-A")
    git(Path(g["worktree"]), "commit", "-q", "-m", "e2e fixture")
    world["hermes"].tasks["t_2"]["status"] = "review"
    world["loop"].tick()
    g = ledger.get_goal(world["conn"], goal["id"])
    assert g["outcome"] == "pass" and g["detail"].startswith("landed")


def test_a_risky_diff_waits_for_eli_to_land(world):
    targets.set_field("team", "risk_paths", ["app.py"])
    goal = _goal(world)
    worker_commits(world, goal)
    world["loop"].tick()
    g = ledger.get_goal(world["conn"], goal["id"])
    assert g["outcome"] is None and g["risk"] == "ask" and g["detail"].startswith("waiting for Eli to land")
    [d] = [d for d in ledger.list_decisions(world["conn"], "team") if d["source"] == "land"]
    assert d["goal_id"] == goal["id"] and "app.py" in d["context"] and d["options"][0] == "land it"
    assert (world["repo"] / "app.py").read_text() == "VALUE = 1\n"
    world["loop"].tick()  # still waiting, no second decision
    assert len([d for d in ledger.list_decisions(world["conn"], "team", status=None) if d["source"] == "land"]) == 1
    ledger.answer_decision(world["conn"], d["id"], "land it")
    world["loop"].tick()
    g = ledger.get_goal(world["conn"], goal["id"])
    assert g["outcome"] == "pass" and g["detail"].startswith("landed")
    assert (world["repo"] / "app.py").read_text().startswith("VALUE = 2")


def test_eli_can_refuse_to_land_and_the_branch_is_kept(world):
    goal = _goal(world, kind="dependencies")  # always Eli's to land
    worker_commits(world, goal)
    world["loop"].tick()
    [d] = [d for d in ledger.list_decisions(world["conn"], "team") if d["source"] == "land"]
    assert "always waits for Eli" in d["context"]
    ledger.answer_decision(world["conn"], d["id"], "drop it")
    world["loop"].tick()
    g = ledger.get_goal(world["conn"], goal["id"])
    assert g["outcome"] == "fail" and "chose not to land" in g["detail"]
    assert grade.branch_exists(world["repo"], goal["branch"])
    assert (world["repo"] / "app.py").read_text() == "VALUE = 1\n"


def test_a_goal_type_must_exist_and_the_builder_must_fit_the_stage(world):
    with pytest.raises(Exception, match="unknown goal type"):
        _goal(world, kind="refactor")
    goal = _goal(world, role="implement")  # explicit builder with the right gate
    assert goal["role"] == "implement" and goal["type"] == "bugfix" and goal["stage_idx"] == 0


def _add_role(world, name, gate, tools='["read-only"]'):
    """A pipeline-stage role beside implement, with its profile directory."""
    text = ROLE.replace('tools = ["terminal", "file"]', f'tools = {tools}\ngate = "{gate}"\nassignable = false')
    write_role(world["root"], name, text=text)
    profiles.profile_dir(name).mkdir(parents=True, exist_ok=True)


def _review_answers(world, goal, verdict):
    """The reviewer's stage task, answered with ``verdict`` (a dict) and handed to review."""
    g = ledger.get_goal(world["conn"], goal["id"])
    tid = g["current_task"]
    assert world["hermes"].tasks[tid]["assignee"] == "review" and tid != goal["current_task"]
    world["hermes"].summaries[tid] = json.dumps(verdict)
    world["hermes"].tasks[tid]["status"] = "review"
    return tid


def test_a_land_verdict_lets_the_goal_land(world):
    _add_role(world, "review", "review")
    goal = _goal(world)
    assert json.loads(goal["stages"]) == ["implement", "test_verify", "review"]
    worker_commits(world, goal)
    world["loop"].tick()  # implement passes, verify passes, the review stage starts
    g = ledger.get_goal(world["conn"], goal["id"])
    assert g["stage_idx"] == 2 and g["role"] == "review" and g["attempt"] == 1 and g["outcome"] is None
    body = world["hermes"].tasks[g["current_task"]]["body"]
    assert body.startswith("[") is False and "+VALUE = 2" in body and '"verdict": "land" | "fix"' in body
    assert world["hermes"].tasks[g["current_task"]]["title"].startswith("[review]")
    assert g["stage_sha"] == git(world["repo"], "rev-parse", goal["branch"]).stdout.strip()
    tid = _review_answers(world, goal, {"verdict": "land", "checked": ["VALUE is 2: app.py:1"], "findings": []})
    world["loop"].tick()
    g = ledger.get_goal(world["conn"], goal["id"])
    assert g["outcome"] == "pass" and g["detail"].startswith("landed")
    review_job = next(j for j in ledger.list_jobs(world["conn"]) if j["role"] == "review")
    assert review_job["outcome"] == "pass" and review_job["grader"] == "review" and "REVIEW: land" in review_job["grader_detail"]
    assert world["hermes"].tasks[tid]["status"] == "done"


def test_a_fix_verdict_sends_the_part_back_to_the_builder_with_the_findings(world):
    _add_role(world, "review", "review")
    goal = _goal(world)
    worker_commits(world, goal)
    world["loop"].tick()
    tid = _review_answers(world, goal, {"verdict": "fix", "findings": [{"path": "app.py", "line": 2, "says": "drop the round comment"}]})
    world["loop"].tick()
    g = ledger.get_goal(world["conn"], goal["id"])
    assert g["outcome"] is None and g["stage_idx"] == 0 and g["role"] == "implement" and g["attempt"] == 2
    assert world["hermes"].tasks[tid]["status"] == "done"  # the review task is finished, not retried
    new = world["hermes"].tasks[g["current_task"]]
    assert new["assignee"] == "implement" and "drop the round comment" in new["body"] and "REVIEW: fix" in new["body"]
    review_job = next(j for j in ledger.list_jobs(world["conn"]) if j["role"] == "review")
    assert review_job["outcome"] == "fail" and review_job["failure_kind"] == "work"
    assert json.loads(g["stage_attempts"]) == {"implement": 1, "review": 1}
    # the builder's second try passes; verify, then a fresh review attempt (its count resumes)
    worker_commits(world, g, text="VALUE = 2\n")
    world["loop"].tick()
    g = ledger.get_goal(world["conn"], goal["id"])
    assert g["stage_idx"] == 2 and g["attempt"] == 2 and g["role"] == "review"


def test_a_prose_review_answer_is_refused_and_the_review_retried(world):
    _add_role(world, "review", "review")
    goal = _goal(world)
    worker_commits(world, goal)
    world["loop"].tick()
    g = ledger.get_goal(world["conn"], goal["id"])
    tid = g["current_task"]
    world["hermes"].summaries[tid] = "Looks good to me, ship it."
    world["hermes"].tasks[tid]["status"] = "review"
    world["loop"].tick()
    g = ledger.get_goal(world["conn"], goal["id"])
    assert g["stage_idx"] == 2 and g["attempt"] == 2 and g["outcome"] is None
    assert world["hermes"].tasks[tid]["status"] == "ready"  # the same review task, reopened
    assert world["hermes"].comments[tid][0].startswith("FAILED: review answer refused")


def test_the_test_write_stage_runs_first_and_only_test_files_may_change(world):
    _add_role(world, "test-write", "tests_only", tools='["terminal", "file"]')
    goal = _goal(world, acceptance="sh tests/accept.sh")
    assert json.loads(goal["stages"]) == ["test_write", "implement", "test_verify"]
    assert goal["role"] == "test-write" and goal["stage_idx"] == 0
    t1 = world["hermes"].tasks[goal["current_task"]]
    assert t1["assignee"] == "test-write" and "Change only test files" in t1["body"] and "sh tests/accept.sh" in t1["body"]
    # a test writer that also edits source is refused
    wt = Path(goal["worktree"])
    grade.ensure_worktree(world["repo"], wt, goal["branch"])
    (wt / "tests").mkdir()
    (wt / "tests" / "accept.sh").write_text("grep -q 'VALUE = 2' app.py\n")
    (wt / "app.py").write_text("VALUE = 1  # touched\n")
    git(wt, "add", "-A")
    git(wt, "commit", "-q", "-m", "tests plus a stray edit")
    world["hermes"].tasks[goal["current_task"]]["status"] = "review"
    world["loop"].tick()
    g = ledger.get_goal(world["conn"], goal["id"])
    assert g["attempt"] == 2 and g["stage_idx"] == 0
    assert "changes only test files" in world["hermes"].comments[goal["current_task"]][0]
    # undo the stray edit: tests only, failing as they should
    (wt / "app.py").write_text("VALUE = 1\n")
    git(wt, "commit", "-q", "-am", "tests only")
    world["hermes"].tasks[goal["current_task"]]["status"] = "review"
    world["loop"].tick()
    g = ledger.get_goal(world["conn"], goal["id"])
    assert g["stage_idx"] == 1 and g["role"] == "implement" and g["attempt"] == 1
    tw = next(j for j in ledger.list_jobs(world["conn"]) if j["role"] == "test-write" and j["outcome"] == "pass")
    assert "failing as they should" in tw["grader_detail"]
    # the builder makes the tests pass; the check and the acceptance pass; verify; land
    worker_commits(world, g)
    world["loop"].tick()
    g = ledger.get_goal(world["conn"], goal["id"])
    assert g["outcome"] == "pass" and g["detail"].startswith("landed")


def test_a_security_goal_carries_the_report_to_eli(world):
    _add_role(world, "security", "security")
    goal = _goal(world, kind="security")
    assert json.loads(goal["stages"]) == ["implement", "test_verify", "security"]
    worker_commits(world, goal)
    world["loop"].tick()
    g = ledger.get_goal(world["conn"], goal["id"])
    assert g["role"] == "security" and '"severity"' in world["hermes"].tasks[g["current_task"]]["body"]
    world["hermes"].summaries[g["current_task"]] = json.dumps(
        {"summary": "nothing dangerous", "findings": [{"path": "app.py", "line": 1, "severity": "info", "says": "a constant"}]}
    )
    world["hermes"].tasks[g["current_task"]]["status"] = "review"
    world["loop"].tick()
    g = ledger.get_goal(world["conn"], goal["id"])
    assert g["outcome"] is None and g["risk"] == "ask"
    [d] = [d for d in ledger.list_decisions(world["conn"], "team") if d["source"] == "land"]
    assert "security report: nothing dangerous" in d["context"] and "app.py:1 [info] a constant" in d["context"]


def test_onboarding_proves_the_check_it_proposes_and_eli_applies_it(world):
    _add_role(world, "onboard", "onboard", tools='["terminal", "file"]')
    goal = _goal(world, "Onboard this repo", kind="onboarding")
    assert json.loads(goal["stages"]) == ["spec"] and goal["role"] == "onboard" and goal["needs_commits"] == 0
    tid = goal["current_task"]
    body = world["hermes"].tasks[tid]["body"]
    assert "break_cmd" in body and "check: `./check.sh`" in body  # what Team already has, to confirm or replace
    # a break the check does not catch is refused
    world["hermes"].summaries[tid] = json.dumps(
        {"check": "test -f app.py", "test_runner": "", "e2e": "", "setup": "", "break_cmd": "true", "notes": ""}
    )
    world["hermes"].tasks[tid]["status"] = "review"
    world["loop"].tick()
    g = ledger.get_goal(world["conn"], goal["id"])
    assert g["attempt"] == 2 and "still passes after the break command" in world["hermes"].comments[tid][0]
    # a check that passes, fails after the break, and passes again once it is undone
    world["hermes"].summaries[tid] = json.dumps(
        {"check": "grep -q VALUE app.py", "test_runner": "sh {path}", "e2e": "", "setup": "", "break_cmd": ": > app.py", "notes": "no tests yet"}
    )
    world["hermes"].tasks[tid]["status"] = "review"
    world["loop"].tick()
    g = ledger.get_goal(world["conn"], goal["id"])
    assert g["outcome"] is None and g["risk"] == "ask" and g["detail"].startswith("waiting for Eli to land")
    [d] = [d for d in ledger.list_decisions(world["conn"], "team") if d["source"] == "land"]
    assert "onboarding proposal:" in d["context"] and "grep -q VALUE app.py" in d["context"]
    assert (Path(goal["worktree"]) / "app.py").read_text() == "VALUE = 1\n"  # the break was undone
    assert targets.require("team")["check"] == "./check.sh"  # nothing applied before Eli answers
    ledger.answer_decision(world["conn"], d["id"], "land it")
    world["loop"].tick()
    g = ledger.get_goal(world["conn"], goal["id"])
    assert g["outcome"] == "pass"
    t = targets.require("team")
    assert t["check"] == "grep -q VALUE app.py" and t["test_runner"] == "sh {path}" and t["e2e"] is None


def test_a_target_overlay_picks_the_profile_and_the_menu(world):
    from team import lead, overlay

    d = overlay.role_dir("team", "implement")
    (d / "skills" / "repo-rules").mkdir(parents=True)
    (d / "skills" / "repo-rules" / "SKILL.md").write_text("---\nname: repo-rules\ndescription: this repo's rules\n---\nFollow them.\n")
    (d / "role.toml").write_text('tools = ["file"]\n')
    profiles.profile_dir("implement--team").mkdir(parents=True)
    menu = lead.menu(world["root"], CATALOG, "team")
    assert menu["implement"]["toolsets"] == ["file"] and "repo-rules" in menu["implement"]["skills"]
    assert lead.menu(world["root"], CATALOG)["implement"]["toolsets"] == ["file", "terminal"]
    goal = _goal(world)
    assert world["hermes"].tasks[goal["current_task"]]["assignee"] == "implement--team"
    job = ledger.get_job(world["conn"], goal["job_id"])
    assert job["role"] == "implement"  # the ledger counts the role, whichever profile ran it


def test_one_team_run_per_target(team_state):
    held = run.acquire_lock("team")
    with pytest.raises(run.RunError, match="another team run holds"):
        run.acquire_lock("team")
    held.close()
    run.acquire_lock("team").close()  # free again
