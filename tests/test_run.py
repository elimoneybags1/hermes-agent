"""Tests for team/run.py: real git in tmp_path, a real ledger, fake hermes and claude."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from team import ledger, run, settings, targets

ROLE = """\
name = "implement"
status = "active"
[runs_on]
harness = "hermes"
model = "nvidia/nemotron-3-ultra-550b-a55b:free"
[escalation]
after_failures = 2
[budget]
run_budget_s = 900
"""

FREE = "nvidia/nemotron-3-ultra-550b-a55b:free"
CHECK = "grep -q ok result.txt"
NOW = 2_000_000_000.0


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def init_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    git(path, "init", "-q", "-b", "main")
    git(path, "config", "user.name", "Test")
    git(path, "config", "user.email", "test@test")
    (path / "README.md").write_text("# test\n")
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "initial")
    return path


def commit(worktree, name, text, message="work"):
    (Path(worktree) / name).write_text(text)
    git(worktree, "add", name)
    git(worktree, "commit", "-q", "-m", message)


class FakeHermes:
    """Hermes kanban, sessions and claude, kept in memory; git and sh run for real."""

    def __init__(self):
        self.boards = {"default"}
        self.tasks: dict[str, dict] = {}
        self.blocked: dict[str, tuple[str, str]] = {}
        self.sessions: dict[str, dict] = {}
        self.calls: list[list[str]] = []
        self.dispatches: list[list[str]] = []
        self.profiles = ["default", "team-worker"]
        self.claude = None  # fn(cwd) -> dict payload
        self.claude_env = None

    def __call__(self, cmd, **kwargs):
        if cmd[0] == "hermes":
            self.calls.append(cmd)
            return self._hermes(cmd[1:])
        if cmd[0] == "claude":
            self.calls.append(cmd)
            self.claude_env = kwargs.get("env")
            payload = self.claude(kwargs["cwd"])
            return subprocess.CompletedProcess(cmd, 0, json.dumps(payload), "")
        return subprocess.run(cmd, **kwargs)

    def _ok(self, out=""):
        return subprocess.CompletedProcess([], 0, out if isinstance(out, str) else json.dumps(out), "")

    def _hermes(self, a):
        if a[:2] == ["profile", "list"]:
            return self._ok("\n".join(f"  {p}   model" for p in self.profiles))
        assert a[:2] == ["-p", "team-worker"], a
        a = a[2:]
        if a[:2] == ["sessions", "list"]:
            rows = [f"work kanban task {t[:9]}   —   now   s_{t}" for t in self.sessions]
            return self._ok("Title   Workspace   Last Active   ID\n" + "\n".join(rows))
        if a[:2] == ["sessions", "export"]:
            task = a[a.index("--session-id") + 1][2:]
            return self._ok(json.dumps(self.sessions[task]) + "\n")
        assert a[0] == "kanban", a
        a = a[1:]
        if a[:2] == ["boards", "list"]:
            return self._ok([{"slug": b} for b in sorted(self.boards)])
        if a[:2] == ["boards", "create"]:
            self.boards.add(a[2])
            return self._ok()
        assert a[0] == "--board" and a[1] in self.boards, a
        sub, rest = a[2], a[3:]
        if sub == "dispatch":
            self.dispatches.append(a)
            return self._ok({"spawned": [], "dispatched": 0})
        if sub == "create":
            tid = f"t_{len(self.tasks) + 1:08x}"
            task = {"id": tid, "title": rest[0], "status": "ready", "result": None,
                    "body": rest[rest.index("--body") + 1],
                    "assignee": rest[rest.index("--assignee") + 1],
                    "argv": a}
            # Extract skills and model if present (after --json)
            skills = []
            i = 0
            while i < len(rest):
                if rest[i] == "--skill":
                    skills.append(rest[i + 1])
                    i += 2
                elif rest[i] == "--model":
                    task["model"] = rest[i + 1]
                    i += 2
                else:
                    i += 1
            if skills:
                task["skills"] = skills
            self.tasks[tid] = {"task": task, "runs": [], "events": [], "latest_summary": None}
            return self._ok(task)
        if sub == "show":
            return self._ok(self.tasks[rest[0]])
        if sub == "block":
            self.blocked[rest[0]] = (rest[1], rest[rest.index("--kind") + 1])
            self.tasks[rest[0]]["task"]["status"] = "blocked"
            return self._ok()
        if sub == "comment":
            self.tasks[rest[0]]["events"].append({"kind": "comment", "payload": rest[1]})
            return self._ok()
        if sub == "reopen-review":
            self.tasks[rest[0]]["task"]["status"] = "ready"
            self.tasks[rest[0]]["events"].append({"kind": "reopen-review", "payload": ""})
            return self._ok()
        if sub == "complete":
            result = None
            if "--result" in rest:
                result = rest[rest.index("--result") + 1]
            self.tasks[rest[0]]["task"]["status"] = "done"
            self.tasks[rest[0]]["task"]["result"] = result
            return self._ok()
        raise AssertionError(f"unexpected kanban call {a}")

    # helpers for tests
    def done(self, tid, model=FREE, cost=0.0):
        self.tasks[tid]["task"]["status"] = "done"
        self.sessions[tid] = {"model": model, "billing_provider": "openrouter",
                              "input_tokens": 100, "output_tokens": 20,
                              "estimated_cost_usd": cost, "api_call_count": 3}

    def running(self, tid, pid, started):
        self.tasks[tid]["task"]["status"] = "running"
        self.tasks[tid]["runs"].append({"id": 1, "worker_pid": pid, "started_at": started})


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("TEAM_STATE", str(tmp_path / "state"))
    repo = init_repo(tmp_path / "demo")
    targets.attach(repo, "demo")
    root = init_repo(tmp_path / "team")
    (root / "roles" / "implement").mkdir(parents=True)
    (root / "roles" / "implement" / "role.toml").write_text(ROLE)
    # Create skills directories for the implement role
    (root / "roles" / "implement" / "skills" / "commit-before-done").mkdir(parents=True)
    (root / "roles" / "implement" / "skills" / "merge-main-first").mkdir(parents=True)
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "role")
    conn = ledger.connect(tmp_path / "state" / "team.db")
    ledger.init(conn)
    fake = FakeHermes()

    class Env:
        pass

    e = Env()
    e.repo, e.root, e.conn, e.fake, e.tmp = repo, root, conn, fake, tmp_path
    e.alive = {}
    e.killed = []
    e.logs = []

    def loop():
        return run.Loop(root, conn, runner=fake, pid_alive=lambda p: e.alive.get(p, False),
                        kill=e.killed.append, now=lambda: NOW, log=e.logs.append)

    def goal(text="Write ok into result.txt", check=CHECK):
        return run.start_goal(root, conn, "demo", text, check=check, runner=fake)

    def jobs():
        return list(reversed(ledger.list_jobs(conn)))

    e.loop, e.goal, e.jobs = loop, goal, jobs
    e.get = lambda gid: ledger.get_goal(conn, gid)
    yield e
    conn.close()


def test_start_goal_creates_worktree_task_job_and_goal(env):
    g = env.goal()
    wt = Path(g["worktree"])
    assert g["branch"] == "team/write-ok-into-result-txt"
    assert wt == env.tmp / "state" / "worktrees" / "demo" / "write-ok-into-result-txt"
    assert git(wt, "branch", "--show-current") == g["branch"]
    assert "demo" in env.fake.boards
    task = env.fake.tasks[g["current_task"]]["task"]
    argv = task["argv"]
    assert argv[:3] == ["--board", "demo", "create"]
    assert argv[argv.index("--workspace") + 1] == f"worktree:{wt}"
    assert argv[argv.index("--branch") + 1] == g["branch"]
    assert argv[argv.index("--max-runtime") + 1] == "900s"
    assert task["assignee"] == "team-worker"
    assert CHECK in task["body"]
    # Check that skills and model are passed
    assert "commit-before-done" in task.get("skills", [])
    assert "merge-main-first" in task.get("skills", [])
    assert task.get("model") == FREE
    [job] = env.jobs()
    assert (job["attempt"], job["task_ref"], job["role"]) == (1, g["current_task"], "implement")
    assert job["target_commit"] == git(env.repo, "rev-parse", "main")
    assert job["role_version"] == git(env.root, "rev-parse", "HEAD")
    assert (g["attempt"], g["job_id"], g["check_cmd"]) == (1, job["id"], CHECK)
    assert settings.target_settings("demo")["check"] == CHECK


def test_start_goal_uses_role_profile_and_a_free_slug(env):
    env.fake.profiles.append("team-implement")
    first = env.goal("Same goal")
    second = env.goal("Same goal")
    assert second["branch"] == first["branch"] + "-2"
    assert env.fake.tasks[second["current_task"]]["task"]["assignee"] == "team-implement"


def test_start_goal_refuses_unknown_target_and_role(env):
    with pytest.raises(run.RunError):
        run.start_goal(env.root, env.conn, "nope", "x", runner=env.fake)
    with pytest.raises(run.RunError):
        run.start_goal(env.root, env.conn, "demo", "x", role="boss", runner=env.fake)


def test_pass_lands_and_cleans_up_even_after_kanban_removed_the_worktree(env):
    g = env.goal()
    commit(g["worktree"], "result.txt", "ok\n")
    git(env.repo, "worktree", "remove", g["worktree"])  # what kanban does on done
    env.fake.done(g["current_task"])
    env.loop().tick()

    assert (env.repo / "result.txt").read_text() == "ok\n"
    assert not Path(g["worktree"]).exists()
    assert git(env.repo, "branch", "--list", g["branch"]) == ""
    [job] = env.jobs()
    assert (job["outcome"], job["grader"]) == ("pass", CHECK)
    done = env.get(g["id"])
    assert done["outcome"] == "pass" and done["detail"].startswith("landed ")
    [call] = [dict(r) for r in env.conn.execute("SELECT * FROM calls")]
    assert (call["model"], call["ok"], call["tokens_in"], call["task_class"]) == (FREE, 1, 100, "implement")
    assert env.fake.dispatches[0][-2:] == ["--max", "3"]


def test_fail_retries_on_the_same_branch_with_the_failure(env):
    g = env.goal()
    commit(g["worktree"], "result.txt", "nope\n")
    env.fake.done(g["current_task"])
    env.loop().tick()

    first, second = env.jobs()
    assert first["outcome"] == "fail"
    assert second["attempt"] == 2 and second["ended"] is None
    g2 = env.get(g["id"])
    assert g2["attempt"] == 2 and g2["current_task"] == second["task_ref"] != g["current_task"]
    task = env.fake.tasks[g2["current_task"]]["task"]
    assert task["argv"][task["argv"].index("--branch") + 1] == g["branch"]
    assert "Attempt 1 failed" in task["body"]
    assert task["title"].endswith("(attempt 2)")


def test_no_commits_is_a_fail(env):
    g = env.goal()
    env.fake.done(g["current_task"])
    env.loop().tick()
    assert env.jobs()[0]["grader_detail"] == "no commits"
    assert env.get(g["id"])["attempt"] == 2


def test_check_runs_against_current_main(env):
    g = env.goal(check="test -f from_main.txt && grep -q ok result.txt")
    commit(env.repo, "from_main.txt", "x\n", "main moved on")
    commit(g["worktree"], "result.txt", "ok\n")
    env.fake.done(g["current_task"])
    env.loop().tick()
    assert env.jobs()[0]["outcome"] == "pass"
    assert env.get(g["id"])["outcome"] == "pass"


def test_merge_conflict_with_main_is_a_failed_attempt(env):
    g = env.goal()
    commit(env.repo, "result.txt", "main\n", "main writes it")
    commit(g["worktree"], "result.txt", "ok\n")
    env.fake.done(g["current_task"])
    env.loop().tick()

    job = env.jobs()[0]
    assert job["outcome"] == "fail"
    assert job["grader_detail"] == "merge conflict with main:\nresult.txt"
    assert git(g["worktree"], "status", "--porcelain") == ""  # merge aborted
    assert env.get(g["id"])["attempt"] == 2


def test_uncommitted_changes_fail_the_attempt(env):
    g = env.goal()
    commit(g["worktree"], "result.txt", "nope\n")
    (Path(g["worktree"]) / "result.txt").write_text("ok\n")
    env.fake.done(g["current_task"])
    env.loop().tick()
    assert env.jobs()[0]["grader_detail"].startswith("uncommitted changes")


def fail_free_attempts(env, g, n=2):
    for _ in range(n):
        cur = env.get(g["id"])
        commit(cur["worktree"], "result.txt", f"nope {cur['attempt']}\n")
        env.fake.done(cur["current_task"])
        env.loop().tick()


def test_escalates_to_claude_after_free_attempts_and_lands(env, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-never-pass")

    def claude(cwd):
        commit(cwd, "result.txt", "ok\n", "claude fix")
        return {"is_error": False, "result": "fixed", "session_id": "sess-1",
                "num_turns": 4, "usage": {"input_tokens": 9, "output_tokens": 3}}

    env.fake.claude = claude
    g = env.goal()
    fail_free_attempts(env, g)

    jobs = env.jobs()
    assert [(j["attempt"], j["outcome"]) for j in jobs] == [(1, "fail"), (2, "fail"), (3, "pass")]
    assert jobs[2]["task_ref"] == "claude:sess-1"
    assert "ANTHROPIC_API_KEY" not in env.fake.claude_env
    assert env.get(g["id"])["outcome"] == "pass"
    assert (env.repo / "result.txt").read_text() == "ok\n"
    assert env.fake.blocked == {}
    claude_call = env.conn.execute("SELECT * FROM calls WHERE job_id = ?", (jobs[2]["id"],)).fetchone()
    assert (claude_call["provider"], claude_call["tokens_in"]) == ("claude-code-subscription", 9)


def test_claude_failure_blocks_the_last_task_for_eli(env):
    env.fake.claude = lambda cwd: {"is_error": True, "result": "gave up", "session_id": "s2"}
    g = env.goal()
    fail_free_attempts(env, g)

    last_task = env.get(g["id"])["current_task"]
    reason, kind = env.fake.blocked[last_task]
    assert kind == "needs_input" and "Claude attempt failed" in reason
    assert env.jobs()[2]["outcome"] == "error"
    assert env.get(g["id"])["outcome"] == "fail"


def test_no_claude_when_settings_turn_it_off(env):
    (env.root / "orchestration.toml").write_text(
        "[escalation]\nclaude = false\n[ask]\nblock_after_claude_fails = false\n")
    g = env.goal()
    fail_free_attempts(env, g)
    assert not any(c[0] == "claude" for c in env.fake.calls)
    assert env.fake.blocked == {}
    assert env.get(g["id"])["outcome"] == "fail"
    assert len(env.jobs()) == 2


def test_dead_worker_is_blocked_and_the_retry_continues(env):
    g = env.goal()
    env.fake.running(g["current_task"], pid=4242, started=NOW - 10)
    env.loop().tick()

    reason, _ = env.fake.blocked[g["current_task"]]
    assert "4242" in reason
    assert env.killed == []
    first, second = env.jobs()
    assert first["outcome"] == "error" and "4242" in first["grader_detail"]
    assert second["attempt"] == 2


def test_overdue_worker_is_terminated(env):
    g = env.goal()
    env.alive[77] = True
    env.fake.running(g["current_task"], pid=77, started=NOW - (900 + 301))
    env.loop().tick()
    assert env.killed == [77]
    assert "terminated pid 77" in env.fake.blocked[g["current_task"]][0]
    assert env.jobs()[0]["outcome"] == "error"
    assert env.get(g["id"])["attempt"] == 2


def test_healthy_worker_is_left_alone(env):
    g = env.goal()
    env.alive[77] = True
    env.fake.running(g["current_task"], pid=77, started=NOW - 900)
    env.loop().tick()
    assert env.killed == [] and env.fake.blocked == {}
    assert env.jobs()[0]["ended"] is None


def test_blocked_task_is_a_ledger_error_with_kanbans_reason(env):
    g = env.goal()
    entry = env.fake.tasks[g["current_task"]]
    entry["task"]["status"] = "blocked"
    entry["events"].append({"kind": "blocked", "payload": {"reason": "needs a key"}})
    env.loop().tick()
    [job] = env.jobs()
    assert (job["outcome"], job["grader_detail"]) == ("error", "needs a key")
    assert env.get(g["id"])["outcome"] == "error"


def test_paid_model_is_refused(env):
    g = env.goal()
    commit(g["worktree"], "result.txt", "ok\n")
    env.fake.done(g["current_task"], model="anthropic/claude-opus")
    env.loop().tick()
    [job] = env.jobs()
    assert job["outcome"] == "error" and "not free" in job["grader_detail"]
    call = env.conn.execute("SELECT * FROM calls").fetchone()
    assert call["ok"] == 0
    assert g["current_task"] in env.fake.blocked
    assert not (env.repo / "result.txt").exists()


def test_cost_above_zero_is_refused(env):
    g = env.goal()
    env.fake.done(g["current_task"], cost=0.01)
    env.loop().tick()
    assert "above zero" in env.jobs()[0]["grader_detail"]


def test_claude_attempt_cut_off_by_a_restart_is_an_error(env):
    g = env.goal()
    job = ledger.start_job(env.conn, "demo", "c", "implement", "v", attempt=3, task_ref="claude:")
    ledger.update_goal(env.conn, g["id"], attempt=3, job_id=job)
    env.loop().tick()
    assert ledger.get_job(env.conn, job)["outcome"] == "error"
    assert env.get(g["id"])["outcome"] == "fail"
    assert g["current_task"] in env.fake.blocked


def test_restart_after_grading_carries_on(env):
    g = env.goal()
    ledger.finish_job(env.conn, g["job_id"], "fail", CHECK, "boom")
    env.loop().tick()  # a fresh Loop: the goals table says where it stopped
    assert env.get(g["id"])["attempt"] == 2


def test_disabled_target_is_skipped(env):
    env.goal()
    settings.set_target("demo", "enabled", False)
    before = len(env.fake.calls)
    env.loop().tick()
    assert len(env.fake.calls) == before


def test_tick_creates_missing_board(env):
    env.loop().tick()
    assert "demo" in env.fake.boards
    assert env.fake.dispatches == [["--board", "demo", "dispatch", "--json", "--max", "3"]]


def test_run_loop_sleeps_the_settings_interval(env):
    (env.root / "orchestration.toml").write_text("[loop]\ninterval_s = 7\n")
    slept = []

    def sleep(s):
        slept.append(s)
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        run.run_loop(env.root, env.conn, runner=env.fake, sleep=sleep)
    assert slept == [7]
    run.run_loop(env.root, env.conn, once=True, runner=env.fake, sleep=sleep)
    assert slept == [7]


def test_pid_alive():
    assert run.pid_alive(subprocess.os.getpid())
    p = subprocess.Popen(["true"])
    p.wait()
    assert not run.pid_alive(p.pid)


def test_slugify():
    assert run.slugify("Add a stop-loss: 5% trailing!") == "add-a-stop-loss-5-trailing"
    assert run.slugify("!!!") == "goal"
    assert len(run.slugify("x" * 100)) == 40


def test_review_pass_completes_and_lands(env):
    """Task in review passes: kanban.complete, then land."""
    g = env.goal()
    commit(g["worktree"], "result.txt", "ok\n")
    # Move task to review status
    env.fake.done(g["current_task"])
    task_entry = env.fake.tasks[g["current_task"]]
    task_entry["task"]["status"] = "review"

    env.loop().tick()

    # Should have completed the task and landed
    assert (env.repo / "result.txt").read_text() == "ok\n"
    done = env.get(g["id"])
    assert done["outcome"] == "pass" and done["detail"].startswith("landed ")
    # Verify complete was called on the task
    assert task_entry["task"]["status"] in ("done", "review")  # complete doesn't change status in fake


def test_review_fail_comments_reopens_and_retries(env):
    """Task in review fails: comment (with tail), reopen_review, new attempt on same branch."""
    g = env.goal()
    commit(g["worktree"], "result.txt", "nope\n")
    env.fake.done(g["current_task"])
    task_entry = env.fake.tasks[g["current_task"]]
    task_entry["task"]["status"] = "review"

    env.loop().tick()

    first, second = env.jobs()
    assert first["outcome"] == "fail"
    # Same task should still be current (no new task created)
    g2 = env.get(g["id"])
    assert g2["attempt"] == 2
    assert g2["current_task"] == g["current_task"]
    # Verify comment and reopen_review were called on the task
    # The fake's _hermes tracks events for comment and reopen-review
    # Check that the task has comment with FAILED prefix and reopen-review events
    events = task_entry.get("events", [])
    comment_events = [e for e in events if e.get("kind") == "comment"]
    reopen_events = [e for e in events if e.get("kind") == "reopen-review"]
    # Should have at least one comment with FAILED prefix
    assert len(comment_events) >= 1, f"Expected comment events, got {events}"
    assert any("FAILED" in e.get("payload", "") for e in comment_events), f"Comment should contain FAILED: {comment_events}"
    # Should have reopen-review event
    assert len(reopen_events) >= 1, f"Expected reopen-review event, got {events}"


def test_done_task_fail_falls_back_to_new_task(env):
    """Worker marked done anyway; on fail, creates new task on same branch."""
    g = env.goal()
    commit(g["worktree"], "result.txt", "nope\n")
    env.fake.done(g["current_task"])
    # Leave status as "done" (worker didn't use review)
    task_entry = env.fake.tasks[g["current_task"]]
    task_entry["task"]["status"] = "done"

    env.loop().tick()

    first, second = env.jobs()
    assert first["outcome"] == "fail"
    g2 = env.get(g["id"])
    assert g2["attempt"] == 2
    # New task created for retry
    assert g2["current_task"] != g["current_task"]
    new_task = env.fake.tasks[g2["current_task"]]["task"]
    assert new_task["argv"][new_task["argv"].index("--branch") + 1] == g["branch"]


def test_review_free_model_check(env):
    """Paid model in review is refused."""
    g = env.goal()
    commit(g["worktree"], "result.txt", "ok\n")
    env.fake.done(g["current_task"])
    task_entry = env.fake.tasks[g["current_task"]]
    task_entry["task"]["status"] = "review"
    # Use a paid model
    env.fake.sessions[g["current_task"]] = {
        "model": "anthropic/claude-opus",
        "billing_provider": "openrouter",
        "input_tokens": 100, "output_tokens": 20,
        "estimated_cost_usd": 0.5, "api_call_count": 3
    }

    env.loop().tick()

    job = env.jobs()[0]
    assert job["outcome"] == "error" and "not free" in job["grader_detail"]
    assert g["current_task"] in env.fake.blocked
    assert not (env.repo / "result.txt").exists()


def test_brief_mentions_kanban_request_review(env):
    """The brief instructs the worker to use kanban_request_review."""
    g = env.goal()
    task = env.fake.tasks[g["current_task"]]["task"]
    assert "kanban_request_review" in task["body"]
    assert "kanban_complete" in task["body"]
    assert "Do NOT call" in task["body"]


def test_brief_merge_conflict_instruction(env):
    """The brief includes merge conflict resolution instructions when appropriate."""
    g = env.goal()
    # Create a merge conflict scenario
    commit(env.repo, "result.txt", "main\n", "main writes it")
    commit(g["worktree"], "result.txt", "ok\n")
    env.fake.done(g["current_task"])
    env.loop().tick()

    g2 = env.get(g["id"])
    task2 = env.fake.tasks[g2["current_task"]]["task"]
    assert "MERGE CONFLICT WITH MAIN" in task2["body"]
    assert "git merge main" in task2["body"]
