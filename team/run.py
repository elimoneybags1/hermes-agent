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

import fcntl
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
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from team import (
    catalog,
    claude_roles,
    claude_run,
    gates,
    grade,
    hermes_run,
    model_tracking,
    kanban,
    lead,
    ledger,
    profiles,
    roles,
    settings,
    stages,
    sweep,
    targets,
)
from team.review import backend

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
ASKED = "asked Eli: decision "  # a Claude attempt's job detail while the goal waits for Eli's answer
CLAUDE_LIMIT_PAUSE_S = 3600
QUOTA_END_CHARS = 2000
# A sweep attempt's goal detail while it waits for the tick that runs it; the previous
# attempt's failure follows on the next lines.
SWEEP_QUEUED = "sweep attempt queued"


class RunError(RuntimeError):
    """A goal that cannot be started or carried on, with the reason why."""


def run_dir() -> Path:
    return ledger.state_dir() / "run"


def acquire_lock(name: str):
    """Hold ``$TEAM_STATE/run/<name>.lock`` for this process (one ``team run`` per target).

    Returns the open file; the lock lives as long as it does. Raises RunError when another
    process holds it, so two loops never grade and land the same target at once.
    """
    path = run_dir() / f"{name}.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    f = open(path, "a+")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        held = f.read().strip() if f.seek(0) is None else ""
        f.close()
        raise RunError(f"another team run holds {path}" + (f" (pid {held})" if held else "")) from None
    f.seek(0)
    f.truncate()
    f.write(str(os.getpid()))
    f.flush()
    return f


@contextmanager
def check_lock():
    """One check at a time across every ``team run`` process on this machine.

    Checks are the expensive, port- and CPU-hungry part; serialising them keeps parallel
    target loops from starving each other and keeps grade-then-land a merge queue.
    """
    path = run_dir() / "check.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def says_land(answer: str | None) -> bool:
    """Eli's answer to a 'land it?' decision: the first option, or a plain yes."""
    text = (answer or "").strip().lower()
    return text.startswith(("land", "yes", "merge", "1", "ok"))


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


def _out(line: str) -> None:
    # Flushed: a loop whose stdout is a log file would otherwise show nothing for hours.
    print(line, flush=True)


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
    answer: str | None = None,
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
    if answer:
        text += f"\n\nEli answered the question this goal stopped on:\n{answer}"
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
        out: Callable[[str], None] = _out,
        complete: Callable[..., dict] = sweep.complete,
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
        self.complete = complete  # one tool-free gateway call (team.sweep.complete); faked in tests
        self.cfg = settings.load(self.root)

    @property
    def cat(self) -> dict:
        if self._cat is None:
            self._cat = catalog.load()
        return self._cat

    # -- ticks ---------------------------------------------------------------

    def tick(self, only: tuple[str, ...] | None = None, boss_work: bool = True) -> None:
        """One pass: preflight, the boss's releases (``boss_work``), then every enabled target
        (or just ``only``)."""
        self.cfg = settings.load(self.root)
        blocked = self.preflight(self.root, self.runner) if self.preflight else []
        for reason in blocked:
            self.log(f"not dispatching: {reason}")
        if not blocked and boss_work:
            from team import boss  # boss builds on this module

            try:
                boss.apply_suggestions(self.root, self.conn, log=self.log)
                boss.release_approved(
                    self.root,
                    self.conn,
                    runner=self.runner,
                    cat=self._cat,
                    log=self.log,
                )
                boss.settle_reviews(
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
            if only is not None and target["name"] not in only:
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
        if dispatch:
            from team import routines

            try:
                routines.tick(self.root, self.conn, name, self.now(), runner=self.runner,
                              cat=self._cat, log=self.log)
            except ValueError as e:
                self.log(f"{name}: invalid routine: {e}")
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
        swept = (job["task_ref"] or "").startswith(sweep.TASK_REF)
        if job["ended"] is None and swept:
            return self._run_sweep(goal, job)
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
            if goal.get("harness_id"):
                self._finish_replay(goal, job)
            elif job["outcome"] == "pass":
                self._advance(goal)
            elif claude and (job["grader_detail"] or "").startswith(ASKED):
                return self._after_decision(goal, job)
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
            elif swept:
                self._after_failed_sweep(goal, job)
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
                if trigger == "timed_out" and self._left_work(goal):
                    # Out of turns with work on the branch: grade what is there
                    # (grade() commits leftovers) before spending another attempt.
                    self.log(f"goal {goal['id']}: {reason}; grading the work it left")
                    self._grade_task(goal, job, "timed_out")
                    return True
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

    def _role(self, name: str, target: str | None = None) -> dict:
        role = roles.load(self.root, name, self.cat, target=target)
        if role["runs_on"]["harness"] not in ("hermes", "hermes-subscription"):
            raise RunError(f"role {name} does not run on hermes workers")
        if role["status"] not in roles.WORKING:
            raise RunError(f"role {name} is not active")
        return role

    def _choice(self, role: dict, attempt: int, target: str | None = None) -> catalog.ModelChoice:
        if role["runs_on"]["harness"] == "hermes-subscription":
            model = role["runs_on"].get("model") or "claude-opus-5-5"
            choice = catalog.resolve(f"claude-subscription:{model}", self.cat)
            return choice._replace(provider="team-subscription")
        from team import models

        return models.choose(self.conn, role, attempt, self.cat, target)

    def _owned(self, goal: dict) -> list[str] | None:
        if not goal.get("part_id"):
            return None
        part = ledger.get_part(self.conn, goal["part_id"])
        return list(part["owned_paths"]) if part and part["owned_paths"] else None

    # -- the pipeline (team.gates) ---------------------------------------------

    def _active_roles(self) -> set[str]:
        out = set()
        for name in roles.names(self.root):
            try:
                r = roles.load(self.root, name, self._cat)
            except roles.RoleError:
                continue
            if r["status"] in roles.WORKING and r["runs_on"]["harness"] in ("hermes", "hermes-subscription"):
                out.add(name)
        return out

    def _stages(self, goal: dict) -> list[gates.Stage]:
        """The goal's pipeline as fixed when it was created; computed only for goals older than that column."""
        if goal.get("stages"):
            return gates.stages_named(json.loads(goal["stages"]))
        return gates.stages_for(goal.get("type") or gates.DEFAULT_TYPE, self._active_roles())

    def _stage(self, goal: dict) -> gates.Stage:
        stages_ = self._stages(goal)
        return stages_[min(int(goal.get("stage_idx") or 0), len(stages_) - 1)]

    def _diff(self, goal: dict) -> str:
        base = goal.get("base") or "main"
        return grade._git(self._repo(goal), "diff", f"{base}...{goal['branch']}", runner=self.runner).stdout or ""

    def _branch_head(self, goal: dict) -> str:
        return grade._must(grade._git(self._repo(goal), "rev-parse", goal["branch"], runner=self.runner), "git rev-parse failed")

    def _brief_for(self, goal: dict, attempt: int, failure: str | None, *, claude: bool = False, answer: str | None = None) -> str:
        """The task body for the goal's current stage: the builder's order, or the stage's own brief."""
        stage = self._stage(goal)
        order = ledger.latest_order(self.conn, goal["id"])
        o = order["order"] if order else None
        if stage.gate == "check":
            if order is not None:
                return lead.render(
                    o, goal, Path(goal["worktree"]), self.cfg["lead.context_bytes"],
                    failure=failure, attempt=attempt, claude=claude, answer=answer,
                )
            return brief(goal, attempt, failure, self._owned(goal), claude=claude, answer=answer)
        if stage.gate == "onboard":
            return stages.onboard_brief(goal, targets.require(goal["target"]), failure=failure, attempt=attempt, claude=claude)
        if stage.gate == "tests_only":
            return stages.tests_brief(goal, o, self._owned(goal), failure=failure, attempt=attempt, claude=claude)
        diff = self._diff(goal)
        if stage.gate == "review":
            prev = ledger.get_job(self.conn, goal["job_id"]) if goal.get("job_id") else None
            verified = prev["grader_detail"] if prev and prev.get("grader") == "verify" else None
            return stages.review_brief(goal, o, diff, verified, failure=failure, attempt=attempt, claude=claude)
        if stage.gate == "security":
            return stages.security_brief(goal, o, diff, failure=failure, attempt=attempt, claude=claude)
        raise RunError(f"stage {stage.name!r} has no brief")

    def _role_for_stage(self, stage: gates.Stage, order: dict | None) -> str | None:
        """The worker for a stage: the lead's pick when it has the stage's gate, else the default."""
        if stage.role is None:
            return None
        wanted = (order or {}).get("order", {}).get("role") if order else None
        if wanted and wanted != stage.role:
            try:
                r = self._role(wanted)
            except (roles.RoleError, RunError):
                return stage.role
            if r.get("gate") == stage.gate and r["status"] in roles.WORKING:
                return wanted
        return stage.role

    def gate(self, goal: dict, text: str | None = None) -> tuple[bool, str]:
        """Run the current stage's gate on the goal's branch: (passed, what it found).

        ``text`` is a Claude attempt's final answer; a kanban worker's answer is read from the
        task instead. A detail starting with ``gates.SEND_BACK`` means: not this stage's
        fault, the builder must fix what the reviewer found.
        """
        return self.run_gate(self._stage(goal).gate, goal, text=text)

    def run_gate(self, name: str, goal: dict, text: str | None = None) -> tuple[bool, str]:
        if name == "check":
            return self.grade(goal)
        if name == "verify":
            return self.verify(goal)
        if name == "tests_only":
            return self.grade_tests_only(goal)
        if name == "review":
            return self.grade_review(goal, text)
        if name == "security":
            return self.grade_security(goal, text)
        if name == "onboard":
            return self.grade_onboard(goal, text)
        raise RunError(f"gate {name!r} has no code yet; a role may not use it")

    ONBOARDING = "onboarding proposal: "

    def grade_onboard(self, goal: dict, text: str | None = None) -> tuple[bool, str]:
        """The onboarding gate: the proposed check passes as the worktree stands, fails after the
        proposed break, and passes again once the break is undone. The proposal then waits for Eli."""
        repo, wt, base = self._repo(goal), Path(goal["worktree"]), goal.get("base") or "main"
        grade.ensure_worktree(repo, wt, goal["branch"], runner=self.runner)
        grade.commit_leftovers(
            wt, f"team: attempt {goal.get('attempt', 1)} left these changes uncommitted; "
            "Team committed them before grading", None, runner=self.runner,
        )
        if not grade.is_clean(wt, runner=self.runner):
            return False, "uncommitted changes in the worktree"
        merged, conflicts = grade.merge_base_into(wt, base, runner=self.runner)
        if not merged:
            return False, f"merge conflict with {base}:\n" + "\n".join(conflicts)
        proposal, errors = stages.parse_onboarding(self._answer(goal, text))
        if proposal is None:
            return False, "onboarding answer refused: " + "; ".join(errors)
        if proposal["setup"]:
            ok, out = self._run_check(wt, proposal["setup"])
            if not ok:
                return False, f"the setup command failed (`{proposal['setup']}`):\n{out}"
        ok, out = self._run_check(wt, proposal["check"])
        if not ok:
            return False, f"the proposed check fails on the worktree as it stands (`{proposal['check']}`):\n{out}"
        try:
            self._run_check(wt, proposal["break_cmd"])
            broken_ok, _ = self._run_check(wt, proposal["check"])
        finally:
            grade._git(wt, "checkout", "--", ".", runner=self.runner)
            grade._git(wt, "clean", "-fdq", runner=self.runner)
        if broken_ok:
            return False, (
                f"the check still passes after the break command (`{proposal['break_cmd']}`), so it "
                f"proves nothing about `{proposal['check']}`; propose a break the check catches, or a "
                "check that catches a break"
            )
        ok, out = self._run_check(wt, proposal["check"])
        if not ok:
            return False, f"the check does not pass again after undoing the break; the break was not undoable:\n{out}"
        detail = self.ONBOARDING + json.dumps(proposal)
        ledger.update_goal(self.conn, goal["id"], detail=detail)
        return True, detail + f"\ncheck passed, failed after `{proposal['break_cmd']}`, passed again"

    def _apply_onboarding(self, goal: dict) -> list[str]:
        """Eli landed an onboarding goal: its proposal becomes the target's settings."""
        detail = goal.get("detail") or ""
        at = detail.find(self.ONBOARDING)
        if at < 0:
            return []
        proposal = json.loads(detail[at + len(self.ONBOARDING):].split("\n", 1)[0])
        applied = []
        for key in ("check", "test_runner", "e2e", "setup"):
            value = proposal.get(key) or None
            if value or targets.require(goal["target"]).get(key):
                targets.set_field(goal["target"], key, value)
                applied.append(f"{key} = {value!r}")
        return applied

    def _unchanged_since_stage(self, goal: dict) -> str | None:
        """Why a read-only stage's branch is not as it was when the stage began, or None."""
        repo, wt = self._repo(goal), Path(goal["worktree"])
        grade.ensure_worktree(repo, wt, goal["branch"], runner=self.runner)
        if goal.get("stage_sha") and self._branch_head(goal) != goal["stage_sha"]:
            return "the branch changed during this stage; a review edits nothing"
        if not grade.is_clean(wt, runner=self.runner):
            return "uncommitted changes in the worktree; a review edits nothing"
        return None

    def _answer(self, goal: dict, text: str | None) -> str | None:
        if text is not None:
            return text
        full = self._show(goal)
        return (full or {}).get("latest_summary")

    def grade_review(self, goal: dict, text: str | None = None) -> tuple[bool, str]:
        """The review gate: an unchanged branch and a verdict whose findings cite the diff."""
        bad = self._unchanged_since_stage(goal)
        if bad:
            return False, bad
        verdict, errors = stages.parse_verdict(self._answer(goal, text), self._diff(goal))
        if verdict is None:
            return False, "review answer refused: " + "; ".join(errors)
        if verdict["verdict"] == "fix":
            return False, gates.SEND_BACK + "\n" + stages.findings_text(verdict["findings"])
        checked = "\n".join(verdict["checked"]) or "(nothing listed as checked)"
        return True, "REVIEW: land\n" + checked

    def grade_security(self, goal: dict, text: str | None = None) -> tuple[bool, str]:
        """The security gate: an unchanged branch and a well-formed report. The report goes to
        Eli with the land decision; the model decides nothing."""
        bad = self._unchanged_since_stage(goal)
        if bad:
            return False, bad
        report, errors = stages.parse_report(self._answer(goal, text), self._diff(goal))
        if report is None:
            return False, "security answer refused: " + "; ".join(errors)
        detail = "security report: " + report["summary"] + "\n" + (
            stages.findings_text(report["findings"]) or "(no findings)"
        )
        ledger.update_goal(self.conn, goal["id"], detail=detail)
        return True, detail

    def _run_check(self, worktree: Path, command: str) -> tuple[bool, str]:
        with check_lock():
            return grade.run_check(
                worktree, command, timeout_s=self.cfg["grading.check_timeout_s"], runner=self.runner
            )

    def verify(self, goal: dict) -> tuple[bool, str]:
        """The code-only stage: base merged, then the check, the acceptance command and the
        target's e2e command in the worktree. No worker, no model."""
        repo, wt, base = self._repo(goal), Path(goal["worktree"]), goal.get("base") or "main"
        grade.ensure_worktree(repo, wt, goal["branch"], runner=self.runner)
        merged, conflicts = grade.merge_base_into(wt, base, runner=self.runner)
        if not merged:
            return False, f"merge conflict with {base}:\n" + "\n".join(conflicts)
        target = targets.require(goal["target"])
        steps = (
            ("check", goal["check_cmd"]),
            ("acceptance", goal.get("acceptance_cmd")),
            ("e2e", target.get("e2e")),
        )
        out = []
        for name, cmd in steps:
            if not cmd:
                continue
            ok, output = self._run_check(wt, cmd)
            if not ok:
                out.append(f"{name} FAILED (`{cmd}`):\n{output or '(no output)'}")
                return False, "\n".join(out)
            out.append(f"{name} passed")
        return True, "; ".join(out) or "nothing to verify"

    def grade_tests_only(self, goal: dict) -> tuple[bool, str]:
        """The test-write gate: only test files changed, committed, and the acceptance command
        (the new tests) fails on the branch, so the tests are real and not yet satisfied."""
        repo, branch, wt, base = (
            self._repo(goal), goal["branch"], Path(goal["worktree"]), goal.get("base") or "main",
        )
        grade.ensure_worktree(repo, wt, branch, runner=self.runner)
        grade.commit_leftovers(
            wt, f"team: attempt {goal.get('attempt', 1)} left these changes uncommitted; "
            "Team committed them before grading", self._owned(goal), runner=self.runner,
        )
        if not grade.has_commits(repo, branch, base, runner=self.runner):
            return False, "no commits"
        if not grade.is_clean(wt, runner=self.runner):
            return False, "uncommitted changes in the worktree"
        merged, conflicts = grade.merge_base_into(wt, base, runner=self.runner)
        if not merged:
            return False, f"merge conflict with {base}:\n" + "\n".join(conflicts)
        changed = grade.changed_files(repo, branch, base, runner=self.runner)
        if not changed:
            return False, "no changes against base"
        source = [p for p in changed if not gates.is_test_path(p)]
        if source:
            return False, "a test-write stage changes only test files; also changed: " + ", ".join(source)
        owned = self._owned(goal)
        if owned:
            outside = grade.outside_owned(changed, owned)
            if outside:
                return False, "changes outside owned files: " + ", ".join(outside)
        if not goal.get("acceptance_cmd"):
            return False, "no acceptance command to run the new tests with"
        ok, output = self._run_check(wt, goal["acceptance_cmd"])
        if ok:
            return False, (
                "the new tests already pass before the part is built, so they do not test the "
                f"change (`{goal['acceptance_cmd']}`):\n{output}"
            )
        return True, f"tests written and failing as they should (`{goal['acceptance_cmd']}`):\n{output}"

    def _advance(self, goal: dict) -> None:
        """The current stage's gate passed. Run every code-only stage that follows, then start
        the next worker stage, or land when the pipeline is done. A code-only stage that fails
        sends the goal back to the worker stage before it, as a failed attempt."""
        goal = ledger.get_goal(self.conn, goal["id"])
        stages_ = self._stages(goal)
        self._save_attempts(goal)
        idx = int(goal.get("stage_idx") or 0) + 1
        while idx < len(stages_) and stages_[idx].role is None:
            stage = stages_[idx]
            ledger.update_goal(self.conn, goal["id"], stage_idx=idx)
            if goal.get("part_id"):
                ledger.set_part_stage(self.conn, goal["part_id"], stage.name)
            job = self._start_job(
                goal, goal["attempt"], f"team-run:{stage.gate}",
                catalog.ModelChoice("none", "team-run", "none", f"{stage.gate} gate: code, no model"),
                role=ledger.CODE_ROLE,
            )
            ok, detail = self.run_gate(stage.gate, goal)
            ledger.finish_job(self.conn, job, "pass" if ok else "fail", stage.gate, detail)
            ledger.update_goal(self.conn, goal["id"], job_id=job)
            if not ok:
                self._back_to_builder(ledger.get_goal(self.conn, goal["id"]), idx, detail)
                return
            self.log(f"goal {goal['id']}: {stage.name} passed")
            idx += 1
        if idx >= len(stages_):
            ledger.update_goal(self.conn, goal["id"], stage_idx=len(stages_) - 1)
            self._land_or_ask(ledger.get_goal(self.conn, goal["id"]))
            return
        goal = self._enter_stage(ledger.get_goal(self.conn, goal["id"]), idx)
        self.log(f"goal {goal['id']}: stage {stages_[idx].name} ({goal['role']})")
        self.free_attempt(goal, goal["attempt"] + 1)

    def _save_attempts(self, goal: dict) -> dict[str, int]:
        """Remember how many attempts the current stage has spent, so coming back to it resumes the count."""
        counts = json.loads(goal.get("stage_attempts") or "{}")
        current = self._stage(goal)
        if current.role is None:  # a code-only stage spends no attempts
            return counts
        counts[current.name] = max(int(goal.get("attempt") or 0), int(counts.get(current.name, 0)))
        ledger.update_goal(self.conn, goal["id"], stage_attempts=json.dumps(counts))
        return counts

    def _enter_stage(self, goal: dict, idx: int) -> dict:
        """Move the goal to worker stage ``idx``: its role, its resumed attempt count, a fresh stage_sha."""
        stage = self._stages(goal)[idx]
        if stage.role is None:
            raise RunError(f"stage {stage.name} is Team's own, not a worker's")
        counts = self._save_attempts(goal)
        order = ledger.latest_order(self.conn, goal["id"])
        ledger.update_goal(
            self.conn, goal["id"], stage_idx=idx, role=self._role_for_stage(stage, order),
            attempt=int(counts.get(stage.name, 0)), stage_sha=None,
        )
        if goal.get("part_id"):
            ledger.set_part_stage(self.conn, goal["part_id"], stage.name)
        return ledger.get_goal(self.conn, goal["id"])

    def _back_to_builder(self, goal: dict, from_idx: int, detail: str) -> None:
        """A later stage (verify, review) failed on the builder's work: back to the last builder
        stage before it, as that stage's next attempt, with what was found."""
        stages_ = self._stages(goal)
        back = max(i for i in range(from_idx) if stages_[i].role)
        goal = self._enter_stage(goal, back)
        self.log(f"goal {goal['id']}: {stages_[from_idx].name} sends it back to {stages_[back].name}: {_tail(detail, 200)}")
        self._next_attempt(goal, detail)

    def _land_decision(self, goal: dict) -> dict | None:
        found = [
            d for d in ledger.list_decisions(self.conn, goal["target"], status=None)
            if d.get("goal_id") == goal["id"] and d.get("source") == "land"
        ]
        return found[-1] if found else None

    def _land_or_ask(self, goal: dict) -> None:
        """Every gate passed. Land now, or ask Eli first when the type or the diff says so."""
        existing = self._land_decision(goal)
        if existing is None:
            repo, base = self._repo(goal), goal.get("base") or "main"
            changed = grade.changed_files(repo, goal["branch"], base, runner=self.runner)
            lines = grade.lines_changed(repo, goal["branch"], base, runner=self.runner)
            target = targets.require(goal["target"])
            risk, why = gates.risk_of(
                goal.get("type") or gates.DEFAULT_TYPE, changed, lines,
                extra_paths=target.get("risk_paths") or [],
                max_lines=self.cfg["risk.max_changed_lines"],
            )
            if risk == "auto" and (goal.get("risk") or "auto") == "auto":
                self._land(goal)
                return
            first = goal["goal"].splitlines()[0][:70]
            report = goal.get("detail") or ""
            report = ("\n\n" + report) if report.startswith(("security report:", self.ONBOARDING)) else ""
            did = ledger.create_decision(
                self.conn,
                goal["target"],
                f"Land goal {goal['id']}? {first}",
                f"{why}. Branch {goal['branch']} passed every gate: {len(changed)} files, {lines} lines changed.{report}",
                ["land it", "drop it (the branch is kept)"],
                "land it",
                goal_id=goal["id"],
                task_id=goal.get("current_task"),
                source="land",
            )
            ledger.update_goal(
                self.conn, goal["id"], risk="ask",
                detail=f"waiting for Eli to land: decision {did}; {why}" + report,  # the report stays readable
            )
            self.log(f"goal {goal['id']}: {why}; asked Eli (decision {did})")
            return
        if existing["status"] == "open":
            return
        if existing["status"] == "dismissed" or not says_land(existing.get("answer")):
            reason = f"Eli chose not to land it (decision {existing['id']}: {existing.get('answer') or 'dismissed'}). Branch {goal['branch']} is kept."
            self.log(f"goal {goal['id']}: {reason}")
            ledger.finish_goal(self.conn, goal["id"], "fail", reason)
            return
        applied = self._apply_onboarding(goal) if goal.get("type") == "onboarding" else []
        if applied:
            self.log(f"goal {goal['id']}: target {goal['target']} now has " + ", ".join(applied))
        self._land(goal)

    def free_attempt(
        self,
        goal: dict,
        attempt: int,
        failure: str | None = None,
        *,
        parents: tuple[str, ...] = (),
    ) -> str:
        """A kanban task for attempt ``attempt`` on the goal's branch; returns its id.

        A builder attempt in sweep mode (``_sweep_wanted``) gets no task: its job is queued
        and the next tick runs it (``_run_sweep``); the returned id is its task_ref.
        """
        role = self._role(goal["role"], goal["target"])
        order = ledger.latest_order(self.conn, goal["id"])
        if self._sweep_wanted(goal, role, order):
            return self._sweep_queue(goal, attempt, failure, role, order)
        profile = role.get("profile") or goal["role"]  # <role>--<target> when the target overlays it
        if goal.get("harness_id") and goal.get("arm") == "branch":
            profile = profiles.candidate_profile(goal["role"], goal["harness_id"])
        if not profiles.profile_dir(profile).exists():
            raise RunError(
                f"missing Hermes profile {profile}; run `team roles sync` first"
            )
        if order is not None and attempt == 1:
            choice = catalog.resolve(order["order"]["model"], self.cat)
        else:
            choice = self._choice(role, attempt, goal["target"])
        grade.ensure_worktree(
            self._repo(goal), goal["worktree"], goal["branch"], runner=self.runner
        )
        stage = self._stage(goal)
        if goal.get("part_id"):
            ledger.set_part_stage(self.conn, goal["part_id"], stage.name)
        if attempt == 1 or not goal.get("stage_sha"):
            # Where the branch stood when this stage began: a read-only stage must leave it there.
            ledger.update_goal(self.conn, goal["id"], stage_sha=self._branch_head(goal))
        title = goal["goal"].splitlines()[0][:80]
        if stage.name != "implement":
            title = f"[{stage.name}] {title}"
        if attempt > 1:
            title += f" (attempt {attempt})"
        body = self._brief_for(goal, attempt, failure)
        if order is not None and stage.gate == "check":
            skills = tuple(order["order"]["skills"])
            toolsets = tuple(order["order"]["toolsets"])
        else:
            skills, toolsets = tuple(role["skills"]), ()
        task = kanban.create_task(
            goal["target"],
            title,
            body,
            assignee=profile,
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

    # -- sweep attempts (team.sweep) ----------------------------------------------

    def _sweep_wanted(self, goal: dict, role: dict, order: dict | None) -> bool:
        """A builder attempt runs as one sweep when the setting is on, the role is a free Hermes
        builder, the goal owns named files, and the lead did not order ``turns``."""
        if not self.cfg["sweep.enabled"] or role["runs_on"]["harness"] != "hermes":
            return False
        if self._stage(goal).gate != "check" or not self._owned(goal):
            return False
        mode = ((order or {}).get("order") or {}).get("mode") or "sweep"
        return mode != "turns"

    def _sweep_queue(self, goal: dict, attempt: int, failure: str | None, role: dict, order: dict | None) -> str:
        """The job row for a sweep attempt; the tick runs it so `team goal` returns at once."""
        if order is not None and attempt == 1:
            choice = catalog.resolve(order["order"]["model"], self.cat)
        else:
            choice = self._choice(role, attempt, goal["target"])
        grade.ensure_worktree(self._repo(goal), goal["worktree"], goal["branch"], runner=self.runner)
        stage = self._stage(goal)
        if goal.get("part_id"):
            ledger.set_part_stage(self.conn, goal["part_id"], stage.name)
        if attempt == 1 or not goal.get("stage_sha"):
            ledger.update_goal(self.conn, goal["id"], stage_sha=self._branch_head(goal))
        ref = f"{sweep.TASK_REF}{goal['id']}-{attempt}"
        job = self._start_job(goal, attempt, ref, choice, order["id"] if order else None)
        detail = SWEEP_QUEUED + (f"\n{_tail(failure)}" if failure else "")
        ledger.update_goal(self.conn, goal["id"], attempt=attempt, current_task=None, job_id=job, detail=detail)
        self.log(f"goal {goal['id']}: attempt {attempt} on {choice.model}, one sweep ({choice.why})")
        return ref

    def _sweep_role(self, goal: dict, role: dict) -> dict:
        """Whose SOUL and skills the sweep prompt carries: the role's files, or for a harness
        review's branch arm the profile synced from the branch."""
        if goal.get("harness_id") and goal.get("arm") == "branch":
            pdir = profiles.profile_dir(profiles.candidate_profile(goal["role"], goal["harness_id"]))
            if pdir.exists():
                return sweep.role_from_profile(goal["role"], pdir)
        return role

    def _run_sweep(self, goal: dict, job: dict) -> bool:
        """Run a queued sweep attempt to its end (calls, apply, commit, check), grade the job and
        move the goal on: advance, the lead and the next attempt, or the replay's record."""
        self._sweep_rounds(goal, job)
        goal, job = ledger.get_goal(self.conn, goal["id"]), ledger.get_job(self.conn, job["id"])
        if goal.get("harness_id"):
            self._finish_replay(goal, job)
        elif job["outcome"] == "pass":
            self._advance(goal)
        else:
            self._after_failed_sweep(goal, job)
        return True

    def _sweep_rounds(self, goal: dict, job: dict) -> None:
        """The calls of one sweep attempt, each applied and checked, until one passes or the
        attempt's calls are spent; finishes the job."""
        role = self._role(goal["role"], goal["target"])
        owned = self._owned(goal) or []
        order = ledger.latest_order(self.conn, goal["id"])
        body = order["order"] if order else None
        choice = catalog.ModelChoice(**job["model_choice"])
        repo, wt = self._repo(goal), Path(goal["worktree"])
        grade.ensure_worktree(repo, wt, goal["branch"], runner=self.runner)
        detail = goal.get("detail") or ""
        failure = detail.split("\n", 1)[1] if detail.startswith(SWEEP_QUEUED) and "\n" in detail else None
        system = sweep.instructions(self._sweep_role(goal, role))
        attempt = int(goal.get("attempt") or 1)
        calls_n, samples = self.cfg["sweep.calls_per_attempt"], self.cfg["sweep.samples"]
        call_failure, grader = None, "sweep"
        for k in range(1, calls_n + 1):
            prompt = sweep.packet(
                body, goal, wt, owned, self.cfg["sweep.packet_bytes"],
                attempt=attempt, call=k, failure=failure, call_failure=call_failure,
            )
            answers = self._sweep_calls(goal, job, choice, system, prompt, samples, k)
            head = grade.head(wt, runner=self.runner)
            for i, ans in enumerate(answers):
                if ans.get("error"):
                    call_failure, grader = ans["error"], "sweep"
                    continue
                changes, refused, cannot = sweep.plan_changes(ans.get("text"), owned, wt)
                if cannot:
                    reason = f"the worker says the part cannot be done in its files: {cannot}"
                    ledger.finish_job(self.conn, job["id"], "fail", "sweep", reason)
                    self.log(f"goal {goal['id']}: {reason}")
                    return
                if changes is None:
                    call_failure, grader = "answer refused: " + "; ".join(refused), "sweep"
                    continue
                if i:  # another sample of the same call starts from where the call began
                    grade._git(wt, "reset", "--hard", head, runner=self.runner)
                sweep.apply(wt, changes)
                grade._git(wt, "add", "-A", runner=self.runner)
                grade._must(
                    grade._git(wt, "commit", "-q", "-m", f"team sweep {attempt}.{k}: {sweep.summary_of(ans.get('text'))}", runner=self.runner),
                    "git commit failed",
                )
                ok, result = self.grade(goal)
                if ok:
                    ledger.finish_job(self.conn, job["id"], "pass", "check", result)
                    self.log(f"goal {goal['id']}: sweep attempt {attempt} passed after {k} call(s)")
                    return
                call_failure, grader = result, "check"
        ledger.finish_job(self.conn, job["id"], "fail", grader, call_failure or "no usable answer")
        self.log(f"goal {goal['id']}: sweep attempt {attempt} failed after {calls_n} call(s): {_tail(call_failure, 200)}")

    def _sweep_calls(self, goal: dict, job: dict, choice: catalog.ModelChoice, system: str, prompt: str, samples: int, k: int) -> list[dict]:
        """``samples`` completions of one packet in parallel, each a ledger call row."""
        session = f"sweep-{goal['id']}-{goal.get('attempt') or 1}-{k}"
        max_tokens = self.cfg["sweep.max_output_tokens"]

        def one(i: int) -> dict:
            started = time.monotonic()
            try:
                r = dict(self.complete(choice.spec, system, prompt, max_tokens=max_tokens, session=f"{session}-{i}"))
            except backend.ScoutError as e:
                # Worded so the ledger files the attempt under infra, not the worker's work.
                r = {"error": f"gateway did not answer: {e}", "usage": e.usage}
            except (OSError, ValueError) as e:
                r = {"error": f"gateway did not answer: {type(e).__name__}: {e}"}
            r["latency_ms"] = int((time.monotonic() - started) * 1000)
            r["session"] = f"{session}-{i}"
            return r

        if samples <= 1:
            results = [one(0)]
        else:
            with ThreadPoolExecutor(max_workers=samples) as pool:
                results = list(pool.map(one, range(samples)))
        for r in results:  # the connection belongs to this thread
            usage = r.get("usage") or {}
            details = usage.get("prompt_tokens_details") or {}
            ledger.record_call(
                self.conn, job["id"], "litellm", choice.model, goal["role"],
                usage.get("prompt_tokens"), usage.get("completion_tokens"), r.get("latency_ms"),
                not r.get("error"), error=r.get("error"), task_ref=job["task_ref"],
                session_id=r["session"], profile=goal["role"], response_model=r.get("model"),
                goal_id=goal["id"], target=goal["target"], cache_read_tokens=details.get("cached_tokens"),
            )
        return results

    def _after_failed_sweep(self, goal: dict, job: dict) -> None:
        """A sweep attempt failed: the lead reads it (as after a kanban worker), then the next
        attempt, Claude, or Eli."""
        detail = job["grader_detail"] or ""
        goal = ledger.get_goal(self.conn, goal["id"])
        role = self._role(goal["role"], goal["target"])
        nxt = goal["attempt"] + 1
        diagnosis = self._diagnose(goal, job, detail) if nxt <= role["escalation"]["after_failures"] else None
        if diagnosis is not None and diagnosis["action"] == "ask":
            self._give_up(goal, f"the lead asks Eli: {diagnosis['question']}", diagnosis["cause"])
            return
        if diagnosis is not None and diagnosis["action"] == "escalate":
            if self.cfg["escalation.claude"]:
                self._claude_attempt(goal, nxt, detail)
            else:
                self._give_up(goal, "the lead says a free model cannot do this", detail)
            return
        self._next_attempt(goal, detail)

    def _watch(self, goal: dict, job: dict, full: dict) -> bool:
        """Kanban neither enforces --max-runtime nor notices a dead worker."""
        runs = full.get("runs") or []
        if not runs:
            return False
        run = runs[-1]
        pid, started = run.get("worker_pid"), run.get("started_at")
        budget = self._role(goal["role"], goal["target"])["budget"]["run_budget_s"]
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
        stage = self._stage(goal)
        ok, detail = self.gate(goal)
        ledger.finish_job(
            self.conn, job["id"], "pass" if ok else "fail", stage.gate, detail
        )
        send_back = not ok and detail.startswith(gates.SEND_BACK)
        if (ok or send_back) and status == "review":
            kanban.complete(
                goal["target"],
                task_id,
                result=(f"passed: {detail[-200:]}" if ok else f"the reviewer sent it back: {detail[-200:]}"),
                runner=self.runner,
            )
        if goal.get("harness_id"):
            if not ok:
                self._block(goal, f"replay attempt failed: {_tail(detail, 200)}")
            self._finish_replay(goal, ledger.get_job(self.conn, job["id"]))
            return
        if ok:
            self._advance(goal)
            return
        if send_back:
            self._back_to_builder(ledger.get_goal(self.conn, goal["id"]), int(goal["stage_idx"]), detail)
            return
        kanban.comment(
            goal["target"], task_id, f"FAILED: {_tail(detail)}", runner=self.runner
        )
        goal = ledger.get_goal(self.conn, goal["id"])
        role = self._role(goal["role"], goal["target"])
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
            choice = self._choice(role, nxt, goal["target"])
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

    def _finish_replay(self, goal: dict, job: dict) -> None:
        """A replay arm is one attempt, never retried or landed: record it, drop the branch."""
        try:
            grade.remove_worktree(
                self._repo(goal), goal["worktree"], goal["branch"], runner=self.runner
            )
        except grade.GradeError as e:
            self.log(f"goal {goal['id']}: replay graded, but {e}")
        ledger.finish_goal(
            self.conn,
            goal["id"],
            job["outcome"] or "error",
            f"replay {goal.get('arm')}: {_tail(job['grader_detail'], 200)}",
        )

    def _next_attempt(self, goal: dict, failure: str | None) -> None:
        """Free attempts up to the role's after_failures, then one on Claude, then Eli."""
        goal = ledger.get_goal(self.conn, goal["id"])
        if goal.get("harness_id"):
            self._finish_replay(goal, ledger.get_job(self.conn, goal["job_id"]))
            return
        role = self._role(goal["role"], goal["target"])
        nxt = goal["attempt"] + 1
        used = ledger.goal_usage(self.conn, goal["id"])
        over = []
        if used["calls"] > self.cfg["worker.max_calls_per_goal"]:
            over.append(f"{used['calls']} worker calls (limit {self.cfg['worker.max_calls_per_goal']})")
        if used["tokens_in"] > self.cfg["worker.max_input_tokens_per_goal"]:
            over.append(f"{used['tokens_in']:,} input tokens (limit {self.cfg['worker.max_input_tokens_per_goal']:,})")
        if over:
            self._give_up(goal, "over its budget after " + " and ".join(over), failure)
        elif nxt <= role["escalation"]["after_failures"]:
            self.free_attempt(goal, nxt, failure)
        elif (
            nxt == role["escalation"]["after_failures"] + 1
            and self.cfg["escalation.claude"]
            and role["runs_on"]["harness"] != "hermes-subscription"
        ):
            self._claude_attempt(goal, nxt, failure)
        else:
            self._give_up(goal, f"all {goal['attempt']} attempts failed", failure)

    def _claude_attempt(
        self,
        goal: dict,
        attempt: int,
        failure: str | None,
        *,
        answer: str | None = None,
    ) -> None:
        grade.ensure_worktree(
            self._repo(goal), goal["worktree"], goal["branch"], runner=self.runner
        )
        model = (
            roles.load(self.root, goal["role"], target=goal["target"]).get("escalation", {}).get("model")
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
        stage = self._stage(goal)
        result = hermes_run.run(
            goal["worktree"],
            self._brief_for(goal, attempt, failure, claude=True, answer=answer),
            max_turns=self.cfg["escalation.claude_max_turns"],
            timeout_s=self.cfg["escalation.claude_timeout_s"],
            runner=self.runner,
            role=goal["role"],
            root=self.root,
            check=goal["check_cmd"] if stage.gate == "check" else None,
            model=model,
            allowed_files=owned,
            decision_file=decision_file,
            target=goal["target"],
        )
        ledger.set_task_ref(
            self.conn, job, f"claude:{result.get('session_id') or 'unknown'}"
        )
        model_tracking.record_result(self.conn, job, result, goal["role"])
        asked = self._claude_decision(goal, decision_file)
        if asked:
            # Not a failed attempt: the goal waits for Eli's answer. step() sees
            # the job's detail and comes back here when the decision is settled.
            reason = f"waiting for Eli: decision {asked}"
            ledger.finish_job(self.conn, job, "error", "claude", f"{ASKED}{asked}")
            ledger.update_goal(self.conn, goal["id"], detail=reason)
            self._block(goal, reason)
            self.log(f"goal {goal['id']}: Claude asked Eli (decision {asked}); waiting")
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
        ok, detail = self.gate(goal, text=str(result.get("result") or ""))
        ledger.finish_job(self.conn, job, "pass" if ok else "fail", stage.gate, detail)
        send_back = not ok and detail.startswith(gates.SEND_BACK)
        if (ok or send_back) and goal.get("current_task"):
            kanban.complete(
                goal["target"],
                goal["current_task"],
                result=(f"claude passed: {detail[-200:]}" if ok else f"claude's review sent it back: {detail[-200:]}"),
                runner=self.runner,
            )
        if ok:
            self._advance(goal)
        elif send_back:
            self._back_to_builder(ledger.get_goal(self.conn, goal["id"]), int(goal["stage_idx"]), detail)
        else:
            self._give_up(goal, "the Claude attempt failed too", detail)

    def _after_decision(self, goal: dict, job: dict) -> bool:
        """The goal stopped on a question to Eli. Nothing until he answers; then
        the branch is graded as it stands (grade() merges main into it, so a
        fix that landed meanwhile counts) and Claude runs again only if that
        fails, with Eli's answer in its brief. A dismissed question stops the goal."""
        did = int(job["grader_detail"][len(ASKED):].split()[0])
        d = ledger.get_decision(self.conn, did)
        if d is None or d["status"] == "open":
            return False
        if d["status"] == "dismissed":
            self._give_up(goal, f"Eli dismissed its question (decision {did})", d["question"])
            return True
        answer = f"Question: {d['question']}\nAnswer: {d['answer']}"
        ok, detail = self.gate(goal)
        if ok:
            regrade = self._start_job(
                goal,
                goal["attempt"],
                "team-run:regrade",
                catalog.ModelChoice("none", "team-run", "none", f"re-graded after decision {did}"),
            )
            ledger.finish_job(self.conn, regrade, "pass", "check", detail)
            ledger.update_goal(self.conn, goal["id"], job_id=regrade)
            self.log(f"goal {goal['id']}: passes as it stands after decision {did}; moving on")
            self._advance(goal)
            return True
        self.log(f"goal {goal['id']}: decision {did} answered; Claude continues with the answer")
        self._claude_attempt(goal, goal["attempt"], detail, answer=answer)
        return True

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

    def _left_work(self, goal: dict) -> bool:
        """True when the attempt left commits on its branch or edits in its worktree."""
        repo, wt, base = self._repo(goal), Path(goal["worktree"]), goal.get("base") or "main"
        if grade.has_commits(repo, goal["branch"], base, runner=self.runner):
            return True
        if not wt.exists():
            return False
        try:
            return not grade.is_clean(wt, runner=self.runner) or bool(
                grade._must(
                    grade._git(wt, "ls-files", "--others", "--exclude-standard", runner=self.runner),
                    "git ls-files failed",
                )
            )
        except grade.GradeError:
            return False

    def grade(self, goal: dict) -> tuple[bool, str]:
        """Merge base into the branch, enforce ownership, run the check. (passed, output)."""
        repo, branch, wt, base = (
            self._repo(goal),
            goal["branch"],
            Path(goal["worktree"]),
            goal.get("base") or "main",
        )
        grade.ensure_worktree(repo, wt, branch, runner=self.runner)
        # Code rescues a near-miss: work left uncommitted (a forgotten commit, a
        # worker out of turns) is committed and graded, not failed on ceremony.
        rescued = grade.commit_leftovers(
            wt,
            f"team: attempt {goal.get('attempt', 1)} left these changes uncommitted; "
            "Team committed them before grading",
            self._owned(goal),
            runner=self.runner,
        )
        note = "Team committed the worker's uncommitted changes before grading. " if rescued else ""
        if goal.get("needs_commits", 1) and not grade.has_commits(
            repo, branch, base, runner=self.runner
        ):
            return False, "no commits"
        if not grade.is_clean(wt, runner=self.runner):
            return False, "uncommitted changes in the worktree"
        merged, conflicts = grade.merge_base_into(wt, base, runner=self.runner)
        if not merged:
            return False, f"merge conflict with {base}:\n" + "\n".join(conflicts)
        changed = grade.changed_files(repo, branch, base, runner=self.runner)
        if goal.get("needs_commits", 1) and not changed:
            return False, note + "no changes against base: a commit with nothing in it is not a result"
        owned = self._owned(goal)
        if owned:
            outside = grade.outside_owned(changed, owned)
            if outside:
                return False, note + "changes outside owned files: " + ", ".join(outside)
        ok, output = self._run_check(wt, goal["check_cmd"])
        if not ok and not output:
            output = f"`{goal['check_cmd']}` failed with no output"
        if not ok:
            return False, note + output
        if goal.get("acceptance_cmd"):
            # The part's own proof: a command that failed before the work and must pass now.
            ok, accepted = self._run_check(wt, goal["acceptance_cmd"])
            if not ok:
                return False, note + f"check passed but the acceptance command failed (`{goal['acceptance_cmd']}`):\n{accepted or '(no output)'}"
            output += f"\nacceptance passed: `{goal['acceptance_cmd']}`"
        return True, note + output

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
        role: str | None = None,
    ) -> int:
        """A job row for an attempt by the goal's role, or by ``role`` ('team' for a code-only gate)."""
        return ledger.start_job(
            self.conn,
            goal["target"],
            grade.head(self._repo(goal), runner=self.runner),
            role or goal["role"],
            roles.version(self.root, goal["role"]) if role is None else "code",
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
                    if found.get("mode"):
                        revised["mode"] = found["mode"]
                    ledger.create_order(
                        self.conn, goal["id"], revised, goal.get("part_id"), lead_job
                    )
                    found["excerpts"] = lead.excerpts(
                        found["context"], tree, self.cfg["lead.context_bytes"]
                    )[0]
        except (lead.LeadError, grade.GradeError, ValueError, RunError) as e:
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
            cause_tag=found.get("cause_tag"),
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
    role: str | None = None,
    check: str | None = None,
    runner: Runner = subprocess.run,
    parents: tuple[str, ...] = (),
    part_id: int | None = None,
    base: str = "main",
    cat: dict | None = None,
    log: Callable[[str], None] = _log,
    kind: str = gates.DEFAULT_TYPE,
    acceptance: str | None = None,
) -> dict:
    """``team goal --no-lead``: a worktree, a kanban task, a ledger job and a goals row.

    ``kind`` picks the pipeline; ``role`` may name the builder for its first worker stage
    (it must have that stage's gate) and defaults to the stage's own.
    """
    record = targets.require(target)
    loop = Loop(root, conn, runner=runner, cat=cat, log=log, preflight=None)
    pipeline = gates.stages_for(kind, loop._active_roles())
    first = next(s for s in pipeline if s.role)
    if role is None:
        role = first.role
    else:
        if loop._role(role).get("gate") != first.gate:
            raise RunError(f"role {role!r} cannot take the {first.name} stage of a {kind} goal")
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
        kind=kind,
        acceptance_cmd=acceptance,
        stages=[s.name for s in pipeline],
        needs_commits=kind != "onboarding",  # a repo that already has its check gets settings, not commits
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
    test_runner = record.get("test_runner")
    try:
        with lead.workspace(
            repo, target, f"plan-{lead_job}", base, runner
        ) as tree:
            # Read once, in code, what the lead used to spend a dozen turns reading.
            scout_context = ""
            if record.get("scout"):
                from team import scout

                report = scout.scan(target, tree=tree, publish=False)
                scout_context = scout.context(target, tree=tree, report=report)
                if report["errors"]:
                    raise RunError("scout failed before lead planning: " + "; ".join(report["errors"]))
            briefing = lead.briefing(tree, goal, loop.cfg["lead.briefing_bytes"], runner=runner,
                                     scout_context=scout_context)
            summary, parts, kind = lead.ask(
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
                    briefing=briefing,
                    test_runner=test_runner,
                ),
                lead.PLAN_SCHEMA,
                lambda data: _with_summary(data, tree, menu, loop, test_runner),
                loop.cfg,
                runner,
                on_call=lambda r: loop._lead_call(lead_job, r),
            )
    except (lead.LeadError, grade.GradeError, ValueError, RunError) as e:
        ledger.finish_job(conn, lead_job, "error", "lead", str(e))
        hint = (
            " (Claude usage limit; try later, or `team goal --no-lead`)"
            if getattr(e, "usage_limited", False)
            else ""
        )
        raise RunError(f"the lead could not plan this goal: {e}{hint}") from e
    ledger.finish_job(
        conn, lead_job, "pass", "lead-validate", f"{len(parts)} parts ({kind}): {summary}"[:500]
    )
    return {
        "goal": goal,
        "check": check_cmd,
        "base": base,
        "summary": summary,
        "type": kind,
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
    kind = draft.get("type") or gates.DEFAULT_TYPE
    pipeline = gates.stages_for(kind, loop._active_roles())
    first = next(s for s in pipeline if s.role)
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
            stage=first.name,
            branch=f"team/{pslug}",
            depends_on=deps,
            owned_paths=part["owned_paths"],
        )
        # The lead's role is the builder; the pipeline's first stage may be another worker's.
        role = loop._role_for_stage(first, {"order": part})
        goal_id = ledger.create_goal(
            conn,
            target,
            part["title"] if len(parts) > 1 else goal,
            role,
            f"team/{pslug}",
            str(grade.worktree_path(target, pslug)),
            draft["check"],
            part_id=part_ids[part["key"]],
            base=base,
            kind=kind,
            acceptance_cmd=part.get("acceptance_cmd"),
            stages=[s.name for s in pipeline],
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


def _with_summary(data: Any, tree: Path, menu: dict, loop: Loop, test_runner: str | None = None) -> tuple:
    """``validate_plan`` for ``lead.ask``: ((summary, parts, type), refusals)."""
    parts, errors = lead.validate_plan(
        data, tree, menu, loop.cfg["lead.max_parts"], loop.cat, test_runner=test_runner
    )
    kind, type_errors = lead.plan_type(data)
    summary = str((data or {}).get("summary") or "") if isinstance(data, dict) else ""
    return ((summary, parts, kind), errors + type_errors)


def run_loop(
    root: Path,
    conn: sqlite3.Connection,
    *,
    once: bool = False,
    interval: int | None = None,
    runner: Runner = subprocess.run,
    sleep: Callable[[float], None] = time.sleep,
    loop: Loop | None = None,
    restart: Callable[[], None] | None = None,
    only: tuple[str, ...] | None = None,
    boss_work: bool = True,
    pin: bool = False,
) -> None:
    """Tick until stopped; the interval is re-read from settings every tick.

    ``only`` restricts the loop to those targets (one ``team run --target X`` per target
    keeps a slow check on one from stalling the others); ``boss_work`` off leaves the
    boss's releases and reviews to another process; ``pin`` keeps this process on the
    code it started with instead of restarting when the checkout changes (a measurement
    run while Team's own code is being edited).

    Between ticks nothing of this process is in flight (workers run under
    Hermes's kanban, the lead's draft is synchronous inside a tick), so when
    Team's code has changed since this process started it replaces itself with
    the same command, and the next tick runs the checkout's code. Without this
    a loop started before a commit kept the old ``team`` modules for days.
    """
    from team import gateway  # team.gateway imports only the ledger

    loop = loop or Loop(root, conn, runner=runner)
    born, sha = time.time(), gateway.checkout_sha(root)
    while True:
        loop.tick(only=only, boss_work=boss_work)
        if once:
            return
        why = None if pin else gateway.changed_since(root, born, sha)
        if why:
            loop.log(f"team run {why}; restarting with the checkout's code")
            (restart or _reexec)()
            born, sha = time.time(), gateway.checkout_sha(root)  # a restart that returned (tests)
        sleep(interval if interval is not None else loop.cfg["loop.interval_s"])


def _reexec() -> None:
    """Replace this process with the same command line, keeping pid, stdio and environment."""
    sys.stdout.flush()
    sys.stderr.flush()
    os.execv(sys.executable, list(sys.orig_argv))
