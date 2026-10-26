"""The flow of goals through the team, for the Flow view in `team ui`.

Read-only. Joins three stores under `$TEAM_STATE`: the ledger (`goals`,
`jobs`, `calls`), each target's kanban board (`kanban/kanban/boards/<target>/
kanban.db`: task status, model, worker pid, parent links) and the boss's saved
plans (`planning/<uuid>.json`: objective, tasks, dependencies, which goal each
task became). Every goal gets a *stage* and a *track*: the stations it passes
through (boss, its role, landed) and which one it is at now.

Stages today are one hop long (a goal goes to one role, then lands); when the
pipeline in docs/PIPELINE.md exists, its stages become more stations.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from team import ledger

PLAN_MARKER = "Team plan task: "
DONE_LIMIT = 20
DESCRIPTION_CHARS = 240

# kanban task status -> goal stage
_TASK_STAGE = {"running": "running", "review": "review", "blocked": "blocked", "done": "review"}
# a stage's order on the track: waiting, queued and running sit at the role's station
_STAGE_ORDER = ["waiting", "queued", "running", "review", "blocked", "landed", "failed"]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _iso(ts) -> str | None:
    if ts is None:
        return None
    try:
        return datetime.fromtimestamp(float(ts), timezone.utc).isoformat(timespec="seconds")
    except (OverflowError, OSError, ValueError, TypeError):
        return None


# --- kanban boards (read-only)


def board_db(target: str) -> Path:
    return ledger.state_dir() / "kanban" / "kanban" / "boards" / target / "kanban.db"


def read_tasks(target: str) -> dict[str, dict]:
    """Tasks on a target's board by id, with their parent task ids."""
    path = board_db(target)
    if not path.is_file():
        return {}
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=1.0)
    except sqlite3.Error:
        return {}
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT id, status, assignee, model_override, worker_pid, started_at, completed_at,"
            " branch_name, last_failure_error, block_kind FROM tasks"
        ).fetchall()
        links = conn.execute("SELECT parent_id, child_id FROM task_links").fetchall()
    except sqlite3.Error:
        return {}
    finally:
        conn.close()
    tasks = {r["id"]: dict(r) | {"parents": []} for r in rows}
    for link in links:
        if link["child_id"] in tasks:
            tasks[link["child_id"]]["parents"].append(link["parent_id"])
    return tasks


# --- plans (read-only)


def read_plans() -> list[dict]:
    """The boss's saved plans: objective, tasks with dependencies, goal per task."""
    base = ledger.state_dir() / "planning"
    if not base.is_dir():
        return []
    plans = []
    for path in sorted(base.glob("*.json")):
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict) or "id" not in data:
            continue
        contract = data.get("contract") or {}
        task_goals = data.get("task_goals") or {}
        tasks = []
        for t in contract.get("tasks") or []:
            if not isinstance(t, dict) or not t.get("id"):
                continue
            tasks.append(
                {
                    "id": t["id"],
                    "title": t.get("title") or t["id"],
                    "role": t.get("role"),
                    "depends_on": list(t.get("depends_on") or []),
                    "files": list(t.get("files") or []),
                    "goal_id": task_goals.get(t["id"]),
                }
            )
        plans.append(
            {
                "id": data["id"],
                "target": data.get("target"),
                "objective": data.get("objective") or "",
                "revision": data.get("revision") or 0,
                "created_at": data.get("created_at"),
                "updated_at": data.get("updated_at"),
                "tasks": tasks,
            }
        )
    plans.sort(key=lambda p: p.get("updated_at") or p.get("created_at") or "", reverse=True)
    return plans


def parallel_groups(tasks: list[dict]) -> list[list[str]]:
    """Tasks grouped into waves: everything in one wave only depends on earlier
    waves, so a wave can run at the same time. Unknown dependencies are ignored;
    a cycle ends up in a final wave of its own."""
    ids = {t["id"] for t in tasks}
    deps = {t["id"]: {d for d in t.get("depends_on") or [] if d in ids} for t in tasks}
    waves, placed = [], set()
    while len(placed) < len(ids):
        wave = [t["id"] for t in tasks if t["id"] not in placed and deps[t["id"]] <= placed]
        if not wave:
            wave = [t["id"] for t in tasks if t["id"] not in placed]
        waves.append(wave)
        placed.update(wave)
    return waves


# --- goals


def _plan_ref(goal_text: str) -> tuple[str | None, str | None]:
    first = goal_text.splitlines()[0] if goal_text else ""
    if not first.startswith(PLAN_MARKER):
        return None, None
    ref = first[len(PLAN_MARKER):].strip()
    plan_id, _, task_id = ref.partition(":")
    return plan_id or None, task_id or None


def _title_and_description(goal_text: str) -> tuple[str, str]:
    lines = [ln.rstrip() for ln in (goal_text or "").splitlines()]
    if lines and lines[0].startswith(PLAN_MARKER):
        lines = lines[1:]
    while lines and not lines[0].strip():
        lines = lines[1:]
    title = lines[0].strip() if lines else "(no title)"
    rest = " ".join(ln.strip() for ln in lines[1:] if ln.strip())
    cut = rest.find("Team scoped assignment:")
    if cut >= 0:
        rest = rest[:cut].strip()
    if len(rest) > DESCRIPTION_CHARS:
        rest = rest[: DESCRIPTION_CHARS - 1].rstrip() + "…"
    return title[:160], rest


def _stage(goal: dict, task: dict | None, tasks: dict[str, dict] | None = None) -> str:
    """landed/failed once finished; else by the kanban task: running, review,
    blocked; `waiting` when a parent task is not done yet (kanban holds it);
    `queued` otherwise."""
    if goal.get("finished"):
        return "landed" if goal.get("outcome") == "pass" else "failed"
    if task is None:
        return "queued"
    stage = _TASK_STAGE.get(task.get("status") or "", "queued")
    if stage == "queued" and tasks:
        for parent in task.get("parents") or []:
            p = tasks.get(parent)
            if p is not None and p.get("status") != "done":
                return "waiting"
    return stage


def _track(goal: dict, stage: str) -> list[dict]:
    """Stations in order with each one's state: done, current or todo."""
    role = goal["role"]
    if stage in ("landed", "failed"):
        return [
            {"station": "boss", "state": "done"},
            {"station": role, "state": "done" if stage == "landed" else "failed"},
            {"station": "landed", "state": "done" if stage == "landed" else "todo"},
        ]
    return [
        {"station": "boss", "state": "done"},
        {"station": role, "state": "current"},
        {"station": "landed", "state": "todo"},
    ]


def _job_rows(conn, goal_ids: list[int]) -> dict[int, list[dict]]:
    """Jobs per goal (by the goal's task refs), each with its latest call's model."""
    if not goal_ids:
        return {}
    rows = conn.execute(
        "SELECT g.id AS goal_id, j.id, j.attempt, j.started, j.ended, j.outcome, j.grader_detail, j.task_ref,"
        " (SELECT c.model FROM calls c WHERE c.job_id = j.id ORDER BY c.id DESC LIMIT 1) AS model,"
        " (SELECT c.provider FROM calls c WHERE c.job_id = j.id ORDER BY c.id DESC LIMIT 1) AS provider"
        " FROM goals g JOIN jobs j ON j.id = g.job_id OR (j.task_ref IS NOT NULL AND j.task_ref = g.current_task)"
        " WHERE g.id IN (%s) ORDER BY j.id" % ",".join("?" * len(goal_ids)),
        goal_ids,
    ).fetchall()
    out: dict[int, list[dict]] = {}
    seen = set()
    for r in rows:
        key = (r["goal_id"], r["id"])
        if key in seen:
            continue
        seen.add(key)
        out.setdefault(r["goal_id"], []).append(
            {
                "id": r["id"],
                "attempt": r["attempt"],
                "started": r["started"],
                "ended": r["ended"],
                "outcome": r["outcome"],
                "model": r["model"],
                "provider": r["provider"],
                "detail": (r["grader_detail"] or "")[:200],
            }
        )
    return out


PROXY_TIERS = {"team-auto", "team-simple", "team-medium", "team-complex", "team-reasoning"}


def proxy_model(task_id: str | None) -> dict | None:
    """The real model the LiteLLM proxy last served a task with (usage.db,
    session = the kanban task id), for tasks pinned to a proxy tier."""
    if not task_id:
        return None
    path = ledger.state_dir() / "litellm" / "usage.db"
    if not path.is_file():
        return None
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=1.0)
    except sqlite3.Error:
        return None
    try:
        row = conn.execute(
            "SELECT model, tier, ts FROM calls WHERE session = ? AND ok = 1 ORDER BY id DESC LIMIT 1", (task_id,)
        ).fetchone()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    return {"model": row[0], "tier": row[1], "ts": row[2]} if row else None


def _model_for(goal: dict, task: dict | None, jobs: list[dict], role_model: str | None) -> dict:
    """The model this goal is on right now, and where that is known from."""
    if task and task.get("model_override") and goal.get("finished") is None:
        m = task["model_override"]
        out = {"model": m, "source": "task", "live": task.get("status") == "running", "behind_proxy": None}
        if m in PROXY_TIERS or m.startswith("team-"):
            out["behind_proxy"] = proxy_model(task.get("id"))
        return out
    for j in reversed(jobs):
        if j.get("model") and j["model"] != "unknown":
            return {"model": j["model"], "source": "ledger", "live": False}
    return {"model": role_model, "source": "role", "live": False}


def goal_cards(conn, roles: list[dict], tasks_by_target: dict[str, dict[str, dict]] | None = None) -> list[dict]:
    """Every active goal plus the last DONE_LIMIT finished ones, as cards."""
    role_model = {r["name"]: (r.get("runs_on") or {}).get("model") for r in roles}
    rows = conn.execute(
        "SELECT * FROM goals WHERE finished IS NULL"
        " UNION ALL SELECT * FROM (SELECT * FROM goals WHERE finished IS NOT NULL ORDER BY id DESC LIMIT ?)"
        " ORDER BY id DESC",
        (DONE_LIMIT,),
    ).fetchall()
    goals = [dict(r) for r in rows]
    jobs = _job_rows(conn, [g["id"] for g in goals])
    tasks_by_target = dict(tasks_by_target or {})
    cards = []
    for g in goals:
        if g["target"] not in tasks_by_target:
            tasks_by_target[g["target"]] = read_tasks(g["target"])
        board = tasks_by_target[g["target"]]
        task = board.get(g.get("current_task") or "")
        stage = _stage(g, task, board)
        plan_id, plan_task = _plan_ref(g["goal"])
        title, description = _title_and_description(g["goal"])
        gjobs = jobs.get(g["id"], [])
        cards.append(
            {
                "id": g["id"],
                "target": g["target"],
                "role": g["role"],
                "title": title,
                "description": description,
                "attempt": g.get("attempt") or 1,
                "created": g.get("created"),
                "finished": g.get("finished"),
                "outcome": g.get("outcome"),
                "detail": (g.get("detail") or "")[:300],
                "branch": g.get("branch"),
                "task_id": g.get("current_task"),
                "task_status": task.get("status") if task else None,
                "task_started": _iso(task.get("started_at")) if task else None,
                "worker_pid": task.get("worker_pid") if task else None,
                "block_kind": task.get("block_kind") if task else None,
                "parents": list(task.get("parents") or []) if task else [],
                "stage": stage,
                "track": _track(g, stage),
                "model": _model_for(g, task, gjobs, role_model.get(g["role"])),
                "jobs": gjobs,
                "plan_id": plan_id,
                "plan_task": plan_task,
            }
        )
    return cards


def _counts(cards: list[dict]) -> dict:
    counts = {"made": len(cards)}
    for stage in _STAGE_ORDER:
        counts[stage] = sum(1 for c in cards if c["stage"] == stage)
    return counts


def snapshot(conn: sqlite3.Connection | None, roles: list[dict], pipeline: list[str] | None = None) -> dict:
    """Agents with their goals by stage, every goal card, plans with parallel waves."""
    cards = goal_cards(conn, roles) if conn is not None else []
    by_role: dict[str, list[dict]] = {}
    for c in cards:
        by_role.setdefault(c["role"], []).append(c)
    order = list(pipeline or [])
    for r in roles:
        if r["name"] not in order:
            order.append(r["name"])
    for name in by_role:
        if name not in order:
            order.append(name)
    role_by_name = {r["name"]: r for r in roles}
    agents = []
    for name in order:
        r = role_by_name.get(name, {})
        mine = by_role.get(name, [])
        running = [c for c in mine if c["stage"] in ("running", "review")]
        live = next(
            ((c["model"].get("behind_proxy") or {}).get("model") or c["model"]["model"] for c in running if c["model"].get("live")),
            None,
        )
        agents.append(
            {
                "name": name,
                "title": r.get("title", ""),
                "status": r.get("status", "unknown" if name not in role_by_name else ""),
                "model": (r.get("runs_on") or {}).get("model"),
                "live_model": live,
                "running": running,
                "queued": [c for c in mine if c["stage"] in ("queued", "waiting")][::-1],  # dispatch order
                "blocked": [c for c in mine if c["stage"] == "blocked"],
                "done": [c for c in mine if c["stage"] in ("landed", "failed")],
            }
        )
    plans = read_plans()
    card_by_id = {c["id"]: c for c in cards}
    for p in plans:
        for t in p["tasks"]:
            card = card_by_id.get(t["goal_id"]) if t["goal_id"] is not None else None
            t["stage"] = card["stage"] if card else ("planned" if t["goal_id"] is None else "unknown")
            t["model"] = card["model"]["model"] if card else None
        landed = {t["id"] for t in p["tasks"] if t["stage"] == "landed"}
        for t in p["tasks"]:
            if t["stage"] == "planned" and any(d not in landed for d in t["depends_on"]):
                t["stage"] = "waiting"
        p["waves"] = parallel_groups(p["tasks"])
        p["dispatched"] = sum(1 for t in p["tasks"] if t["goal_id"] is not None)
    return {
        "generated_at": _now_iso(),
        "stations": ["boss", *[n for n in order if n != "boss"], "landed"],
        "agents": agents,
        "goals": cards,
        "plans": plans,
        "counts": _counts(cards),
    }


def fingerprint() -> str:
    """Changes when any board or saved plan changes (the ledger is watched elsewhere)."""
    parts = []
    boards = ledger.state_dir() / "kanban" / "kanban" / "boards"
    if boards.is_dir():
        for db in sorted(boards.glob("*/kanban.db")):
            for p in (db, db.with_name("kanban.db-wal")):
                try:
                    st = p.stat()
                except OSError:
                    continue
                parts.append(f"{db.parent.name}:{p.name}:{st.st_mtime_ns}:{st.st_size}")
    plans = ledger.state_dir() / "planning"
    if plans.is_dir():
        for p in sorted(plans.glob("*.json")):
            try:
                st = p.stat()
            except OSError:
                continue
            parts.append(f"plan:{p.name}:{st.st_mtime_ns}")
    return "|".join(parts)
