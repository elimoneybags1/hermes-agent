"""``team goal`` and ``team run``: the autonomous loop on Hermes kanban.

A goal is one branch and one worktree. Each attempt is a ledger job: free
attempts are kanban tasks on that branch (one task for the goal's whole
life, reopened from ``review`` on a failure), the last one may be Claude
Code on Eli's subscription. Every tick, for every enabled target: run the
preflight (gate audit, catalog check, spend guard), let kanban dispatch, then
step each goal Team tracks. What Team tracks lives in the ledger's ``goals``
table, so ``team run`` can stop and restart at any point.

"Done" is decided by code: the target's check in the worktree after ``base``
is merged in, only owned files changed, work committed. Model calls are
recorded by the Team Hermes plugin as they happen; grading attaches them to
the job and refuses anything the catalog does not allow.

Every subprocess goes through an injectable ``runner`` (hermes, git, the
check, claude), and worker liveness through ``pid_alive`` / ``kill``, so tests
run none of them for real except git and ``/bin/sh``.
"""

from __future__ import annotations

import json
import os
import re
import signal
import sqlite3
import subprocess
import sys
import time
import traceback
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from team import (
    catalog,
    claude_roles,
    claude_run,
    grade,
    kanban,
    ledger,
    profiles,
    roles,
    settings,
    targets,
)

Runner = Callable[..., subprocess.CompletedProcess[Any]]

# A run gets this long past its role's budget before Team calls it stuck.
GRACE_S = 300
FAILURE_CHARS = 4000
# A worker that hits a free tier's daily cap ends its log with this; the log
# also echoes the task body, so only the end of the log counts.
QUOTA_ERROR = "free-models-per-day"
QUOTA_RESET_RE = re.compile(r"'X-RateLimit-Reset': '(\d+)'")
LOG_TAIL_BYTES = 8000
QUOTA_END_CHARS = 2000


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


def _tail(text: str | None, limit: int = FAILURE_CHARS) -> str:
    text = text or ""
    return text if len(text) <= limit else "…" + text[-limit:]


def slugify(text: str, limit: int = 40) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:limit].rstrip("-") or "goal"


def _free_slug(repo: Path, target: str, base: str, runner: Runner) -> str:
    """``base``, or ``base-2``, ``base-3``... whichever has neither branch nor worktree."""
    for n in range(1, 100):
        slug = base if n == 1 else f"{base}-{n}"
        if (
            not grade.branch_exists(repo, f"team/{slug}", runner)
            and not grade.worktree_path(target, slug).exists()
        ):
            return slug
    raise RunError(f"no free slug for {base!r}")


def brief(
    goal: dict,
    attempt: int,
    failure: str | None,
    owned: list[str] | None,
    claude: bool = False,
) -> str:
    text = (
        f"{goal['goal']}\n\n"
        f"Workspace: {goal['worktree']}. Resolve file paths and run commands in this directory. "
        f"You work in a git worktree on branch {goal['branch']}. Done means this "
        f"check passes there: `{goal['check_cmd']}`. Commit your work on this "
        "branch: uncommitted work is lost and does not count."
    )
    if owned:
        text += "\n\nFiles you own (change nothing else): " + ", ".join(owned)
    if not claude:
        text += (
            "\n\nWhen you are done, call `kanban_request_review` with a summary of what "
            "you did and the check's last line. Never call `kanban_complete`: Team grades."
        )
    if failure:
        text += f"\n\nAttempt {attempt - 1} failed. What it left:\n{_tail(failure)}"
        if "merge conflict with" in failure:
            text += (
                "\n\nMERGE CONFLICT: run `git merge "
                + goal.get("base", "main")
                + "` first, "
                "resolve keeping both sides, run the check and commit the merge."
            )
    return text


def quota_reset(log_text: str, now: float) -> float | None:
    """When the free quota that stopped this worker resets (epoch s), or None."""
    if QUOTA_ERROR not in log_text[-QUOTA_END_CHARS:]:
        return None
    resets = QUOTA_RESET_RE.findall(log_text)
    if resets:
        return int(resets[-1]) / 1000
    return (now // 86400 + 1) * 86400  # free tiers reset at UTC midnight


def _gave_up(full: dict) -> dict | None:
    """Kanban's ``gave_up`` payload when that, not a block, is the task's last word."""
    for event in reversed(full.get("events") or []):
        if event.get("kind") == "blocked":
            return None
        if event.get("kind") == "gave_up":
            return event.get("payload") or {}
    return None


def _status(full: dict | None) -> str:
    return full.get("task", {}).get("status", "unknown") if full else "none"


def _blocked_reason(full: dict) -> str:
    task = full.get("task", {})
    for event in reversed(full.get("events") or []):
        if event.get("kind") == "blocked" and (event.get("payload") or {}).get(
            "reason"
        ):
            return event["payload"]["reason"]
    return (
        full.get("latest_summary")
        or task.get("result")
        or task.get("status")
        or "blocked"
    )


def default_preflight(root: Path, runner: Runner) -> list[str]:
    """Reasons not to dispatch this tick: a profile outside the lock, a stale catalog build."""
    reasons: list[str] = []
    if profiles.hermes_home().exists():
        for name, found in profiles.audit_profiles(root).items():
            reasons.append(f"profile {name}: " + "; ".join(found))
    build = Path(root) / "integrations" / "portkey" / "build.py"
    if build.exists():
        r = runner(
            [sys.executable, str(build), "--check"], capture_output=True, text=True
        )
        if r.returncode != 0:
            reasons.append(
                "catalog build is stale: " + _tail((r.stderr or r.stdout).strip(), 400)
            )
    return reasons


class Loop:
    """One ``team run``: settings are read fresh at the start of every tick."""

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
        preflight: Callable[[Path, Runner], list[str]] | None = default_preflight,
        cat: dict | None = None,
        out: Callable[[str], None] = print,
    ):
        self.root = Path(root)
        self.conn = conn
        self.runner = runner
        self.pid_alive = pid_alive
        self.kill = kill
        self.now = now
        self.log = log
        self.preflight = preflight
        self._cat = cat
        self.out = out
        self.cfg = settings.load(self.root)

    @property
    def cat(self) -> dict:
        if self._cat is None:
            self._cat = catalog.load()
        return self._cat

    # -- ticks ---------------------------------------------------------------

    def tick(self) -> None:
        self.cfg = settings.load(self.root)
        blocked = self.preflight(self.root, self.runner) if self.preflight else []
        for reason in blocked:
            self.log(f"not dispatching: {reason}")
        for target in targets.list_targets():
            if not target.get("enabled", True):
                continue
            try:
                self.tick_target(target["name"], dispatch=not blocked)
            except (
                kanban.KanbanError,
                grade.GradeError,
                RunError,
                catalog.GateError,
                OSError,
            ) as e:
                self.log(f"{target['name']}: {e} {getattr(e, 'stderr', '')}".rstrip())

    def tick_target(self, name: str, dispatch: bool = True) -> None:
        kanban.ensure_board(name, runner=self.runner)
        dispatched = 0
        if dispatch and ledger.active_pause(self.conn, name, self.now()) is None:
            result = kanban.dispatch(
                name, max_spawns=self.cfg["loop.max_new_per_tick"], runner=self.runner
            )
            dispatched = len(result.get("spawned") or [])
        transitions, running = [], 0
        paused = ledger.paused_goals(self.conn, name)
        for goal in ledger.active_goals(self.conn, name):
            if goal["id"] in paused:
                continue
            try:
                full = self._show(goal)
                before = _status(full)
                after = (
                    _status(self._show(ledger.get_goal(self.conn, goal["id"])))
                    if self.step(goal, full)
                    else before
                )
                if before != after:
                    transitions.append(f"goal {goal['id']} {before}→{after}")
                if after == "running":
                    running += 1
            except Exception as e:  # one goal's failure must not stop the tick
                self.log(
                    f"goal {goal['id']}: unexpected error: {e}\n{traceback.format_exc()}"
                )
        ts = time.strftime("%H:%M:%S", time.localtime(self.now()))
        parts = []
        pause = ledger.active_pause(self.conn, name, self.now())
        if pause is not None:
            until = datetime.fromisoformat(pause["until"]).timestamp()
            parts.append(
                f"paused, free quota until {time.strftime('%H:%M', time.localtime(until))}"
            )
        if dispatched:
            parts.append(f"dispatched {dispatched}")
        parts.extend(transitions)
        if parts:
            self.out(f"{ts} {name}: " + "; ".join(parts))
        elif running:
            self.out(f"{ts} {name}: idle ({running} goals running)")

    def _show(self, goal: dict | None) -> dict | None:
        if not goal or not goal.get("current_task"):
            return None
        return kanban.show_full(
            goal["target"], goal["current_task"], runner=self.runner
        )

    def step(self, goal: dict, full: dict | None = None) -> bool:
        """Move one goal along as far as its state allows; True when it acted."""
        job = ledger.get_job(self.conn, goal["job_id"]) if goal["job_id"] else None
        if job is None:
            created = goal.get("created")
            if (
                created
                and self.now() - datetime.fromisoformat(created).timestamp() < 120
            ):
                return False
            ledger.finish_goal(self.conn, goal["id"], "error", "goal has no job")
            return True
        claude = (job["task_ref"] or "").startswith("claude:")
        if job["ended"] is None and claude:
            ledger.finish_job(
                self.conn,
                job["id"],
                "error",
                "team-run",
                "team run stopped during the Claude attempt",
            )
            job = ledger.get_job(self.conn, job["id"])
        if job["ended"] is not None:
            if job["outcome"] == "pass":
                self._land(goal)
            elif claude:
                self._give_up(
                    goal, "the Claude attempt failed too", job["grader_detail"]
                )
            else:
                self._next_attempt(goal, job["grader_detail"])
            return True
        if full is None:
            full = kanban.show_full(
                goal["target"], goal["current_task"], runner=self.runner
            )
        status = full["task"].get("status")
        if status in ("review", "done"):
            self._grade_task(goal, job, status)
        elif status == "running":
            return self._watch(goal, job, full)
        elif status in ("blocked", "crashed", "archived"):
            if status != "archived":
                until = self._quota_reset(goal)
                if until is not None:
                    return self._quota_wait(goal, until)
            gave_up = _gave_up(full) if status != "archived" else None
            if gave_up is not None:
                trigger = gave_up.get("trigger_outcome") or "error"
                reason = f"kanban gave up after {trigger}: {gave_up.get('error') or _blocked_reason(full)}"
                ledger.finish_job(
                    self.conn,
                    job["id"],
                    "timeout" if trigger == "timed_out" else "error",
                    "kanban",
                    reason,
                )
                self._next_attempt(goal, reason)
                return True
            reason = _blocked_reason(full)
            ledger.finish_job(self.conn, job["id"], "error", "kanban", reason)
            ledger.finish_goal(
                self.conn, goal["id"], "error", f"kanban {status}: {reason}"
            )
        else:
            return False  # still queued: kanban dispatches it
        return True

    # -- free quota ------------------------------------------------------------

    def _quota_reset(self, goal: dict) -> float | None:
        text = kanban.log(
            goal["target"],
            goal["current_task"],
            tail=LOG_TAIL_BYTES,
            runner=self.runner,
        )
        return quota_reset(text, self.now())

    def _quota_wait(self, goal: dict, until: float) -> bool:
        """Not an attempt: pause the target until the quota resets, then unblock."""
        if until <= self.now():
            kanban.unblock(
                goal["target"],
                goal["current_task"],
                reason="free quota has reset",
                runner=self.runner,
            )
            return True
        pause = ledger.active_pause(self.conn, goal["target"], self.now())
        if pause is None or datetime.fromisoformat(pause["until"]).timestamp() < until:
            ledger.add_pause(
                self.conn,
                goal["target"],
                until,
                f"free quota (task {goal['current_task']}, goal {goal['id']})",
            )
        return False

    # -- attempts --------------------------------------------------------------

    def _role(self, name: str) -> dict:
        role = roles.load(self.root, name, self.cat)
        if role["runs_on"]["harness"] != "hermes":
            raise RunError(f"role {name} does not run on hermes workers")
        if role["status"] != "active":
            raise RunError(f"role {name} is not active")
        return role

    def _choice(self, role: dict, attempt: int) -> catalog.ModelChoice:
        spec = roles.candidate(role, attempt)
        if not spec:
            raise RunError(f"role {role['name']} has no model candidates")
        return catalog.resolve(spec, self.cat)

    def _owned(self, goal: dict) -> list[str] | None:
        if not goal.get("part_id"):
            return None
        part = ledger.get_part(self.conn, goal["part_id"])
        return list(part["owned_paths"]) if part and part["owned_paths"] else None

    def free_attempt(
        self,
        goal: dict,
        attempt: int,
        failure: str | None = None,
        *,
        parents: tuple[str, ...] = (),
    ) -> str:
        """A kanban task for attempt ``attempt`` on the goal's branch; returns its id."""
        role = self._role(goal["role"])
        if not profiles.profile_dir(goal["role"]).exists():
            raise RunError(
                f"missing Hermes profile {goal['role']}; run `team roles sync` first"
            )
        choice = self._choice(role, attempt)
        grade.ensure_worktree(
            self._repo(goal), goal["worktree"], goal["branch"], runner=self.runner
        )
        title = goal["goal"].splitlines()[0][:80]
        if attempt > 1:
            title += f" (attempt {attempt})"
        task = kanban.create_task(
            goal["target"],
            title,
            brief(goal, attempt, failure, self._owned(goal)),
            assignee=goal["role"],
            model=choice,
            workspace=f"worktree:{goal['worktree']}",
            branch=goal["branch"],
            max_runtime_s=role["budget"]["run_budget_s"],
            max_retries=1,
            skills=tuple(role["skills"]),
            parents=parents,
            runner=self.runner,
        )
        job = self._start_job(goal, attempt, task["id"], choice)
        ledger.update_goal(
            self.conn, goal["id"], attempt=attempt, current_task=task["id"], job_id=job
        )
        self.log(
            f"goal {goal['id']}: attempt {attempt} on {choice.model} ({choice.why})"
        )
        return task["id"]

    def _watch(self, goal: dict, job: dict, full: dict) -> bool:
        """Kanban neither enforces --max-runtime nor notices a dead worker."""
        runs = full.get("runs") or []
        if not runs:
            return False
        run = runs[-1]
        pid, started = run.get("worker_pid"), run.get("started_at")
        budget = self._role(goal["role"])["budget"]["run_budget_s"]
        alive = pid is not None and self.pid_alive(int(pid))
        reason = None
        if pid is not None and not alive:
            reason = f"worker pid {pid} is gone"
        elif started is not None and self.now() - float(started) > budget + GRACE_S:
            reason = f"run {run.get('id')} is {int(self.now() - float(started))}s old, over its {budget}s budget + {GRACE_S}s"
        if reason is None:
            return False
        if alive:
            self.kill(int(pid))
            reason += f"; terminated pid {pid}"
        until = None if alive else self._quota_reset(goal)
        if until is not None:
            kanban.block(
                goal["target"],
                goal["current_task"],
                f"{reason}; free quota",
                runner=self.runner,
            )
            self._quota_wait(goal, until)
            return True
        kanban.block(goal["target"], goal["current_task"], reason, runner=self.runner)
        ledger.finish_job(self.conn, job["id"], "error", "team-run", reason)
        self._next_attempt(goal, reason)
        return True

    def _grade_task(self, goal: dict, job: dict, status: str) -> None:
        """The worker asked for review (or marked done): attach its calls, check the gate, grade."""
        task_id = goal["current_task"]
        ledger.attach_calls(self.conn, task_id, job["id"])
        violations = ledger.violations_for(self.conn, task_id)
        if violations:
            reason = "free gate: " + "; ".join(v["detail"] for v in violations)
            ledger.finish_job(self.conn, job["id"], "error", "free-gate", reason)
            self._block(goal, reason)
            ledger.finish_goal(self.conn, goal["id"], "error", reason)
            return
        ok, detail = self.grade(goal)
        ledger.finish_job(
            self.conn, job["id"], "pass" if ok else "fail", "check", detail
        )
        if ok:
            if status == "review":
                kanban.complete(
                    goal["target"],
                    task_id,
                    result=f"passed: {detail[-200:]}",
                    runner=self.runner,
                )
            self._land(goal)
            return
        kanban.comment(
            goal["target"], task_id, f"FAILED: {_tail(detail)}", runner=self.runner
        )
        goal = ledger.get_goal(self.conn, goal["id"])
        role = self._role(goal["role"])
        nxt = goal["attempt"] + 1
        if nxt <= role["escalation"]["after_failures"] and status == "review":
            # Same task, reopened: the worker sees the failure in its comments.
            choice = self._choice(role, nxt)
            kanban.set_model(goal["target"], task_id, choice, runner=self.runner)
            kanban.reopen_review(
                goal["target"],
                task_id,
                reason=f"attempt {nxt} on {choice.model}",
                runner=self.runner,
            )
            new_job = self._start_job(goal, nxt, task_id, choice)
            ledger.update_goal(self.conn, goal["id"], attempt=nxt, job_id=new_job)
            self.log(
                f"goal {goal['id']}: attempt {nxt} on {choice.model} ({choice.why})"
            )
        else:
            self._next_attempt(goal, detail)

    # -- escalation ------------------------------------------------------------

    def _next_attempt(self, goal: dict, failure: str | None) -> None:
        """Free attempts up to the role's after_failures, then one on Claude, then Eli."""
        goal = ledger.get_goal(self.conn, goal["id"])
        role = self._role(goal["role"])
        nxt = goal["attempt"] + 1
        if nxt <= role["escalation"]["after_failures"]:
            self.free_attempt(goal, nxt, failure)
        elif (
            nxt == role["escalation"]["after_failures"] + 1
            and self.cfg["escalation.claude"]
        ):
            self._claude_attempt(goal, nxt, failure)
        else:
            self._give_up(goal, f"all {goal['attempt']} attempts failed", failure)

    def _claude_attempt(self, goal: dict, attempt: int, failure: str | None) -> None:
        grade.ensure_worktree(
            self._repo(goal), goal["worktree"], goal["branch"], runner=self.runner
        )
        choice = catalog.ModelChoice(
            "claude", "subscription", "claude", "escalation after free attempts"
        )
        job = self._start_job(goal, attempt, "claude:", choice)
        ledger.update_goal(self.conn, goal["id"], attempt=attempt, job_id=job)
        decision_file = (
            claude_roles.claude_dir()
            / "decisions"
            / f"goal-{goal['id']}-{attempt}.json"
        )
        decision_file.parent.mkdir(parents=True, exist_ok=True)
        decision_file.unlink(missing_ok=True)
        owned = self._owned(goal)
        result = claude_run.run(
            goal["worktree"],
            brief(goal, attempt, failure, owned, claude=True),
            max_turns=self.cfg["escalation.claude_max_turns"],
            timeout_s=self.cfg["escalation.claude_timeout_s"],
            runner=self.runner,
            role=goal["role"],
            root=self.root,
            check=goal["check_cmd"],
            allowed_files=owned,
            decision_file=decision_file,
        )
        ledger.set_task_ref(
            self.conn, job, f"claude:{result.get('session_id') or 'unknown'}"
        )
        usage = result.get("usage") or {}
        ledger.record_call(
            self.conn,
            job,
            "subscription",
            "claude",
            goal["role"],
            usage.get("input_tokens"),
            usage.get("output_tokens"),
            result.get("duration_ms"),
            bool(result["ok"]),
            None if result["ok"] else _tail(str(result["result"]), 500),
            session_id=result.get("session_id"),
            profile=goal["role"],
            response_model="claude",
        )
        asked = self._claude_decision(goal, decision_file)
        if asked:
            ledger.finish_job(
                self.conn, job, "fail", "claude", f"asked Eli: decision {asked}"
            )
            self._give_up(
                goal, f"Claude needs a decision from Eli (decision {asked})", None
            )
            return
        if not result["ok"]:
            detail = f"claude run failed: {result['result']}"
            ledger.finish_job(self.conn, job, "error", "claude", detail)
            self._give_up(goal, "the Claude attempt failed too", detail)
            return
        ok, detail = self.grade(goal)
        ledger.finish_job(self.conn, job, "pass" if ok else "fail", "check", detail)
        if ok:
            if goal.get("current_task"):
                kanban.complete(
                    goal["target"],
                    goal["current_task"],
                    result=f"claude passed: {detail[-200:]}",
                    runner=self.runner,
                )
            self._land(goal)
        else:
            self._give_up(goal, "the Claude attempt failed too", detail)

    def _claude_decision(self, goal: dict, path: Path) -> int | None:
        """A decision Claude wrote to $TEAM_DECISION_FILE goes to Eli's inbox."""
        if not path.exists():
            return None
        try:
            d = json.loads(path.read_text())
        except ValueError:
            d = {"question": path.read_text().strip()}
        if not isinstance(d, dict) or not d.get("question"):
            return None
        return ledger.create_decision(
            self.conn,
            goal["target"],
            str(d["question"]),
            str(d.get("context") or ""),
            [str(o) for o in d.get("options") or []],
            str(d.get("recommendation") or ""),
            goal_id=goal["id"],
            task_id=goal.get("current_task"),
            source="claude",
        )

    def _give_up(self, goal: dict, why: str, detail: str | None) -> None:
        """Stop working on the goal; block its task and ask Eli."""
        reason = (
            f"{why}. Branch {goal['branch']} is kept.\n{_tail(detail, 1500)}".strip()
        )
        if self.cfg["ask.block_after_claude_fails"]:
            self._block(goal, reason)
            ledger.create_decision(
                self.conn,
                goal["target"],
                f"Goal {goal['id']} stopped: {why}",
                reason,
                ["retry with a new brief", "drop the goal", "take it over by hand"],
                "look at the branch first",
                goal_id=goal["id"],
                task_id=goal.get("current_task"),
                source="run",
            )
        ledger.finish_goal(self.conn, goal["id"], "fail", reason)

    def _block(self, goal: dict, reason: str) -> None:
        if not goal.get("current_task"):
            return
        try:
            task = kanban.show(goal["target"], goal["current_task"], runner=self.runner)
            if task.get("status") in {"done", "archived", "blocked"}:
                return
            if task.get("status") == "review":
                kanban.reopen_review(
                    goal["target"],
                    goal["current_task"],
                    reason=reason,
                    runner=self.runner,
                )
            kanban.block(
                goal["target"], goal["current_task"], reason, runner=self.runner
            )
        except kanban.KanbanError as e:
            self.log(
                f"goal {goal['id']}: could not block {goal['current_task']}: {e.stderr}"
            )

    # -- grading and landing ---------------------------------------------------

    def _repo(self, goal: dict) -> Path:
        return targets.repo_path(goal["target"])

    def grade(self, goal: dict) -> tuple[bool, str]:
        """Merge base into the branch, enforce ownership, run the check. (passed, output)."""
        repo, branch, wt, base = (
            self._repo(goal),
            goal["branch"],
            Path(goal["worktree"]),
            goal.get("base") or "main",
        )
        grade.ensure_worktree(repo, wt, branch, runner=self.runner)
        if goal.get("needs_commits", 1) and not grade.has_commits(
            repo, branch, base, runner=self.runner
        ):
            return False, "no commits"
        if not grade.is_clean(wt, runner=self.runner):
            return False, "uncommitted changes in the worktree"
        merged, conflicts = grade.merge_base_into(wt, base, runner=self.runner)
        if not merged:
            return False, f"merge conflict with {base}:\n" + "\n".join(conflicts)
        owned = self._owned(goal)
        if owned:
            outside = grade.outside_owned(
                grade.changed_files(repo, branch, base, runner=self.runner), owned
            )
            if outside:
                return False, "changes outside owned files: " + ", ".join(outside)
        ok, output = grade.run_check(
            wt,
            goal["check_cmd"],
            timeout_s=self.cfg["grading.check_timeout_s"],
            runner=self.runner,
        )
        if not ok and not output:
            output = f"`{goal['check_cmd']}` failed with no output"
        return ok, output

    def _land(self, goal: dict) -> None:
        repo = self._repo(goal)
        first = goal["goal"].splitlines()[0][:60]
        base = goal.get("base") or "main"
        try:
            sha = grade.land(
                repo,
                goal["branch"],
                f"Merge {goal['branch']}: {first} (goal {goal['id']})",
                base,
                runner=self.runner,
            )
        except grade.GradeError as e:
            reason = f"passed its check but could not land: {e}. Branch {goal['branch']} is kept."
            self._block(goal, reason)
            ledger.finish_goal(self.conn, goal["id"], "error", reason)
            return
        try:
            grade.remove_worktree(
                repo, goal["worktree"], goal["branch"], runner=self.runner
            )
        except grade.GradeError as e:
            self.log(f"goal {goal['id']}: landed, but {e}")
        if goal.get("part_id"):
            ledger.set_part_stage(self.conn, goal["part_id"], "done", status="landed")
        ledger.finish_goal(self.conn, goal["id"], "pass", f"landed {sha}")

    def _start_job(
        self, goal: dict, attempt: int, task_ref: str, choice: catalog.ModelChoice
    ) -> int:
        return ledger.start_job(
            self.conn,
            goal["target"],
            grade.head(self._repo(goal), runner=self.runner),
            goal["role"],
            roles.version(self.root, goal["role"]),
            attempt=attempt,
            task_ref=task_ref,
            model_choice=choice.as_dict(),
            part_id=goal.get("part_id"),
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
    parents: tuple[str, ...] = (),
    part_id: int | None = None,
    base: str = "main",
    cat: dict | None = None,
    log: Callable[[str], None] = _log,
) -> dict:
    """``team goal``: a worktree, a kanban task, a ledger job and a goals row."""
    record = targets.require(target)
    loop = Loop(root, conn, runner=runner, cat=cat, log=log, preflight=None)
    loop._role(role)
    check_cmd = check if check is not None else settings.check_for(root, target)
    repo = Path(record["path"])
    kanban.ensure_board(target, runner=runner)
    slug = _free_slug(repo, target, slugify(goal), runner)
    path, branch = grade.create_worktree(repo, target, slug, base=base, runner=runner)
    goal_id = ledger.create_goal(
        conn,
        target,
        goal,
        role,
        branch,
        str(path),
        check_cmd,
        part_id=part_id,
        base=base,
    )
    try:
        loop.free_attempt(ledger.get_goal(conn, goal_id), 1, parents=parents)
    except Exception as e:
        ledger.finish_goal(
            conn, goal_id, "error", f"could not create the first task: {e}"
        )
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
