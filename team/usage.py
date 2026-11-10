"""Token usage totals by model, role and goal for a target"""
from __future__ import annotations

import sqlite3

from team import targets


def _model_name(row: sqlite3.Row) -> str:
    """Pick the model name to group a call row by."""
    response_model = row["response_model"]
    if response_model and isinstance(response_model, str) and response_model != "unknown":
        return response_model
    model = row["model"]
    if model and isinstance(model, str) and model != "unknown":
        return model
    return "unknown"


def _bucket() -> dict:
    """Return a fresh zeroed bucket: input, output, calls."""
    return {"input": 0, "output": 0, "calls": 0}


def _add(bucket: dict, tin: int | None, tout: int | None) -> None:
    """Add one call's tokens (NULL defaults to 0) to a bucket."""
    bucket["input"] += tin or 0
    bucket["output"] += tout or 0
    bucket["calls"] += 1


def usage_breakdown(conn: sqlite3.Connection, target: str) -> dict:
    """Return token totals by model, role and goal for one target."""
    targets.require(target)

    # Job id -> goal id. Later (newer) goals overwrite earlier ones, so every
    # job maps to exactly one goal and the groups never double count.
    goal_of: dict[int, int] = {}
    for row in conn.execute(
        "SELECT id, part_id, job_id FROM goals WHERE target = ? ORDER BY id",
        (target,),
    ):
        goal_id = row["id"]
        part_id = row["part_id"]
        job_id = row["job_id"]
        if part_id is not None:
            for job_row in conn.execute("SELECT id FROM jobs WHERE part_id = ?", (part_id,)):
                goal_of[job_row["id"]] = goal_id
        elif job_id is not None:
            goal_of[job_id] = goal_id

    total = _bucket()
    by_model: dict[str, dict] = {}
    by_role: dict[str, dict] = {}
    by_goal: dict[str, dict] = {}

    for row in conn.execute(
        "SELECT c.job_id, c.model, c.response_model, c.task_class,"
        " c.tokens_in, c.tokens_out, j.role AS job_role"
        " FROM calls c LEFT JOIN jobs j ON j.id = c.job_id"
        " WHERE COALESCE(c.target, j.target) = ?",
        (target,),
    ):
        task_class = row["task_class"]
        if not task_class or task_class == "unknown":
            task_class = row["job_role"]
        job_id = row["job_id"]
        goal_key = str(goal_of[job_id]) if job_id in goal_of else "none"
        model_key = _model_name(row)
        tin = row["tokens_in"]
        tout = row["tokens_out"]
        _add(total, tin, tout)
        _add(by_model.setdefault(model_key, _bucket()), tin, tout)
        _add(by_role.setdefault(task_class, _bucket()), tin, tout)
        _add(by_goal.setdefault(goal_key, _bucket()), tin, tout)

    return {"total": total, "by_model": by_model, "by_role": by_role, "by_goal": by_goal}
