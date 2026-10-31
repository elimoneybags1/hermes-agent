"""The Team tab's board: the boss over one column per role, and every token.

Each column (Planner, Implementer, Rescue) holds the goals that are at that
role now. A goal carries its trail of attempts: the tier the attempt asked
for, the model LiteLLM actually answered with, that model's rank in its tier
(1 is the tier's first choice) and, when it was not the first choice, which
higher-ranked models failed during the attempt. Tokens are summed by zone
(boss, planner, implementer, rescue, other) and by model.

Read-only. Sources: the ledger (jobs, calls, goals, parts, orders), the
catalog (tier order) and LiteLLM's usage log, opened read-only so reporting
never creates it. Nothing is estimated: a number the logs do not hold is
``None`` and the tab says "not recorded".
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import tomllib
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

from team import boss, catalog, ledger, targets

ZONES = ("boss", "aide", "lead", "implement", "rescue", "other")
TOKENS = ("tokens_in", "tokens_out", "cache_read", "cache_write")
LANDED_SHOWN = 8


def ranks(cat: dict) -> dict[str, list[str]]:
    """Each tier's models in LiteLLM's order, leaving out models no provider serves today."""
    out: dict[str, list[tuple[int, str]]] = defaultdict(list)
    for m in cat["models"]:
        if not m.get("tier"):
            continue
        if not any(not s.get("unavailable") for s in m.get("served_by", [])):
            continue
        out[m["tier"]].append((m.get("order") or 99, m["id"]))
    return {t: [i for _, i in sorted(rows)] for t, rows in out.items()}


def ranks_at(root: Path, cat: dict):
    """Tier order as it was at a moment: from catalog.json's git history, and the file on
    disk (dated by its mtime) when it has uncommitted changes. An attempt is ranked against
    the order LiteLLM had when it ran, not today's."""
    rel = "integrations/portkey/catalog.json"
    versions: list[tuple[str, dict]] = []
    log = subprocess.run(["git", "-C", str(root), "log", "--format=%H %cI", "--", rel],
                         capture_output=True, text=True, check=False)
    for line in log.stdout.splitlines():
        sha, when = line.split(" ", 1)
        shown = subprocess.run(["git", "-C", str(root), "show", f"{sha}:{rel}"],
                               capture_output=True, text=True, check=False)
        try:
            versions.append((_utc(when), ranks(json.loads(shown.stdout))))
        except (ValueError, KeyError, TypeError):
            continue
    dirty = subprocess.run(["git", "-C", str(root), "status", "--porcelain", "--", rel],
                           capture_output=True, text=True, check=False).stdout.strip()
    path = Path(root) / rel
    if dirty and path.exists():
        mtime = datetime.fromtimestamp(path.stat().st_mtime, UTC)
        versions.append((mtime.isoformat(timespec="seconds"), ranks(cat)))
    versions.sort(key=lambda v: v[0])
    now = ranks(cat)

    def at(ts: str) -> dict[str, list[str]]:
        ts = _utc(ts)
        before = [r for when, r in versions if when <= ts]
        return before[-1] if before else (versions[0][1] if versions else now)

    return at


def _utc(ts: str) -> str:
    return datetime.fromisoformat(ts).astimezone(UTC).isoformat(timespec="seconds")


def configured(root: Path) -> dict[str, dict]:
    """What each zone is set to run on, from the roles' role.toml."""

    def role(name: str) -> dict:
        path = root / "team" / "roles" / name / "role.toml"
        return tomllib.loads(path.read_text()) if path.exists() else {}

    implement = role("implement")
    rescue = implement.get("escalation", {})
    return {
        "boss": {"model": role("boss").get("runs_on", {}).get("model")},
        "lead": {"model": role("lead").get("runs_on", {}).get("model")
                 or role("lead").get("runs_on", {}).get("harness")},
        "aide": {"tiers": role("aide").get("models", {}).get("candidates", [])},
        "implement": {"tiers": implement.get("models", {}).get("candidates", [])},
        "rescue": {"model": rescue.get("model") or rescue.get("harness"),
                   "after_failures": rescue.get("after_failures")},
    }


def _gateway() -> sqlite3.Connection | None:
    path = ledger.state_dir() / "litellm" / "usage.db"
    if not path.exists():
        return None
    gw = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    gw.row_factory = sqlite3.Row
    return gw


def _spec(job: dict) -> str:
    return (job.get("model_choice") or {}).get("spec") or ""


def zone(job: dict | None, profile: str | None) -> str:
    if job is None:
        return profile if profile in ("boss", "aide") else "other"
    if job["role"] == "lead":
        return "lead"
    if job["role"] == "implement":
        return "rescue" if _spec(job) == "claude" or (job.get("model_choice") or {}).get(
            "why", ""
        ).startswith("escalation") else "implement"
    return "other"


def _sum(rows) -> dict:
    """Token totals; a field no row carries (cache today) stays None."""
    out = {k: None for k in TOKENS}
    out["calls"] = 0
    for r in rows:
        out["calls"] += 1
        for k in TOKENS:
            v = r[k] if k in r.keys() else None
            if v is not None:
                out[k] = (out[k] or 0) + v
    return out


def _short(text: str | None, n: int = 160) -> str:
    line = next((x for x in (text or "").splitlines() if x.strip()), "")
    return line if len(line) <= n else line[: n - 1] + "…"


def answered(
    gw, job: dict, sessions: set[str], order: dict[str, list[str]], cooldown_s: int = 0
) -> dict:
    """Which model(s) answered the job's tier calls in LiteLLM, and why not the first choice.

    Matches LiteLLM rows by session id; when no session matches (older rows),
    by the tier group within the job's start and end, marked ``by: "time"``.
    A higher-ranked model counts as passed over when it failed during the job
    or within LiteLLM's cooldown before it started (a failed model sits out).
    """
    group = (job.get("model_choice") or {}).get("model") or ""
    tier = group.removeprefix("tier-") if group.startswith("tier-") else None
    out = {"tier": tier, "models": [], "by": None, "passed_over": [], "tried": [], "top_tried": None}
    if gw is None or not tier:
        return out
    end = job.get("ended") or ledger._now()
    rows = []
    if sessions:
        marks = ",".join("?" * len(sessions))
        rows = gw.execute(
            f'SELECT * FROM calls WHERE "group"=? AND session IN ({marks}) AND ts>=? AND ts<=?',
            (group, *sessions, job["started"], end),
        ).fetchall()
        out["by"] = "session" if rows else None
    if not rows:
        rows = gw.execute(
            'SELECT * FROM calls WHERE "group"=? AND ts>=? AND ts<=?',
            (group, job["started"], end),
        ).fetchall()
        out["by"] = "time" if rows else None
    ranked = order.get(tier, [])
    seen: dict[str, int] = {}
    for r in rows:
        if r["ok"] and r["model"]:
            name = r["model"].removeprefix("openai/")
            seen[name] = seen.get(name, 0) + 1
    out["models"] = [
        {"model": m, "calls": n, "rank": ranked.index(m) + 1 if m in ranked else None,
         "of": len(ranked)}
        for m, n in sorted(seen.items(), key=lambda x: -x[1])
    ]
    tried: dict[str, bool] = {}
    for r in rows:
        name = (r["model"] or "").removeprefix("openai/")
        if name:
            tried[name] = tried.get(name, False) or bool(r["ok"])
    # In the order LiteLLM tried them, so "skipped #1, #3 failed, #2 answered" reads off it.
    out["tried"] = [
        {"model": m, "ok": ok, "rank": ranked.index(m) + 1 if m in ranked else None}
        for m, ok in tried.items()
    ]
    out["top_tried"] = bool(ranked) and ranked[0] in tried
    best = min((m["rank"] for m in out["models"] if m["rank"]), default=None)
    if best and best > 1:
        failed = gw.execute(
            'SELECT model, error, rate_limited FROM calls WHERE "group"=? AND ok=0 AND ts>=? AND ts<=?',
            (group, _before(job["started"], cooldown_s), end),
        ).fetchall()
        above = set(ranked[: best - 1])
        for f in failed:
            name = (f["model"] or "").removeprefix("openai/")
            if name in above:
                out["passed_over"].append(
                    {"model": name, "rank": ranked.index(name) + 1,
                     "why": "rate limited" if f["rate_limited"] else _short(f["error"], 80)}
                )
    return out


def _before(ts: str, seconds: int) -> str:
    from datetime import datetime, timedelta

    return (datetime.fromisoformat(ts) - timedelta(seconds=seconds)).isoformat(timespec="seconds")


def _attempt(conn, gw, job: dict, order, cooldown_s: int = 0) -> dict:
    order = order(job["started"]) if callable(order) else order
    calls = conn.execute("SELECT * FROM calls WHERE job_id=?", (job["id"],)).fetchall()
    sessions = set()
    for c in calls:
        for k in ("gateway_session_id", "session_id"):
            if k in c.keys() and c[k]:
                sessions.add(c[k])
    choice = job.get("model_choice") or {}
    out = {
        "job": job["id"],
        "attempt": job["attempt"],
        "zone": zone(job, None),
        "asked": choice.get("spec") or choice.get("model"),
        "outcome": job["outcome"],
        "detail": _short(job.get("grader_detail")),
        "started": job["started"],
        "ended": job.get("ended"),
        "tokens": _sum(calls),
        "route": answered(gw, job, sessions, order, cooldown_s),
    }
    if out["zone"] in ("rescue", "lead"):
        names = {c["response_model"] or c["model"] for c in calls if c["ok"]}
        out["route"]["models"] = [{"model": n, "calls": None, "rank": None, "of": None}
                                  for n in sorted(names)]
    return out


def _goal_jobs(conn, goal: dict) -> list[dict]:
    ids = set()
    if goal.get("part_id"):
        ids |= {r[0] for r in conn.execute("SELECT id FROM jobs WHERE part_id=?", (goal["part_id"],))}
    ids |= {r[0] for r in conn.execute(
        "SELECT j.id FROM jobs j JOIN orders o ON j.order_id=o.id WHERE o.goal_id=?", (goal["id"],))}
    if goal.get("job_id"):
        ids.add(goal["job_id"])
    rows = [ledger.get_job(conn, i) for i in sorted(ids)]
    return [j for j in rows if j and j["role"] != "lead"]


_MERGE_FILES: dict[tuple[str, int], list[str]] = {}


def merge_files(repo: Path, branch: str, goal_id: int) -> list[str]:
    """Files changed by the goal's merge commit versus its first parent; [] if the commit is not found.

    A landed goal's merge never changes, so a found answer is kept for the
    process: the page polls this for every landed goal, and each lookup is two
    git runs of about 0.2 s in this repository.
    """
    key = (str(repo), goal_id)
    if key in _MERGE_FILES:
        return list(_MERGE_FILES[key])
    result = subprocess.run(
        ["git", "-C", str(repo), "log", "--merges", "--format=%H%x09%s", "--fixed-strings", "--grep", f"Merge {branch}: "],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return []
    for line in result.stdout.splitlines():
        sha, _, subject = line.partition("\t")
        if subject.startswith(f"Merge {branch}: ") and subject.endswith(f"(goal {goal_id})"):
            diff_result = subprocess.run(
                ["git", "-C", str(repo), "diff", "--name-only", f"{sha}^1", sha],
                capture_output=True,
                text=True,
                check=False,
            )
            if diff_result.returncode != 0:
                return []
            files = [x for x in diff_result.stdout.splitlines() if x]
            _MERGE_FILES[key] = files
            return list(files)
    return []


def goal_card(conn, gw, goal: dict, order, cooldown_s: int = 0) -> dict:
    part = ledger.get_part(conn, goal["part_id"]) if goal.get("part_id") else None
    latest = ledger.latest_order(conn, goal["id"])
    attempts = [_attempt(conn, gw, j, order, cooldown_s) for j in _goal_jobs(conn, goal)]
    lead_job = ledger.get_job(conn, latest["lead_job_id"]) if latest and latest.get("lead_job_id") else None
    if goal["outcome"] == "pass":
        station = "landed"
    elif goal["outcome"]:
        station = "stopped"
    elif attempts:
        station = attempts[-1]["zone"]
    else:
        station = "lead"
    files: list[str] = []
    if station == "landed":
        try:
            files = merge_files(targets.repo_path(goal["target"]), goal["branch"], goal["id"])
        except (targets.TargetError, OSError):
            files = []
    return {
        "id": goal["id"],
        "target": goal["target"],
        "goal": goal["goal"],
        "station": station,
        "outcome": goal["outcome"],
        "detail": _short(goal.get("detail")),
        "check": goal["check_cmd"],
        "branch": goal["branch"],
        "owns": part["owned_paths"] if part else [],
        "depends_on": part["depends_on"] if part else [],
        "files": files,
        "order_bytes": latest["body_bytes"] if latest else None,
        "order_version": latest["version"] if latest else None,
        "planned": _attempt(conn, gw, lead_job, order, cooldown_s) if lead_job else None,
        "attempts": attempts,
        "created": goal["created"],
        "finished": goal.get("finished"),
    }


def tokens(conn, gw) -> dict:
    """Every recorded token, by zone (ledger) and by model (LiteLLM's log)."""
    jobs = {j["id"]: j for j in ledger.list_jobs(conn)}
    by_zone: dict[str, list] = defaultdict(list)
    rows = conn.execute("SELECT * FROM calls").fetchall()
    for c in rows:
        by_zone[zone(jobs.get(c["job_id"]), c["profile"])].append(c)
    first = rows[0]["ts"] if rows else None
    out = {
        "since": first,
        "total": _sum(rows),
        "zones": {z: _sum(by_zone.get(z, [])) for z in ZONES},
        "models": [],
    }
    if gw is not None:
        out["models"] = [
            dict(r) for r in gw.execute(
                'SELECT tier, model, count(*) calls, sum(ok) ok, sum(rate_limited) rate_limited,'
                ' sum(tokens_in) tokens_in, sum(tokens_out) tokens_out, max(ts) last'
                ' FROM calls GROUP BY tier, model ORDER BY tier, calls DESC'
            )
        ]
        for m in out["models"]:
            m["model"] = (m["model"] or "").removeprefix("openai/")
    return out


def tiers(gw, order: dict[str, list[str]]) -> list[dict]:
    """Each tier's ranked models with their record in LiteLLM's log: is the first choice serving?"""
    seen = {}
    if gw is not None:
        for r in gw.execute(
            'SELECT model, count(*) calls, sum(ok) ok, sum(rate_limited) rl, max(ts) last,'
            ' max(CASE WHEN ok=0 THEN ts END) last_fail FROM calls GROUP BY model'
        ):
            seen[(r["model"] or "").removeprefix("openai/")] = dict(r)
    return [
        {"tier": t, "models": [{**seen.get(m, {"calls": 0}), "model": m, "rank": i + 1}
                               for i, m in enumerate(ms)]}
        for t, ms in order.items()
    ]


def board(conn: sqlite3.Connection, root: Path, cat: dict | None = None) -> dict:
    cat = cat if cat is not None else catalog.load()
    order = ranks(cat)
    cooldown_s = int(cat.get("policy", {}).get("litellm", {}).get("cooldown_time") or 0)
    gw = _gateway()
    try:
        goals = ledger.list_goals(conn, all_goals=True)
        at = ranks_at(root, cat)
        cards = [goal_card(conn, gw, g, at, cooldown_s) for g in goals]
        waiting = boss.waiting()
        cols = {z: [c for c in cards if c["station"] == z] for z in ("lead", "implement", "rescue")}
        done = [c for c in cards if c["station"] in ("landed", "stopped")][:LANDED_SHOWN]
        return {
            "configured": configured(root),
            "waiting": waiting,
            "columns": cols,
            "done": done,
            "tokens": tokens(conn, gw),
            "tiers": tiers(gw, order),
        }
    finally:
        if gw is not None:
            gw.close()
