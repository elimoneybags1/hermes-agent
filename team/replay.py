"""The replay set: finished worker orders re-run to grade a change to a role's files.

Eli's rule (IDEAS.md, point 6): a change to a skill, prompt or route is kept only
if it beats the current version on a fixed replay set, reported next to that
base rate, and adopted changes are git commits. This module is the set and its
arms. ``team.boss`` proposes a change and settles the result; ``team.run``
grades each arm like any goal and throws the branch away.

The set is ``$TEAM_STATE/replay/<target>.json``: the newest ``replay.size``
finished worker orders, one per goal, each with the commit it was written
against and the check that graded it. A replay goal is that order again, in a
worktree from that commit, graded by that check: one attempt, no lead, no
Claude. The ``branch`` arm runs on a profile synced from the review's branch;
the ``base`` arm runs on the role's live profile, and is measured again only
when the role's files have changed since the set was last measured.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from team import grade, kanban, ledger, roles, targets

Runner = Callable[..., subprocess.CompletedProcess[Any]]
ARMS = ("branch", "base")


class ReplayError(RuntimeError):
    """The set cannot be built."""


def set_path(target: str) -> Path:
    return ledger.state_dir() / "replay" / f"{target}.json"


def load_set(target: str) -> dict | None:
    path = set_path(target)
    return json.loads(path.read_text()) if path.exists() else None


def save_set(s: dict) -> None:
    path = set_path(s["target"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(s, indent=1, sort_keys=True) + "\n")


def build_set(
    conn: sqlite3.Connection, target: str, size: int, runner: Runner = subprocess.run
) -> dict:
    """Freeze the newest finished worker orders, one per goal, whose commit still exists."""
    repo = targets.repo_path(target)
    rows = conn.execute(
        "SELECT j.id AS job_id, j.target_commit, j.outcome, o.id AS order_id, o.body,"
        " g.id AS goal_id, g.goal, g.check_cmd, g.role"
        " FROM jobs j JOIN orders o ON o.id = j.order_id JOIN goals g ON g.id = o.goal_id"
        " WHERE j.target = ? AND j.outcome IS NOT NULL AND g.harness_id IS NULL"
        " AND (j.task_ref IS NULL OR j.task_ref NOT LIKE 'claude:%')"
        " ORDER BY j.id DESC",
        (target,),
    ).fetchall()
    items: list[dict] = []
    seen: set[int] = set()
    for r in rows:
        if r["goal_id"] in seen:
            continue
        exists = grade._git(
            repo, "cat-file", "-e", f"{r['target_commit']}^{{commit}}", runner=runner
        )
        if exists.returncode != 0:
            continue
        seen.add(r["goal_id"])
        items.append(
            {
                "goal_id": r["goal_id"],
                "job_id": r["job_id"],
                "order_id": r["order_id"],
                "order": json.loads(r["body"]),
                "goal": r["goal"],
                "check": r["check_cmd"],
                "role": r["role"],
                "commit": r["target_commit"],
                "outcome": r["outcome"],
            }
        )
        if len(items) >= size:
            break
    if not items:
        raise ReplayError("no finished worker orders to replay yet")
    return {"target": target, "built": ledger._now(), "items": items, "base": None}


def versions(root: Path, role_names: list[str]) -> dict[str, str]:
    return {r: roles.version(root, r) for r in role_names}


def base_is_current(s: dict, root: Path, role_names: list[str]) -> bool:
    """True when the set's base was measured with these roles' files as they are on main now."""
    base = s.get("base") or {}
    have = base.get("versions") or {}
    return bool(base) and all(have.get(r) == v for r, v in versions(root, role_names).items())


def start(
    root: Path,
    conn: sqlite3.Connection,
    review_id: int,
    target: str,
    s: dict,
    arms: list[str],
    *,
    runner: Runner = subprocess.run,
    cat: dict | None = None,
    log: Callable[[str], None] = lambda _m: None,
) -> list[int]:
    """One replay goal per item and arm, each with its task on the board; returns goal ids."""
    from team import run  # run grades the arms; it does not import this module at load

    record = targets.require(target)
    repo = Path(record["path"])
    loop = run.Loop(root, conn, runner=runner, cat=cat, log=log, preflight=None)
    kanban.ensure_board(target, runner=runner)
    ids = []
    for arm in arms:
        if arm not in ARMS:
            raise ValueError(f"arm must be one of {ARMS}, got {arm!r}")
        for k, item in enumerate(s["items"], 1):
            path, branch = grade.create_worktree(
                repo, target, f"replay-{review_id}-{arm}-{k}", base=item["commit"], runner=runner
            )
            gid = ledger.create_goal(
                conn,
                target,
                item["goal"],
                item["role"],
                branch,
                str(path),
                item["check"],
                base=item["commit"],
            )
            ledger.update_goal(conn, gid, harness_id=review_id, arm=arm)
            ledger.create_order(conn, gid, item["order"])
            loop.free_attempt(ledger.get_goal(conn, gid), 1)
            ids.append(gid)
    return ids


def results(conn: sqlite3.Connection, review_id: int) -> dict | None:
    """Per arm: goals passed, goals run, worker input tokens; None while any goal is unfinished."""
    goals = ledger.harness_goals(conn, review_id)
    if not goals or any(g["finished"] is None for g in goals):
        return None
    out: dict[str, dict] = {}
    for arm in ARMS:
        mine = [g for g in goals if g["arm"] == arm]
        if not mine:
            continue
        job_ids = [g["job_id"] for g in mine if g["job_id"]]
        tokens = 0
        if job_ids:
            marks = ",".join("?" * len(job_ids))
            tokens = conn.execute(
                f"SELECT COALESCE(SUM(tokens_in), 0) FROM calls WHERE job_id IN ({marks})",
                job_ids,
            ).fetchone()[0]
        out[arm] = {
            "pass": sum(g["outcome"] == "pass" for g in mine),
            "n": len(mine),
            "tokens": int(tokens or 0),
        }
    return out


def verdict(base: dict, branch: dict) -> str:
    """``adopt`` when the branch passed more, or as many for fewer tokens; a tie is not a win."""
    if branch["pass"] > base["pass"]:
        return "adopt"
    if branch["pass"] == base["pass"] and branch["tokens"] < base["tokens"]:
        return "adopt"
    return "dismiss"


def describe(base: dict, branch: dict) -> str:
    return (
        f"passed {branch['pass']}/{branch['n']} (base {base['pass']}/{base['n']}); "
        f"worker input tokens {branch['tokens']:,} (base {base['tokens']:,})"
    )
