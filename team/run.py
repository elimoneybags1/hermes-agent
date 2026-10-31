"""``team goal`` and ``team run``: the autonomous loop on Hermes kanban.

A goal is one branch and one worktree. Each attempt is a ledger job: free
attempts are kanban tasks on that branch (one task for the goal's whole
life, reopened from ``review`` on a failure), the last one may be Claude
through LiteLLM on Eli's subscription. Every tick, for every enabled target: run the
preflight (gate audit, catalog check, spend guard), let kanban dispatch, then
step each goal Team tracks. What Team tracks lives in the ledger's ``goals``
table, so ``team run`` can stop and restart at any point.

With ``lead.enabled``, ``team goal`` asks the lead first (``team.lead``): it
splits the goal into parts, one goal row each, and writes every part's work
order. A worker's task body is then the rendered order, and its toolsets,
skills and first model are the order's. A part that depends on others waits
until they have landed, then starts from the new base. After a failed check
the lead diagnoses the attempt and the retry carries its note.

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
    hermes_run,
    model_tracking,
    kanban,
    lead,
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
# A Claude 429 (subscription limit) pauses the target this long, then retries the same attempt.
CLAUDE_LIMIT = "claude usage limit"
CLAUDE_LIMIT_PAUSE_S = 3600
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
        if not blocked:
            from team import boss  # boss builds on this module

            try:
                boss.release_approved(
                    self.root,
                    self.conn,
                    runner=self.runner,
                    cat=self._cat,
                    log=self.log,
                )
            except (
                boss.BossError,
                RunError,
                kanban.KanbanError,
                grade.GradeError,
            ) as e:
                self.log(f"could not release a proposal: {e}")
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
        from team import boss  # boss builds on this module

        # A released plan whose goals have all landed is closed here, on the
        # tick that lands the last one, not only when the boss next asks for status.
        for p in boss.record_closed(self.conn, name):
            transitions.append(f"plan {p['id']} closed: {p['headline'][:60]}")
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
        if job is None and goal.get("part_id"):
            return self._start_when_ready(goal)
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
            elif claude and (job["grader_detail"] or "").startswith(CLAUDE_LIMIT):
                if (
                    ledger.active_pause(self.conn, goal["target"], self.now())
                    is not None
                ):
                    return False
                self._claude_attempt(goal, goal["attempt"], job["grader_detail"])
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
        if role["runs_on"]["harness"] not in ("hermes", "hermes-subscription"):
            raise RunError(f"role {name} does not run on hermes workers")
        if role["status"] != "active":
            raise RunError(f"role {name} is not active")
        return role

    def _choice(self, role: dict, attempt: int) -> catalog.ModelChoice:
        if role["runs_on"]["harness"] == "hermes-subscription":
            model = role["runs_on"].get("model") or "claude-opus-5-5"
            choice = catalog.resolve(f"claude-subscription:{model}", self.cat)
            return choice._replace(provider="team-subscription")
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
        order = ledger.latest_order(self.conn, goal["id"])
        if order is not None and attempt == 1:
            choice = catalog.resolve(order["order"]["model"], self.cat)
        else:
            choice = self._choice(role, attempt)
        grade.ensure_worktree(
            self._repo(goal), goal["worktree"], goal["branch"], runner=self.runner
        )
        title = goal["goal"].splitlines()[0][:80]
        if attempt > 1:
            title += f" (attempt {attempt})"
        if order is not None:
            body = lead.render(
                order["order"],
                goal,
                Path(goal["worktree"]),
                self.cfg["lead.context_bytes"],
                failure=failure,
                attempt=attempt,
            )
            skills = tuple(order["order"]["skills"])
            toolsets = tuple(order["order"]["toolsets"])
        else:
            body = brief(goal, attempt, failure, self._owned(goal))
            skills, toolsets = tuple(role["skills"]), ()
        task = kanban.create_task(
            goal["target"],
            title,
            body,
            assignee=goal["role"],
            model=choice,
            workspace=f"worktree:{goal['worktree']}",
            branch=goal["branch"],
            max_runtime_s=role["budget"]["run_budget_s"],
            max_retries=1,
            skills=skills,
            toolsets=toolsets,
            parents=parents,
            runner=self.runner,
        )
        job = self._start_job(
            goal, attempt, task["id"], choice, order["id"] if order else None
        )
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
        diagnosis = (
            self._diagnose(goal, job, detail)
            if nxt <= role["escalation"]["after_failures"]
            else None
        )
        if diagnosis is not None and diagnosis["action"] == "ask":
            self._give_up(
                goal, f"the lead asks Eli: {diagnosis['question']}", diagnosis["cause"]
            )
            return
        if diagnosis is not None and diagnosis["action"] == "escalate":
            if self.cfg["escalation.claude"]:
                self._claude_attempt(goal, nxt, detail)
            else:
                self._give_up(goal, "the lead says a free model cannot do this", detail)
            return
        if diagnosis is not None:
            kanban.comment(
                goal["target"],
                task_id,
                self._lead_comment(goal, diagnosis),
                runner=self.runner,
            )
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
            and role["runs_on"]["harness"] != "hermes-subscription"
        ):
            self._claude_attempt(goal, nxt, failure)
        else:
            self._give_up(goal, f"all {goal['attempt']} attempts failed", failure)

    def _claude_attempt(self, goal: dict, attempt: int, failure: str | None) -> None:
        grade.ensure_worktree(
            self._repo(goal), goal["worktree"], goal["branch"], runner=self.runner
        )
        model = (
            roles.load(self.root, goal["role"]).get("escalation", {}).get("model")
            or hermes_run.RESCUE_MODEL
        )
        choice = catalog.ModelChoice(
            model, "team-subscription", model, "escalation after free attempts"
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
        order = ledger.latest_order(self.conn, goal["id"])
        result = hermes_run.run(
            goal["worktree"],
            lead.render(
                order["order"],
                goal,
                Path(goal["worktree"]),
                self.cfg["lead.context_bytes"],
                failure=failure,
                attempt=attempt,
                claude=True,
            )
            if order is not None
            else brief(goal, attempt, failure, owned, claude=True),
            max_turns=self.cfg["escalation.claude_max_turns"],
            timeout_s=self.cfg["escalation.claude_timeout_s"],
            runner=self.runner,
            role=goal["role"],
            root=self.root,
            check=goal["check_cmd"],
            model=model,
            allowed_files=owned,
            decision_file=decision_file,
        )
        ledger.set_task_ref(
            self.conn, job, f"claude:{result.get('session_id') or 'unknown'}"
        )
        model_tracking.record_result(self.conn, job, result, goal["role"])
        asked = self._claude_decision(goal, decision_file)
        if asked:
            ledger.finish_job(
                self.conn, job, "fail", "claude", f"asked Eli: decision {asked}"
            )
            self._give_up(
                goal, f"Claude needs a decision from Eli (decision {asked})", None
            )
            return
        if not result["ok"] and claude_run.usage_limited(result):
            # Not a failed attempt: the subscription is out of quota. Pause the
            # target and keep the goal at this attempt; step() retries Claude
            # once the pause ends.
            detail = f"{CLAUDE_LIMIT}: {_tail(str(result['result']), 300)}"
            ledger.finish_job(self.conn, job, "error", "claude", detail)
            ledger.add_pause(
                self.conn, goal["target"], self.now() + CLAUDE_LIMIT_PAUSE_S, detail
            )
            self.log(
                f"goal {goal['id']}: Claude usage limit; paused {CLAUDE_LIMIT_PAUSE_S // 60} min"
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
        except grade.LandLater as e:
            # The checkout is mid-edit on these files. The goal keeps its passed
            # job, so step() comes back here every tick until the tree is clean.
            reason = f"waiting to land: {e}"
            if goal.get("detail") != reason:
                self.log(f"goal {goal['id']}: {reason}")
                ledger.update_goal(self.conn, goal["id"], detail=reason)
            return
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
        self,
        goal: dict,
        attempt: int,
        task_ref: str,
        choice: catalog.ModelChoice,
        order_id: int | None = None,
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
            order_id=order_id,
        )

    # -- the lead ----------------------------------------------------------------

    def _start_when_ready(self, goal: dict) -> bool:
        """A planned part starts once every part it depends on has landed."""
        part = ledger.get_part(self.conn, goal["part_id"])
        waiting = []
        for dep_id in part["depends_on"] if part else []:
            dep = ledger.get_part(self.conn, dep_id)
            if dep and dep["stage"] == "done":
                continue
            failed = [
                g
                for g in ledger.goals_for_part(self.conn, dep_id)
                if g["outcome"] and g["outcome"] != "pass"
            ]
            if failed:
                ledger.finish_goal(
                    self.conn,
                    goal["id"],
                    "fail",
                    f"part {dep_id} it depends on did not land: {failed[-1]['detail']}",
                )
                return True
            waiting.append(dep_id)
        if waiting:
            return False
        repo, base = self._repo(goal), goal.get("base") or "main"
        if not grade.branch_exists(repo, goal["branch"], self.runner):
            grade.create_worktree(
                repo,
                goal["target"],
                goal["branch"].removeprefix("team/"),
                base=base,
                runner=self.runner,
            )
        self.free_attempt(goal, 1)
        return True

    def _lead_job(self, target: str, commit: str, why: str) -> int:
        model = roles.load(self.root, lead.ROLE)["runs_on"].get("model") or hermes_run.LEAD_MODEL
        return ledger.start_job(
            self.conn,
            target,
            commit,
            lead.ROLE,
            roles.version(self.root, lead.ROLE),
            task_ref="claude:",
            model_choice=catalog.ModelChoice(
                model, "team-subscription", model, why
            ).as_dict(),
        )

    def _lead_call(self, job_id: int, result: dict) -> None:
        ledger.set_task_ref(
            self.conn, job_id, f"claude:{result.get('session_id') or 'unknown'}"
        )
        model_tracking.record_result(self.conn, job_id, result, lead.ROLE)

    def _diagnose(self, goal: dict, job: dict, failure: str) -> dict | None:
        """The lead's reading of a failed check, or None (no order, turned off, or unavailable)."""
        order = ledger.latest_order(self.conn, goal["id"])
        if order is None or not self.cfg["lead.diagnose"]:
            return None
        repo, base = self._repo(goal), goal.get("base") or "main"
        diff = (
            grade._git(
                repo, "diff", f"{base}...{goal['branch']}", runner=self.runner
            ).stdout
            or "(no diff)"
        )
        lead_job = self._lead_job(goal["target"], job["target_commit"], "diagnosis")
        try:
            with lead.workspace(
                repo,
                goal["target"],
                f"diagnose-{goal['id']}",
                goal["branch"],
                self.runner,
            ) as tree:
                found = lead.ask(
                    self.root,
                    tree,
                    lambda refused, _prev: lead.diagnose_prompt(
                        order["order"], failure, diff, goal["attempt"], refused=refused
                    ),
                    lead.DIAGNOSIS_SCHEMA,
                    lambda data: lead.validate_diagnosis(data, tree),
                    self.cfg,
                    self.runner,
                    on_call=lambda r: self._lead_call(lead_job, r),
                )
                if found["action"] == "retry":
                    revised = dict(order["order"], lead_note=found["note"])
                    if found["steps"]:
                        revised["steps"] = found["steps"]
                    if found["context"]:
                        revised["context"] = found["context"]
                    ledger.create_order(
                        self.conn, goal["id"], revised, goal.get("part_id"), lead_job
                    )
                    found["excerpts"] = lead.excerpts(
                        found["context"], tree, self.cfg["lead.context_bytes"]
                    )[0]
        except (lead.LeadError, grade.GradeError) as e:
            ledger.finish_job(self.conn, lead_job, "error", "lead", str(e))
            self.log(f"goal {goal['id']}: no diagnosis, retrying without it: {e}")
            return None
        ledger.finish_job(
            self.conn,
            lead_job,
            "pass",
            "lead-validate",
            f"{found['action']}: {found['cause']}",
        )
        ledger.add_diagnosis(
            self.conn,
            goal["id"],
            job["id"],
            found["cause"],
            found["note"],
            found["action"],
            lead_job,
        )
        self.log(f"goal {goal['id']}: lead says {found['action']}: {found['cause']}")
        return found

    def _lead_comment(self, goal: dict, diagnosis: dict) -> str:
        text = f"LEAD: {diagnosis['cause']}\n\n{diagnosis['note']}"
        if diagnosis["steps"]:
            text += "\n\nDo this instead:\n" + lead._bullets(
                diagnosis["steps"], numbered=True
            )
        if diagnosis.get("excerpts"):
            text += "\n\nCode you need:\n\n" + diagnosis["excerpts"]
        return text


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


def plan_goal(
    root: Path,
    conn: sqlite3.Connection,
    target: str,
    goal: str,
    *,
    check: str | None = None,
    runner: Runner = subprocess.run,
    base: str = "main",
    cat: dict | None = None,
    log: Callable[[str], None] = _log,
) -> list[dict]:
    """``team goal`` with the lead: draft the plan, then release it at once."""
    draft = draft_goal(
        root,
        conn,
        target,
        goal,
        check=check,
        runner=runner,
        base=base,
        cat=cat,
        log=log,
    )
    return release_goal(root, conn, target, draft, runner=runner, cat=cat, log=log)


def draft_goal(
    root: Path,
    conn: sqlite3.Connection,
    target: str,
    goal: str,
    *,
    check: str | None = None,
    notes: str = "",
    runner: Runner = subprocess.run,
    base: str = "main",
    cat: dict | None = None,
    log: Callable[[str], None] = _log,
    plan_context: dict | None = None,
) -> dict:
    """Ask the lead for a validated plan; nothing is created on the board.

    ``notes`` is what the boss knows that the repo does not say (decisions,
    the larger plan this goal belongs to); the lead plans with it.
    ``plan_context`` names the plan's other goals: ``before`` (drafted, and
    landed by the time this one runs) and ``alongside`` (running at once).
    Returns ``{goal, check, base, summary, parts, lead_job}``.
    """
    record = targets.require(target)
    loop = Loop(root, conn, runner=runner, cat=cat, log=log, preflight=None)
    check_cmd = check if check is not None else settings.check_for(root, target)
    repo = Path(record["path"])
    menu = lead.menu(root, loop.cat)
    if not menu:
        raise RunError("no active hermes role for the lead to assign")
    commit = grade._must(
        grade._git(repo, "rev-parse", base, runner=runner), "git rev-parse failed"
    )
    lead_job = loop._lead_job(target, commit, "plan")
    try:
        with lead.workspace(
            repo, target, f"plan-{lead_job}", base, runner
        ) as tree:
            summary, parts = lead.ask(
                root,
                tree,
                lambda refused, previous: lead.plan_prompt(
                    goal,
                    check_cmd,
                    menu,
                    loop.cfg["lead.max_parts"],
                    notes=notes,
                    refused=refused,
                    previous=previous,
                    plan_context=plan_context,
                ),
                lead.PLAN_SCHEMA,
                lambda data: _with_summary(data, tree, menu, loop),
                loop.cfg,
                runner,
                on_call=lambda r: loop._lead_call(lead_job, r),
            )
    except (lead.LeadError, grade.GradeError) as e:
        ledger.finish_job(conn, lead_job, "error", "lead", str(e))
        hint = (
            " (Claude usage limit; try later, or `team goal --no-lead`)"
            if getattr(e, "usage_limited", False)
            else ""
        )
        raise RunError(f"the lead could not plan this goal: {e}{hint}") from e
    ledger.finish_job(
        conn, lead_job, "pass", "lead-validate", f"{len(parts)} parts: {summary}"[:500]
    )
    return {
        "goal": goal,
        "check": check_cmd,
        "base": base,
        "summary": summary,
        "parts": parts,
        "lead_job": lead_job,
    }


def release_goal(
    root: Path,
    conn: sqlite3.Connection,
    target: str,
    draft: dict,
    *,
    plan_id: int | None = None,
    after: tuple[int, ...] = (),
    runner: Runner = subprocess.run,
    cat: dict | None = None,
    log: Callable[[str], None] = _log,
) -> list[dict]:
    """Turn a drafted plan into goal rows, one per part.

    Every part also waits for the part ids in ``after`` (another goal's parts).
    Parts with nothing to wait for get their worktree and first task now; the
    rest start in ``team run`` once what they depend on has landed.
    """
    record = targets.require(target)
    loop = Loop(root, conn, runner=runner, cat=cat, log=log, preflight=None)
    repo, goal, base = Path(record["path"]), draft["goal"], draft["base"]
    kanban.ensure_board(target, runner=runner)
    slug = _free_slug(repo, target, slugify(goal), runner)
    if plan_id is None:
        plan_id = ledger.create_plan(
            conn,
            target,
            goal.splitlines()[0][:120],
            summary=draft["summary"],
            status="active",
        )
    parts = draft["parts"]
    part_ids: dict[str, int] = {}
    goals = []
    for n, part in enumerate(parts, 1):
        pslug = slug if len(parts) == 1 else f"{slug[:34]}-p{n}"
        deps = [part_ids[d] for d in part["depends_on"]] + list(after)
        part_ids[part["key"]] = ledger.create_part(
            conn,
            plan_id,
            target,
            part["title"],
            stage="implement",
            branch=f"team/{pslug}",
            depends_on=deps,
            owned_paths=part["owned_paths"],
        )
        goal_id = ledger.create_goal(
            conn,
            target,
            part["title"] if len(parts) > 1 else goal,
            part["role"],
            f"team/{pslug}",
            str(grade.worktree_path(target, pslug)),
            draft["check"],
            part_id=part_ids[part["key"]],
            base=base,
        )
        ledger.create_order(
            conn,
            goal_id,
            dict(part, goal=goal),
            part_ids[part["key"]],
            draft["lead_job"],
        )
        if not deps:
            try:
                grade.create_worktree(repo, target, pslug, base=base, runner=runner)
                loop.free_attempt(ledger.get_goal(conn, goal_id), 1)
            except Exception as e:
                ledger.finish_goal(
                    conn, goal_id, "error", f"could not create the first task: {e}"
                )
                raise
        goals.append(ledger.get_goal(conn, goal_id))
    return goals


def _with_summary(data: Any, tree: Path, menu: dict, loop: Loop) -> tuple:
    """``validate_plan`` for ``lead.ask``: ((summary, parts), refusals)."""
    parts, errors = lead.validate_plan(
        data, tree, menu, loop.cfg["lead.max_parts"], loop.cat
    )
    summary = str((data or {}).get("summary") or "") if isinstance(data, dict) else ""
    return ((summary, parts), errors)


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
