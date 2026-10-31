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
        self.claude_behaviour = None

    def __call__(self, cmd, **kwargs):
        self.calls.append(list(cmd))
        if cmd[0].endswith("team-hermes") and cmd[1] == "kanban":
            return self.kanban(cmd[2:])
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
    job = ledger.get_job(world["conn"], g["job_id"])
    assert job["outcome"] == "pass" and job["grader"] == "check"
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


def test_claude_writes_a_decision_for_eli(world):
    goal = run.start_goal(
        world["root"],
        world["conn"],
        "team",
        "Set VALUE to 2",
        runner=world["hermes"],
        cat=CATALOG,
    )

    def claude_asks(worktree, env):
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
    assert decision["question"] == "Which value?" and decision["options"] == ["2", "3"]
    g = ledger.get_goal(world["conn"], goal["id"])
    assert "needs a decision" in g["detail"]


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
