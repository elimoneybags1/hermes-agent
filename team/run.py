"""`team goal` and `team run`: the autonomous loop on Hermes kanban.

Contract: docs/RUN.md, Round 3. Design: docs/KANBAN.md.

A goal is one branch and one worktree. Each attempt is a ledger job: free
attempts are kanban tasks on that branch, the last one may be Claude Code.
Every tick, for every enabled target: make sure its board exists, let kanban
dispatch, then step each goal Team tracks. What Team tracks lives in the
ledger's `goals` table, so `team run` can stop and restart at any point.

Every subprocess goes through an injectable `runner` (hermes, git, the check,
claude), and worker liveness through `pid_alive` / `kill`, so tests run none
of them for real.
"""

from __future__ import annotations

import os
import re
import signal
import sqlite3
import subprocess
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from team import claude_run, grade, kanban, ledger, settings, targets
from team.free import is_free_model
from team.roles import load_roles

Runner = Callable[..., subprocess.CompletedProcess]

# A run gets this long past its role's budget before Team calls it stuck.
GRACE_S = 300
FAILURE_CHARS = 4000


class RunError(RuntimeError):
    """A goal that cannot be started or carried on, with the reason why."""


def pid_alive(pid: int) -> bool:
    """True unless the process is gone. Another user's process counts as alive."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def terminate(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass


def _log(msg: str) -> None:
    print(f"team run: {msg}", file=sys.stderr, flush=True)


def _git(runner: Runner, repo: Path | str, *args: str) -> subprocess.CompletedProcess:
    return runner(["git", "-C", str(repo), *args], capture_output=True, text=True)


def _tail(text: str, limit: int = FAILURE_CHARS) -> str:
    return text if len(text) <= limit else "…" + text[-limit:]


def role_info(root: Path, name: str) -> dict:
    """The role's manifest; RunError unless it is a Hermes worker with a budget."""
    role = next((r for r in load_roles(root) if r["name"] == name), None)
    if role is None:
        raise RunError(f"unknown role: {name}")
    if role.get("runs_on", {}).get("harness") != "hermes":
        raise RunError(f"role {name} does not run on hermes workers")
    return role


def role_skills(root: Path, name: str) -> tuple[str, ...]:
    """Skill names under roles/<name>/skills/ (directory names)."""
    skills_dir = root / "roles" / name / "skills"
    if not skills_dir.is_dir():
        return ()
    return tuple(sorted(d.name for d in skills_dir.iterdir() if d.is_dir()))


def run_budget_s(role: dict) -> int:
    return int(role.get("budget", {}).get("run_budget_s", 900))


def after_failures(role: dict) -> int:
    return int(role.get("escalation", {}).get("after_failures", 0))


def role_version(root: Path, name: str, runner: Runner) -> str:
    """Git sha of the last commit touching roles/<name>, or "unknown"."""
    r = _git(runner, root, "log", "-1", "--format=%H", "--", f"roles/{name}")
    return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else "unknown"


def assignee_for(role: str, runner: Runner) -> str:
    """`team-<role>` when that Hermes profile exists, else `team-worker`."""
    profile = f"team-{role}"
    r = runner(["hermes", "profile", "list"], capture_output=True, text=True)
    if r.returncode == 0:
        names = {line.strip().lstrip("◆*").split()[0]
                 for line in r.stdout.splitlines() if line.strip()}
        if profile in names:
            return profile
    return "team-worker"


def slugify(text: str, limit: int = 40) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:limit].rstrip("-") or "goal"


def _free_slug(repo: Path, target: str, base: str, runner: Runner) -> str:
    """`base`, or `base-2`, `base-3`... whichever has neither branch nor worktree."""
    for n in range(1, 100):
        slug = base if n == 1 else f"{base}-{n}"
        taken = _git(runner, repo, "rev-parse", "--verify", "--quiet",
                     f"refs/heads/team/{slug}").returncode == 0
        if not taken and not grade.worktree_path(target, slug).exists():
            return slug
    raise RunError(f"no free slug for {base!r}")


def _brief(goal: dict, attempt: int, failure: str | None) -> str:
    text = (
        f"{goal['goal']}\n\n"
        f"You work in a git worktree on branch {goal['branch']}. Done means this "
        f"check passes there: `{goal['check_cmd']}`. Commit your work on this "
        "branch: uncommitted work is lost and does not count.\n\n"
        "IMPORTANT: When you are done, call `kanban_request_review` with a summary "
        "of what you did and the check's last line. Do NOT call `kanban_complete`."
    )
    if failure:
        text += f"\n\nAttempt {attempt - 1} failed. What it left:\n{_tail(failure)}"
        if "merge conflict with main" in failure:
            text += (
                "\n\nMERGE CONFLICT WITH MAIN: run `git merge main` first, "
                "resolve keeping both sides, run the check and commit the merge."
            )
    return text


def _paid(session: dict) -> str | None:
    """Why this session cannot be recorded as free, or None. Fails closed."""
    model = session.get("model")
    if not model:
        return "the session does not name its model"
    if not is_free_model(model):
        return f"model {model} is not free"
    cost = session.get("estimated_cost_usd")
    try:
        cost = float(cost or 0)
    except (TypeError, ValueError):
        return f"unreadable cost {cost!r}"
    if cost > 0:
        return f"estimated cost ${cost} is above zero"
    return None


def _is_paid_model(session: dict | None) -> tuple[bool, str | None]:
    """Check if a session has a paid model; returns (is_paid, reason)."""
    if session is None:
        return False, None
    reason = _paid(session)
    return reason is not None, reason


def _build_session_data(session: dict | None) -> dict:
    """Build provider/model/tokens data for record_call, handling None session."""
    if session is None:
        return {
            "provider": "unknown",
            "model": "unknown",
            "tokens_in": None,
            "tokens_out": None,
            "ok": True,  # Can't prove it's paid, so allow
            "error": None,
        }
    return {
        "provider": session.get("billing_provider") or "unknown",
        "model": session.get("model") or "unknown",
        "tokens_in": session.get("input_tokens"),
        "tokens_out": session.get("output_tokens"),
        "ok": True,
        "error": None,
    }


def _blocked_reason(full: dict) -> str:
    task = full.get("task", {})
    for event in reversed(full.get("events") or []):
        if event.get("kind") == "blocked" and (event.get("payload") or {}).get("reason"):
            return event["payload"]["reason"]
    return full.get("latest_summary") or task.get("result") or task.get("status") or "blocked"


class Loop:
    """One `team run`: its settings are read fresh at the start of every tick."""

    def __init__(
        self,
        root: Path,
        conn: sqlite3.Connection,
        *,
        runner: Runner = subprocess.run,
        pid_alive: Callable[[int], bool] = pid_alive,
        kill: Callable[[int], None] = terminate,
        now: Callable[[], float] = time.time,
        log: Callable[[str], None] = _log,
    ):
        self.root = Path(root)
        self.conn = conn
        self.runner = runner
        self.pid_alive = pid_alive
        self.kill = kill
        self.now = now
        self.log = log
        self.cfg = settings.load(self.root)

    # -- ticks ---------------------------------------------------------------

    def tick(self) -> None:
        self.cfg = settings.load(self.root)
        for target in targets.list_targets():
            name = target["name"]
            if not settings.target_settings(name)["enabled"]:
                continue
            try:
                self.tick_target(name)
            except (kanban.KanbanError, grade.GradeError, RunError, OSError) as e:
                self.log(f"{name}: {e} {getattr(e, 'stderr', '')}".rstrip())

    def tick_target(self, name: str) -> None:
        kanban.ensure_board(name, runner=self.runner)
        # Collect status for one-line stdout output
        dispatch_result = kanban.dispatch(name, max_spawns=self.cfg["loop.max_new_per_tick"], runner=self.runner)
        spawned = dispatch_result.get("spawned", [])
        dispatched = len(spawned)
        
        goals = ledger.active_goals(self.conn, name)
        transitions = []
        running_count = 0
        
        for goal in goals:
            full = None
            try:
                status_before = self._get_goal_status(goal)
                self.step(goal, full)
                status_after = self._get_goal_status(goal)
                if status_before != status_after:
                    transitions.append(f"goal {goal['id']} {status_before}\u2192{status_after}")
                if status_after == "running":
                    running_count += 1
            except Exception as e:
                # Any unexpected exception is logged with traceback and does not stop the tick/loop
                tb = traceback.format_exc()
                self.log(f"goal {goal['id']}: unexpected error: {e}\n{tb}")
        
        # One line per tick on stdout
        if dispatched > 0 or transitions:
            ts = time.strftime("%H:%M:%S", time.localtime(self.now()))
            parts = [f"{ts} {name}:"]
            if dispatched > 0:
                parts.append(f"dispatched {dispatched}")
            parts.extend(transitions)
            print("; ".join(parts))
        elif running_count > 0:
            ts = time.strftime("%H:%M:%S", time.localtime(self.now()))
            print(f"{ts} {name}: idle ({running_count} goals running)")

    def _get_goal_status(self, goal: dict) -> str:
        """Get current kanban task status for a goal."""
        if not goal.get("current_task"):
            return "none"
        try:
            full = kanban.show_full(goal["target"], goal["current_task"], runner=self.runner)
            return full.get("task", {}).get("status", "unknown")
        except (kanban.KanbanError, OSError):
            return "error"

    def step(self, goal: dict, full: dict | None = None) -> None:
        """Move one goal along as far as its current state allows."""
        job = ledger.get_job(self.conn, goal["job_id"]) if goal["job_id"] else None
        if job is None:
            # Race: goal row exists before team goal attaches its job.
            # Skip if created < 120s ago instead of failing with 'goal has no job'.
            created = goal.get("created")
            if created is not None:
                try:
                    created_ts = datetime.fromisoformat(created).timestamp()
                    if self.now() - created_ts < 120:
                        return
                except (ValueError, TypeError):
                    pass
            ledger.finish_goal(self.conn, goal["id"], "error", "goal has no job")
            return
        claude = (job["task_ref"] or "").startswith("claude:")
        if job["ended"] is None and claude:
            reason = "team run stopped during the Claude attempt"
            ledger.finish_job(self.conn, job["id"], "error", "team-run", reason)
            job = ledger.get_job(self.conn, job["id"])
        if job["ended"] is not None:
            # Stopped between grading this job and acting on its outcome.
            if job["outcome"] == "pass":
                self._land(goal)
            elif claude:
                self._give_up(goal, "the Claude attempt failed too", job["grader_detail"])
            else:
                self._next_attempt(goal, job["grader_detail"])
            return

        if full is None:
            full = kanban.show_full(goal["target"], goal["current_task"], runner=self.runner)
        task = full["task"]
        status = task.get("status")
        if status == "review":
            self._grade_review(goal, job, full)
        elif status == "done":
            # Worker marked done anyway - grade as before; on fail fall back to new task
            self._grade_free(goal, job, full)
        elif status == "running":
            self._watch(goal, job, full)
        elif status in ("blocked", "archived"):
            reason = _blocked_reason(full)
            ledger.finish_job(self.conn, job["id"], "error", "kanban", reason)
            ledger.finish_goal(self.conn, goal["id"], "error", f"kanban {status}: {reason}")
        # Anything else is still queued: kanban dispatches it.

    # -- free attempts ---------------------------------------------------------

    def free_attempt(self, goal: dict, attempt: int, failure: str | None = None) -> str:
        """A kanban task for attempt `attempt` on the goal's branch; returns its id."""
        role = role_info(self.root, goal["role"])
        self._ensure_worktree(goal)
        title = goal["goal"].splitlines()[0][:80]
        if attempt > 1:
            title += f" (attempt {attempt})"
        skills = role_skills(self.root, goal["role"])
        model = role.get("runs_on", {}).get("model", "")
        if not model or not is_free_model(model):
            raise RunError(f"role {goal['role']} has no free model configured")
        task = kanban.create_task(
            goal["target"], title, _brief(goal, attempt, failure),
            assignee=assignee_for(goal["role"], self.runner),
            workspace=f"worktree:{goal['worktree']}",
            branch=goal["branch"],
            max_runtime=run_budget_s(role),
            skills=skills,
            model=model,
            runner=self.runner,
        )
        job = self._start_job(goal, attempt, task["id"])
        ledger.update_goal(self.conn, goal["id"], attempt=attempt,
                           current_task=task["id"], job_id=job)
        return task["id"]

    def _watch(self, goal: dict, job: dict, full: dict) -> None:
        """Kanban neither enforces --max-runtime nor notices a dead worker."""
        runs = full.get("runs") or []
        if not runs:
            return
        run = runs[-1]
        pid, started = run.get("worker_pid"), run.get("started_at")
        budget = run_budget_s(role_info(self.root, goal["role"]))
        alive = pid is not None and self.pid_alive(int(pid))
        reason = None
        if pid is not None and not alive:
            reason = f"worker pid {pid} is gone"
        elif started is not None and self.now() - float(started) > budget + GRACE_S:
            age = int(self.now() - float(started))
            reason = f"run {run.get('id')} is {age}s old, over its {budget}s budget + {GRACE_S}s"
        if reason is None:
            return
        if alive:
            self.kill(int(pid))
            reason += f"; terminated pid {pid}"
        kanban.block(goal["target"], goal["current_task"], reason, runner=self.runner)
        ledger.finish_job(self.conn, job["id"], "error", "team-run", reason)
        self._next_attempt(goal, reason)

    def _grade_free(self, goal: dict, job: dict, full: dict) -> None:
        task_id = goal["current_task"]
        # Get assignee from task to use as profile for session_for_task, like _grade_review does
        assignee = full.get("task", {}).get("assignee", "team-worker")
        session = kanban.session_for_task(task_id, profile=assignee, runner=self.runner)
        is_paid, reason = _is_paid_model(session)
        session_data = _build_session_data(session)
        ledger.record_call(
            self.conn, job["id"],
            provider=session_data["provider"],
            model=session_data["model"],
            task_class=goal["role"], tokens_in=session_data["tokens_in"],
            tokens_out=session_data["tokens_out"], latency_ms=None,
            ok=session_data["ok"] and not is_paid, error=reason if is_paid else session_data["error"],
        )
        if is_paid:
            reason = f"refused as free: {reason}"
            ledger.finish_job(self.conn, job["id"], "error", "free-gate", reason)
            self._block(goal, reason)
            ledger.finish_goal(self.conn, goal["id"], "error", reason)
            return
        ok, detail = self.grade(goal)
        ledger.finish_job(self.conn, job["id"], "pass" if ok else "fail",
                          goal["check_cmd"], detail)
        if ok:
            self._land(goal)
        else:
            self._next_attempt(goal, detail)

    def _grade_review(self, goal: dict, job: dict, full: dict) -> None:
        """Grade a task that the worker moved to review."""
        task_id = goal["current_task"]
        # Get assignee from task to use as profile for session_for_task
        assignee = full.get("task", {}).get("assignee", "team-worker")
        session = kanban.session_for_task(task_id, profile=assignee, runner=self.runner)
        is_paid, reason = _is_paid_model(session)
        session_data = _build_session_data(session)
        ledger.record_call(
            self.conn, job["id"],
            provider=session_data["provider"],
            model=session_data["model"],
            task_class=goal["role"], tokens_in=session_data["tokens_in"],
            tokens_out=session_data["tokens_out"], latency_ms=None,
            ok=session_data["ok"] and not is_paid, error=reason if is_paid else session_data["error"],
        )
        if is_paid:
            reason = f"refused as free: {reason}"
            ledger.finish_job(self.conn, job["id"], "error", "free-gate", reason)
            self._block(goal, reason)
            ledger.finish_goal(self.conn, goal["id"], "error", reason)
            return
        ok, detail = self.grade(goal)
        ledger.finish_job(self.conn, job["id"], "pass" if ok else "fail",
                          goal["check_cmd"], detail)
        if ok:
            kanban.complete(goal["target"], task_id, result=f"passed: {detail[-200:]}", runner=self.runner)
            self._land(goal)
        else:
            # Decide next step BEFORE touching kanban
            goal_updated = ledger.get_goal(self.conn, goal["id"])
            if goal_updated is None:
                return
            role = role_info(self.root, goal_updated["role"])
            nxt = goal_updated["attempt"] + 1
            if nxt <= after_failures(role):
                # Next free attempt: comment failure tail, then reopen_review
                kanban.comment(goal["target"], task_id, f"FAILED: {_tail(detail)}", runner=self.runner)
                kanban.reopen_review(goal["target"], task_id, runner=self.runner)
                new_job = self._start_job(goal_updated, nxt, task_id)
                ledger.update_goal(self.conn, goal_updated["id"], attempt=nxt, job_id=new_job)
            elif nxt == after_failures(role) + 1 and self.cfg["escalation.claude"]:
                # Claude attempt: comment, then block the task (kind needs_input)
                kanban.comment(goal["target"], task_id, f"FAILED: {_tail(detail)}", runner=self.runner)
                kanban.block(goal["target"], task_id, "Escalating to Claude", kind="needs_input", runner=self.runner)
                self._claude_attempt(goal_updated, nxt, detail)
            else:
                # Give up: comment and block
                kanban.comment(goal["target"], task_id, f"FAILED: {_tail(detail)}", runner=self.runner)
                self._give_up(goal_updated, f"all {goal_updated['attempt']} attempts failed", detail)

    # -- escalation ------------------------------------------------------------

    def _next_attempt(self, goal: dict, failure: str | None) -> None:
        """Free attempts up to the role's after_failures, then one on Claude."""
        goal = ledger.get_goal(self.conn, goal["id"])
        role = role_info(self.root, goal["role"])
        nxt = goal["attempt"] + 1
        if nxt <= after_failures(role):
            self.free_attempt(goal, nxt, failure)
        elif nxt == after_failures(role) + 1 and self.cfg["escalation.claude"]:
            self._claude_attempt(goal, nxt, failure)
        else:
            self._give_up(goal, f"all {goal['attempt']} attempts failed", failure)

    def _claude_attempt(self, goal: dict, attempt: int, failure: str | None) -> None:
        self._ensure_worktree(goal)
        job = self._start_job(goal, attempt, "claude:")
        ledger.update_goal(self.conn, goal["id"], attempt=attempt, job_id=job)
        result = claude_run.run(
            goal["worktree"], _brief(goal, attempt, failure),
            max_turns=self.cfg["escalation.claude_max_turns"],
            timeout_s=self.cfg["escalation.claude_timeout_s"],
            runner=self.runner,
        )
        ledger.set_task_ref(self.conn, job, f"claude:{result.get('session_id') or 'unknown'}")
        usage = result.get("usage") or {}
        ledger.record_call(
            self.conn, job, provider="claude-code-subscription", model="claude",
            task_class=goal["role"], tokens_in=usage.get("input_tokens"),
            tokens_out=usage.get("output_tokens"), latency_ms=result.get("duration_ms"),
            ok=bool(result["ok"]), error=None if result["ok"] else _tail(str(result["result"]), 500),
        )
        if not result["ok"]:
            detail = f"claude run failed: {result['result']}"
            ledger.finish_job(self.conn, job, "error", "claude", detail)
            # Comment result on task, then block
            kanban.comment(goal["target"], goal["current_task"], f"CLAUDE FAILED: {_tail(detail)}", runner=self.runner)
            kanban.block(goal["target"], goal["current_task"], "Claude attempt failed", kind="needs_input", runner=self.runner)
            self._give_up(goal, "the Claude attempt failed too", detail)
            return
        ok, detail = self.grade(goal)
        ledger.finish_job(self.conn, job, "pass" if ok else "fail", goal["check_cmd"], detail)
        if ok:
            kanban.complete(goal["target"], goal["current_task"], result=f"claude passed: {detail[-200:]}", runner=self.runner)
            self._land(goal)
        else:
            kanban.comment(goal["target"], goal["current_task"], f"CLAUDE FAILED CHECK: {_tail(detail)}", runner=self.runner)
            kanban.block(goal["target"], goal["current_task"], "Claude attempt failed check", kind="needs_input", runner=self.runner)
            self._give_up(goal, "the Claude attempt failed too", detail)

    def _give_up(self, goal: dict, why: str, detail: str | None) -> None:
        """Stop working on the goal; block its last kanban task so Eli decides."""
        reason = f"{why}. Branch {goal['branch']} is kept.\n{_tail(detail or '', 1500)}".strip()
        if self.cfg["ask.block_after_claude_fails"]:
            self._block(goal, reason)
        ledger.finish_goal(self.conn, goal["id"], "fail", reason)

    def _block(self, goal: dict, reason: str) -> None:
        try:
            kanban.block(goal["target"], goal["current_task"], reason, runner=self.runner)
        except kanban.KanbanError as e:
            self.log(f"goal {goal['id']}: could not block {goal['current_task']}: {e.stderr}")

    # -- grading and landing ---------------------------------------------------

    def _repo(self, goal: dict) -> Path:
        target = targets.get(goal["target"])
        if target is None:
            raise RunError(f"target {goal['target']} is no longer attached")
        return Path(target["path"])

    def _ensure_worktree(self, goal: dict) -> None:
        """Kanban removes a clean worktree when its task completes; the branch stays."""
        path = Path(goal["worktree"])
        if path.exists():
            return
        repo = self._repo(goal)
        _git(self.runner, repo, "worktree", "prune")
        path.parent.mkdir(parents=True, exist_ok=True)
        r = _git(self.runner, repo, "worktree", "add", str(path), goal["branch"])
        if r.returncode != 0:
            raise RunError(f"cannot recreate worktree {path}: {r.stderr.strip()}")

    def grade(self, goal: dict) -> tuple[bool, str]:
        """Merge main into the branch, then run the check there. (passed, output)."""
        repo, branch, wt = self._repo(goal), goal["branch"], Path(goal["worktree"])
        self._ensure_worktree(goal)
        if not grade.has_commits(repo, branch, runner=self.runner):
            return False, "no commits"
        dirty = _git(self.runner, wt, "status", "--porcelain", "--untracked-files=no")
        if dirty.stdout.strip():
            return False, f"uncommitted changes in the worktree:\n{dirty.stdout.rstrip()}"
        merge = _git(self.runner, wt, "merge", "--no-edit", "main")
        if merge.returncode != 0:
            conflicts = _git(self.runner, wt, "diff", "--name-only", "--diff-filter=U").stdout.strip()
            _git(self.runner, wt, "merge", "--abort")
            return False, ("merge conflict with main:\n"
                           + (conflicts or (merge.stdout + merge.stderr).strip()))
        ok, output = grade.run_check(wt, goal["check_cmd"],
                                     timeout_s=self.cfg["grading.check_timeout_s"],
                                     runner=self.runner)
        if not ok and not output:
            output = f"`{goal['check_cmd']}` failed with no output"
        return ok, output

    def _land(self, goal: dict) -> None:
        repo = self._repo(goal)
        first = goal["goal"].splitlines()[0][:60]
        try:
            sha = grade.land(repo, goal["branch"],
                             f"Merge {goal['branch']}: {first} (goal {goal['id']})",
                             runner=self.runner)
        except grade.GradeError as e:
            reason = f"passed its check but could not land: {e}. Branch {goal['branch']} is kept."
            self._block(goal, reason)
            ledger.finish_goal(self.conn, goal["id"], "error", reason)
            return
        try:
            grade.remove_worktree(repo, goal["worktree"], goal["branch"], runner=self.runner)
        except grade.GradeError as e:
            self.log(f"goal {goal['id']}: landed, but {e}")
        ledger.finish_goal(self.conn, goal["id"], "pass", f"landed {sha}")

    def _start_job(self, goal: dict, attempt: int, task_ref: str) -> int:
        repo = self._repo(goal)
        head = _git(self.runner, repo, "rev-parse", "main")
        commit = head.stdout.strip() if head.returncode == 0 else "unknown"
        return ledger.start_job(
            self.conn, goal["target"], commit, goal["role"],
            role_version(self.root, goal["role"], self.runner),
            attempt=attempt, task_ref=task_ref,
        )


def start_goal(
    root: Path,
    conn: sqlite3.Connection,
    target: str,
    goal: str,
    *,
    role: str = "implement",
    check: str | None = None,
    runner: Runner = subprocess.run,
) -> dict:
    """`team goal`: a worktree, a kanban task, a ledger job and a goals row."""
    record = targets.get(target) if targets.NAME_RE.fullmatch(target) else None
    if record is None:
        raise RunError(f"unknown target: {target}")
    role_info(root, role)
    if check is not None:
        settings.set_target(target, "check", check)
    check_cmd = settings.check_for(root, target)
    repo = Path(record["path"])

    kanban.ensure_board(target, runner=runner)
    slug = _free_slug(repo, target, slugify(goal), runner)
    path, branch = grade.create_worktree(repo, target, slug, runner=runner)
    goal_id = ledger.create_goal(conn, target, goal, role, branch, str(path), check_cmd)
    try:
        Loop(root, conn, runner=runner).free_attempt(ledger.get_goal(conn, goal_id), 1)
    except Exception as e:
        ledger.finish_goal(conn, goal_id, "error", f"could not create the first task: {e}")
        raise
    return ledger.get_goal(conn, goal_id)


def run_loop(
    root: Path,
    conn: sqlite3.Connection,
    *,
    once: bool = False,
    interval: int | None = None,
    runner: Runner = subprocess.run,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Tick until stopped; the interval is re-read from settings every tick."""
    loop = Loop(root, conn, runner=runner)
    while True:
        loop.tick()
        if once:
            return
        sleep(interval if interval is not None else loop.cfg["loop.interval_s"])