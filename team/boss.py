"""What the boss works with: proposals Eli approves, project notes, status, findings.

The boss is the agent Eli talks to (``team/roles/boss``, a Hermes profile on
Claude through the subscription). It never reads code and never touches the
board directly: its tools (``plugins/team``) call the functions here.

- A **proposal** is a plan the boss wants to run: one sentence, a deeper
  summary, and its goals in order. Making one asks the lead to draft every
  goal (``run.draft_goal``), so the proposal already shows which parts each
  worker gets, with toolsets, skills, model and owned files. It opens a
  decision in Eli's inbox; only Eli's answer approves it (the boss has no tool
  that can), and ``team run`` releases approved proposals.
- **Project notes** (``$TEAM_STATE/targets/<t>/project.md``) are the boss's
  durable context: what the project is for, decisions, the larger plan, open
  questions. Capped at ``boss.notes_chars`` so it stays cheap to carry.
- **Status** and **findings** are compact readings of the ledger: what is
  running and what came back; and where attempts fail, what the lead said
  about it, and what a landed goal costs in tokens.
- A **suggestion** is the boss's proposed change to how Team works (a role's
  SOUL or skill, a setting). It goes to the inbox; nothing edits itself.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any

from team import ledger, run, settings, targets

HEADLINE_CHARS = 160
SUMMARY_CHARS = 1500
PLAN_OPTIONS = ["approve", "dismiss"]
SUGGESTION_OPTIONS = ["apply", "dismiss"]


class BossError(ValueError):
    """A request the boss made that Team refuses, with the reason it can act on."""


def _dir(target: str) -> Path:
    return ledger.state_dir() / "targets" / target


def proposal_path(target: str, plan_id: int) -> Path:
    return _dir(target) / "proposals" / f"{plan_id}.json"


def _save(p: dict) -> None:
    path = proposal_path(p["target"], p["id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(p, indent=2))


def load_proposal(target: str, plan_id: int) -> dict:
    path = proposal_path(target, plan_id)
    if not path.exists():
        raise BossError(f"no proposal {plan_id} on {target}")
    return json.loads(path.read_text())


def proposals(target: str | None = None) -> list[dict]:
    """Every proposal, newest first."""
    names = [target] if target else [t["name"] for t in targets.list_targets()]
    out = []
    for name in names:
        d = _dir(name) / "proposals"
        if d.is_dir():
            out += [json.loads(f.read_text()) for f in d.glob("*.json")]
    return sorted(out, key=lambda p: p["id"], reverse=True)


# --- proposals ------------------------------------------------------------------


def propose(
    root: Path,
    conn: sqlite3.Connection,
    target: str,
    headline: str,
    summary: str,
    goals: list[dict],
    *,
    notes: str = "",
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    cat: dict | None = None,
    log: Callable[[str], None] = run._log,
) -> dict:
    """Draft every goal with the lead and put the plan in Eli's inbox; returns the proposal."""
    targets.require(target)
    cfg = settings.load(root)
    headline, summary = " ".join(headline.split()), summary.strip()
    if not headline or len(headline) > HEADLINE_CHARS:
        raise BossError(f"headline must be one sentence of at most {HEADLINE_CHARS} characters")
    if not summary or len(summary) > SUMMARY_CHARS:
        raise BossError(f"summary must be 1 to {SUMMARY_CHARS} characters")
    if not goals or len(goals) > cfg["boss.max_goals"]:
        raise BossError(f"a proposal has 1 to {cfg['boss.max_goals']} goals")
    clean = []
    for i, g in enumerate(goals):
        text = str(g.get("goal") or "").strip()
        after = [int(a) for a in g.get("after") or []]
        if not text:
            raise BossError(f"goal {i + 1} is empty")
        if any(a < 1 or a > i for a in after):
            raise BossError(f"goal {i + 1}: after may only name earlier goals (1 to {i})")
        clean.append({"goal": text, "after": after, "check": g.get("check") or None})
    plan_id = ledger.create_plan(conn, target, headline, summary=summary, status="draft")
    try:
        for g in clean:
            g["draft"] = run.draft_goal(
                root, conn, target, g["goal"], check=g["check"], notes=notes,
                runner=runner, cat=cat, log=log,
            )
    except run.RunError as e:
        ledger.update_plan(conn, plan_id, status="done", summary=f"not drafted: {e}")
        raise BossError(str(e)) from e
    decision = ledger.create_decision(
        conn,
        target,
        headline,
        summary,
        PLAN_OPTIONS,
        "approve",
        source=f"boss-plan:{plan_id}",
    )
    p = {
        "id": plan_id,
        "target": target,
        "headline": headline,
        "summary": summary,
        "status": "waiting",
        "decision_id": decision,
        "goals": clean,
        "created": ledger._now(),
    }
    _save(p)
    return p


def card(p: dict) -> dict:
    """What Eli sees: the sentence, the summary, and what each worker gets."""
    goals = []
    for n, g in enumerate(p["goals"], 1):
        d = g["draft"]
        goals.append(
            {
                "n": n,
                "goal": g["goal"],
                "after": g["after"],
                "plan": d["summary"],
                "parts": [
                    {
                        "title": part["title"],
                        "role": part["role"],
                        "model": part["model"],
                        "toolsets": part["toolsets"],
                        "skills": part["skills"],
                        "owned_paths": part["owned_paths"],
                        "steps": len(part["steps"]),
                        "context": [f"{c['path']}:{c['start']}-{c['end']}" for c in part["context"]],
                        "depends_on": part["depends_on"],
                    }
                    for part in d["parts"]
                ],
            }
        )
    return {
        "id": p["id"],
        "target": p["target"],
        "headline": p["headline"],
        "summary": p["summary"],
        "status": p["status"],
        "decision_id": p["decision_id"],
        "goals": goals,
        "goal_ids": p.get("goal_ids", []),
    }


# The tab draws these top to bottom: Boss, Planner (the lead), Implementer, Landed.
STATIONS = ("boss", "lead", "workers", "landed")


def flow(conn: sqlite3.Connection, target: str | None = None, limit: int = 20) -> list[dict]:
    """Every recent plan, goal by goal, with the station each goal is at.

    A goal sits with the **boss** while its plan waits for Eli, with the
    **lead** once approved and until it is released, with the **workers**
    while any of its parts is open, and is **landed** when every part passed.
    ``stopped`` means a part failed and nothing of it is still running.
    """
    out = []
    for p in proposals(target)[:limit]:
        c = card(p)
        for g, raw in zip(c["goals"], p["goals"], strict=True):
            rows = [ledger.get_goal(conn, i) for i in raw.get("goal_ids") or []]
            for part, row in zip(g["parts"], rows, strict=False):
                part["state"] = _part_state(conn, row)
            if p["status"] == "waiting":
                d = ledger.get_decision(conn, p["decision_id"])
                accepted = d is not None and d["status"] == "answered"
                g["station"] = "lead" if accepted else "boss"
            elif p["status"] in ("dismissed",):
                g["station"] = "dismissed"
            elif not rows:
                g["station"] = "lead" if p["status"] != "failed" else "stopped"
            elif all(r["outcome"] == "pass" for r in rows):
                g["station"] = "landed"
            elif any(r["outcome"] is None for r in rows):
                g["station"] = "workers"
            else:
                g["station"] = "stopped"
        c["error"] = p.get("error")
        if c["status"] == "waiting" and c["goals"] and c["goals"][0]["station"] == "lead":
            c["status"] = "accepted"
        out.append(c)
    return out


def _part_state(conn: sqlite3.Connection, row: dict | None) -> str:
    if row is None:
        return "not released"
    if row["outcome"] == "pass":
        return "landed"
    if row["outcome"]:
        return f"{row['outcome']}: {_line(row['detail'], 80)}"
    if not row["current_task"]:
        return "waits for the parts before it"
    notes = len(ledger.list_diagnoses(conn, row["id"]))
    return f"attempt {row['attempt']}" + (f", {notes} lead notes" if notes else "")


def settle(conn: sqlite3.Connection, decision_id: int, answer: str | None) -> None:
    """Eli's answer to a proposal or suggestion: ``approve``/``apply`` or ``dismiss`` (None)."""
    d = ledger.get_decision(conn, decision_id)
    if d is None or d["status"] != "open":
        raise BossError(f"decision {decision_id} is already {d['status'] if d else 'gone'}")
    if answer is None:
        ledger.dismiss_decision(conn, decision_id)
    else:
        ledger.answer_decision(conn, decision_id, answer)


def release_approved(
    root: Path,
    conn: sqlite3.Connection,
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    cat: dict | None = None,
    log: Callable[[str], None] = run._log,
) -> list[int]:
    """Release every waiting proposal Eli approved; dismiss the ones he dismissed. Returns released ids."""
    released = []
    for p in proposals():
        if p["status"] != "waiting":
            continue
        d = ledger.get_decision(conn, p["decision_id"])
        if d is None or d["status"] == "open":
            continue
        if d["status"] == "dismissed" or not str(d["answer"] or "").lower().startswith("approve"):
            p["status"] = "dismissed"
            ledger.update_plan(conn, p["id"], status="done")
            _save(p)
            continue
        # Marked first: a release that fails halfway must not run twice.
        p["status"] = "releasing"
        _save(p)
        part_ids: list[list[int]] = []
        goal_ids: list[int] = []
        try:
            for g in p["goals"]:
                after = tuple(pid for a in g["after"] for pid in part_ids[a - 1])
                rows = run.release_goal(
                    root, conn, p["target"], g["draft"], plan_id=p["id"], after=after,
                    runner=runner, cat=cat, log=log,
                )
                part_ids.append([r["part_id"] for r in rows])
                g["goal_ids"] = [r["id"] for r in rows]
                goal_ids += g["goal_ids"]
        except Exception as e:
            p["status"], p["goal_ids"], p["error"] = "failed", goal_ids, str(e)
            _save(p)
            raise BossError(f"proposal {p['id']} released {len(goal_ids)} goals, then: {e}") from e
        p["status"], p["goal_ids"] = "released", goal_ids
        ledger.update_plan(conn, p["id"], status="active")
        _save(p)
        released.append(p["id"])
        log(f"released proposal {p['id']}: {p['headline']} ({len(goal_ids)} goals)")
    return released


# --- project notes ------------------------------------------------------------------


def notes_path(target: str) -> Path:
    return _dir(target) / "project.md"


def read_notes(target: str) -> str:
    path = notes_path(target)
    return path.read_text() if path.exists() else ""


def write_notes(root: Path, target: str, text: str) -> int:
    """Replace the project notes; refused past ``boss.notes_chars``. Returns the length."""
    targets.require(target)
    cap = settings.load(root)["boss.notes_chars"]
    text = text.strip() + "\n"
    if len(text) > cap:
        raise BossError(f"notes are {len(text)} characters; the cap is {cap}. Trim, then write again.")
    path = notes_path(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return len(text)


# --- reading the ledger compactly ----------------------------------------------------


def _line(text: str | None, n: int = 90) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


def status(conn: sqlite3.Connection, target: str) -> str:
    """What is running, waiting for Eli, and what came back lately; a few hundred tokens."""
    targets.require(target)
    out = [f"target {target}"]
    active = ledger.active_goals(conn, target)
    out.append(f"running goals: {len(active)}")
    for g in active[:10]:
        state = f"attempt {g['attempt']}" if g["current_task"] else "waits for its dependencies"
        out.append(f"  goal {g['id']}: {state} - {_line(g['goal'], 70)}")
    waiting = [p for p in proposals(target) if p["status"] == "waiting"]
    out.append(f"proposals waiting for Eli: {len(waiting)}")
    out += [f"  proposal {p['id']}: {p['headline']}" for p in waiting[:5]]
    open_d = [d for d in ledger.list_decisions(conn, target) if not d["source"].startswith("boss-plan")]
    out.append(f"other open decisions: {len(open_d)}")
    out += [f"  decision {d['id']}: {_line(d['question'])}" for d in open_d[:5]]
    done = [g for g in ledger.list_goals(conn, target, all_goals=True) if g["outcome"]][:8]
    out.append("latest results:")
    out += [f"  goal {g['id']} {g['outcome']}: {_line(g['goal'], 60)} - {_line(g['detail'], 60)}" for g in done]
    rates = ledger.order_rates(conn, target)
    out.append(
        "implement attempts passed: "
        + ", ".join(f"{name} {p}/{n}" for name, (p, n) in rates.items())
    )
    return "\n".join(out)


def findings(conn: sqlite3.Connection, target: str, limit: int = 40) -> str:
    """Where the team loses time: failures, what the lead diagnosed, refused plans, tokens per landing."""
    targets.require(target)
    jobs = ledger.list_jobs(conn, target=target, limit=limit)
    failed = [j for j in jobs if j["outcome"] in ("fail", "error", "timeout") and j["role"] != "lead"]
    out = [f"last {len(jobs)} jobs: {len(failed)} worker attempts failed"]
    kinds = Counter(_line(j["grader_detail"], 50) for j in failed)
    out += [f"  {n}x {kind}" for kind, n in kinds.most_common(6)]
    lead_bad = [j for j in jobs if j["role"] == "lead" and j["outcome"] != "pass" and j["outcome"]]
    out.append(f"lead calls that failed or were refused: {len(lead_bad)}")
    out += [f"  {_line(j['grader_detail'])}" for j in lead_bad[:4]]
    rows = conn.execute(
        "SELECT d.cause, d.action FROM diagnoses d JOIN goals g ON g.id = d.goal_id"
        " WHERE g.target = ? ORDER BY d.id DESC LIMIT 10",
        (target,),
    ).fetchall()
    out.append(f"lead diagnoses: {len(rows)}")
    out += [f"  {r['action']}: {_line(r['cause'])}" for r in rows]
    cost = conn.execute(
        "SELECT g.id, SUM(c.tokens_in) AS tin, COUNT(c.id) AS n FROM goals g"
        " JOIN jobs j ON j.part_id = g.part_id OR (g.part_id IS NULL AND j.id = g.job_id)"
        " JOIN calls c ON c.job_id = j.id"
        " WHERE g.target = ? AND g.outcome = 'pass' AND j.role != 'lead'"
        " GROUP BY g.id ORDER BY g.id DESC LIMIT 5",
        (target,),
    ).fetchall()
    if cost:
        out.append("worker input tokens per landed goal (newest first):")
        out += [f"  goal {r['id']}: {r['tin']} over {r['n']} calls" for r in cost]
    return "\n".join(out)


# --- suggestions ------------------------------------------------------------------


def suggest(conn: sqlite3.Connection, target: str, title: str, change: str, why: str) -> int:
    """A proposed change to how Team works, as a decision; nothing changes until Eli applies it."""
    title, change, why = _line(title, HEADLINE_CHARS), change.strip(), why.strip()
    if not title or not change or not why:
        raise BossError("a suggestion needs a title, the change, and why (with the evidence)")
    path = _dir(target) / "suggestions.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    decision = ledger.create_decision(
        conn, target, title, f"{change}\n\nWhy: {why}", SUGGESTION_OPTIONS, "apply",
        source="boss-suggestion",
    )
    with path.open("a") as f:
        f.write(json.dumps({"decision_id": decision, "title": title, "change": change,
                            "why": why, "created": ledger._now()}) + "\n")
    return decision
