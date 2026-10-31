"""What the boss works with: projects Eli approves goal by goal, notes, status, findings.

The boss is the agent Eli talks to (``team/roles/boss``, a Hermes profile on
Claude through the subscription). It never reads code and never touches the
board directly: its tools (``plugins/team``) call the functions here.

- A **project** is what the boss builds with Eli: one sentence, a deeper
  summary, and goals added as they are worked out. Every goal is a card in
  Eli's inbox the moment the boss writes it; Eli approves, skips or sends it
  back one by one (the boss has no tool that can). ``team run`` drafts each
  approved goal with the lead (``run.draft_goal``) and releases it, so the
  card fills in with the parts each worker gets as they are drafted and
  built. The project closes itself when no goal is open.
- A **question** (``ask``) is a card with options and the boss's
  recommendation; Eli's answer comes back as the boss's next message.
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
import re
import shutil
import sqlite3
import subprocess
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any

from team import grade, ledger, profiles, replay, roles, run, settings, targets
from team.goals_landed import all_goals_landed

HEADLINE_CHARS = 160
SUMMARY_CHARS = 1500
PLAN_OPTIONS = ["approve", "dismiss"]
SUGGESTION_OPTIONS = ["apply", "dismiss"]
HARNESS_OPTIONS = ["adopt", "dismiss"]
HARNESS_FILE_CHARS = 20000
HARNESS_EDITS = 6
HARNESS_PATH = re.compile(
    r"^team/roles/([a-z][a-z0-9_-]*)/(SOUL\.md|role\.toml|skills/[a-z0-9_-]+/SKILL\.md)$"
)


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
        raise BossError(f"no project {plan_id} on {target}")
    return _upgrade(json.loads(path.read_text()))


def proposals(target: str | None = None) -> list[dict]:
    """Every project, newest first."""
    names = [target] if target else [t["name"] for t in targets.list_targets()]
    out = []
    for name in names:
        d = _dir(name) / "proposals"
        if d.is_dir():
            out += [_upgrade(json.loads(f.read_text())) for f in d.glob("*.json")]
    return sorted(out, key=lambda p: p["id"], reverse=True)


# --- projects --------------------------------------------------------------------
#
# A project (a ``plans`` row, saved as ``proposals/<id>.json``) is a headline, a
# summary, and the goals the boss adds as it works through the idea with Eli.
# Each goal is its own decision in the inbox: Eli approves or skips them one by
# one, and ``release_approved`` drafts (with the lead) and releases each approved
# goal on the loop's next ticks. A goal with ``after`` is released too; its parts
# wait on the board until those goals' parts have landed. Goals without one
# build at the same time.

GOAL_OPTIONS = ["approve", "skip"]
ASK_OPTIONS = (2, 4)
GOAL_OPEN = ("proposed", "approved", "drafting", "released")
GOAL_SETTLED = ("landed", "stopped", "skipped", "sent_back", "failed")
PROJECT_STATUSES = ("open", "closed", "dismissed")
_OLD_GOAL_STATUS = {
    "waiting": "proposed", "accepted": "proposed", "releasing": "approved", "released": "released",
    "closed": "landed", "dismissed": "skipped", "failed": "failed",
}


def _upgrade(p: dict) -> dict:
    """A proposal saved before goals had their own cards: one plan-level decision."""
    if p.get("status") in PROJECT_STATUSES and all("status" in g for g in p["goals"]):
        return p
    old = p.get("status")
    for n, g in enumerate(p["goals"], 1):
        g.setdefault("n", n)
        g.setdefault("status", _OLD_GOAL_STATUS.get(old, "skipped"))
        g.setdefault("goal_ids", [])
        g.setdefault("decision_id", p.get("decision_id"))
        g.setdefault("notes", "")
    p["status"] = old if old in ("closed", "dismissed") else "open"
    return p


def goal_of(p: dict, n: int) -> dict:
    for g in p["goals"]:
        if g["n"] == n:
            return g
    raise BossError(f"project {p['id']} has no goal {n}")


def plan_of(decision: dict) -> int | None:
    """The project a ``boss-goal:<id>:<n>`` or ``boss-ask:<id>`` decision belongs to."""
    kind, _, rest = decision["source"].partition(":")
    pid = rest.split(":")[0]
    if kind in ("boss-goal", "boss-ask", "boss-plan") and pid.isdigit() and int(pid):
        return int(pid)
    return None


def _goal_decision(conn: sqlite3.Connection, p: dict, g: dict) -> int:
    return ledger.create_decision(
        conn, p["target"], g["goal"], p["summary"], GOAL_OPTIONS, "approve",
        source=f"boss-goal:{p['id']}:{g['n']}",
    )


def propose(
    root: Path,
    conn: sqlite3.Connection,
    target: str,
    headline: str,
    summary: str,
    goals: list[dict],
    *,
    notes: str = "",
    project: int | None = None,
) -> dict:
    """Open a project, or add goals to an open one; every goal is a card in Eli's inbox at once.

    Nothing is drafted here. The lead drafts a goal once Eli approves it
    (``release_approved``), so a skipped goal costs no lead tokens and the
    cards appear as fast as the boss writes them. ``notes`` is what the lead
    should know that the repo does not say; it is kept with these goals.
    Returns the project.
    """
    targets.require(target)
    cfg = settings.load(root)
    headline, summary = " ".join(headline.split()), summary.strip()
    if len(headline) > HEADLINE_CHARS or (project is None and not headline):
        raise BossError(f"headline must be one sentence of at most {HEADLINE_CHARS} characters")
    if len(summary) > SUMMARY_CHARS or (project is None and not summary):
        raise BossError(f"summary must be 1 to {SUMMARY_CHARS} characters")
    if not goals or len(goals) > cfg["boss.max_goals"]:
        raise BossError(f"add 1 to {cfg['boss.max_goals']} goals at a time")
    if project is None:
        p = {
            "id": None, "target": target, "headline": headline, "summary": summary,
            "status": "open", "goals": [], "created": ledger._now(),
        }
    else:
        p = load_proposal(target, int(project))
        if p["status"] != "open":
            raise BossError(f"project {p['id']} is {p['status']}; open a new one")
        p["headline"], p["summary"] = headline or p["headline"], summary or p["summary"]
    first = len(p["goals"]) + 1
    new = []
    for i, g in enumerate(goals):
        n = first + i
        text = str(g.get("goal") or "").strip()
        after = [int(a) for a in g.get("after") or []]
        if not text:
            raise BossError(f"goal {n} is empty")
        if any(a < 1 or a >= n for a in after):
            raise BossError(f"goal {n}: after may only name earlier goals (1 to {n - 1})")
        new.append(
            {
                "n": n, "goal": text, "after": after, "check": g.get("check") or None,
                "status": "proposed", "notes": notes, "goal_ids": [], "created": ledger._now(),
            }
        )
    if p["id"] is None:
        p["id"] = ledger.create_plan(conn, target, headline, summary=summary, status="draft")
    for g in new:
        g["decision_id"] = _goal_decision(conn, p, g)
        p["goals"].append(g)
    _save(p)
    return p


def send_back(conn: sqlite3.Connection, target: str, plan_id: int, n: int, text: str) -> dict:
    """Eli hands a proposed goal back to the boss with what should change; its card closes."""
    text = " ".join(text.split())
    if not text:
        raise BossError("say what should change")
    p = load_proposal(target, plan_id)
    g = goal_of(p, n)
    if g["status"] != "proposed":
        raise BossError(f"goal {n} of project {plan_id} is {g['status']}")
    d = ledger.get_decision(conn, g["decision_id"])
    if d is not None and d["status"] == "open":
        ledger.dismiss_decision(conn, g["decision_id"])
    g["status"], g["note"] = "sent_back", text
    _save(p)
    return g


def settle_goals(conn: sqlite3.Connection, p: dict, answer: str | None) -> list[int]:
    """Approve (``"approve"``) or skip (``None``) every goal of ``p`` still waiting for Eli."""
    done = []
    for g in p["goals"]:
        d = ledger.get_decision(conn, g["decision_id"]) if g["status"] == "proposed" else None
        if d is not None and d["status"] == "open":
            settle(conn, d["id"], answer)
            done.append(g["n"])
    if not done:
        raise BossError(f"project {p['id']} has no goal waiting for Eli")
    return done


def ask(
    conn: sqlite3.Connection,
    target: str,
    question: str,
    options: list[str],
    recommendation: str,
    context: str = "",
    project: int | None = None,
) -> int:
    """The boss's question for Eli as a card with option buttons; the answer comes back as
    the boss's next message. Returns the decision id."""
    targets.require(target)
    question = " ".join(question.split())
    options = [" ".join(str(o).split()) for o in options if str(o).strip()]
    recommendation = " ".join(str(recommendation).split())
    if not question or len(question) > HEADLINE_CHARS:
        raise BossError(f"a question is one sentence of at most {HEADLINE_CHARS} characters")
    if not ASK_OPTIONS[0] <= len(options) <= ASK_OPTIONS[1] or len(set(options)) != len(options):
        raise BossError(f"give {ASK_OPTIONS[0]} to {ASK_OPTIONS[1]} distinct options")
    if recommendation not in options:
        raise BossError("the recommendation must be one of the options")
    if project is not None:
        p = load_proposal(target, int(project))
        if p["status"] != "open":
            raise BossError(f"project {p['id']} is {p['status']}")
    return ledger.create_decision(
        conn, target, question, context.strip(), options, recommendation,
        source=f"boss-ask:{int(project) if project is not None else 0}",
    )


def _ancestors(goals: list[dict], i: int) -> set[int]:
    """Indexes of the goals goal ``i`` waits for, directly or through others."""
    seen: set[int] = set()
    todo = [a - 1 for a in goals[i]["after"]]
    while todo:
        j = todo.pop()
        if j not in seen:
            seen.add(j)
            todo += [a - 1 for a in goals[j]["after"]]
    return seen


def _owned(g: dict) -> set[str]:
    return {f for p in (g.get("draft") or {}).get("parts", []) for f in p["owned_paths"]}


def _brief(n: int, g: dict) -> dict:
    return {
        "goal": n,
        "text": g["goal"],
        "parts": [
            {"title": p["title"], "owned_paths": p["owned_paths"], "interfaces": p.get("interfaces", [])}
            for p in (g.get("draft") or {}).get("parts", [])
        ],
    }


def plan_context(goals: list[dict], i: int) -> dict:
    """What the lead drafting goal ``i`` must know about the project's other goals.

    ``before``: goals it waits for; they land first, so it builds on them and must
    not plan them again. ``alongside``: other live goals that may run at the same
    time; their files are not its to own. Skipped and sent-back goals are left out.
    """
    up = _ancestors(goals, i)
    before = [_brief(j + 1, goals[j]) for j in sorted(up)]
    alongside = [
        {"goal": j + 1, "text": goals[j]["goal"], "owned_paths": sorted(_owned(goals[j]))}
        for j in range(len(goals))
        if j != i and j not in up and goals[j]["status"] in GOAL_OPEN
    ]
    return {"before": before, "alongside": alongside}


def overlaps(goals: list[dict]) -> list[str]:
    """Files more than one drafted goal's parts change: a conflict when the goals run at the
    same time, and worth a look when one builds on the other (it may be redoing that work)."""
    out = []
    for i in range(len(goals)):
        if goals[i]["status"] in ("skipped", "sent_back"):
            continue
        up = _ancestors(goals, i)
        mine = _owned(goals[i])
        for j in range(i):
            if goals[j]["status"] in ("skipped", "sent_back"):
                continue
            shared = sorted(mine & _owned(goals[j]))
            if not shared:
                continue
            files = ", ".join(shared)
            if j in up:
                out.append(f"goal {i + 1} changes {files} again after goal {j + 1}: check it builds on that work instead of redoing it")
            else:
                out.append(f"goals {j + 1} and {i + 1} run at the same time and both change {files}: they will conflict")
    return out


def card(p: dict) -> dict:
    """What Eli sees: the project, and each goal with its state and, once drafted, its parts."""
    goals = []
    for g in p["goals"]:
        d = g.get("draft")
        goals.append(
            {
                "n": g["n"],
                "goal": g["goal"],
                "after": g["after"],
                "status": g["status"],
                "decision_id": g.get("decision_id"),
                "plan": d["summary"] if d else None,
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
                    for part in (d["parts"] if d else [])
                ],
                "goal_ids": g.get("goal_ids", []),
                "error": g.get("error"),
                "note": g.get("note"),
            }
        )
    return {
        "id": p["id"],
        "target": p["target"],
        "headline": p["headline"],
        "summary": p["summary"],
        "status": p["status"],
        "goals": goals,
        "goal_ids": [i for g in p["goals"] for i in g.get("goal_ids", [])],
        "counts": dict(Counter(g["status"] for g in p["goals"])),
        "warnings": overlaps(p["goals"]),
        "created": p.get("created"),
        "closed": p.get("closed"),
    }


def waiting(target: str | None = None) -> list[dict]:
    """Every goal that waits for Eli, oldest project first."""
    out = []
    for p in sorted(proposals(target), key=lambda x: x["id"]):
        if p["status"] != "open":
            continue
        out += [
            {"plan_id": p["id"], "headline": p["headline"], "n": g["n"], "goal": g["goal"]}
            for g in p["goals"]
            if g["status"] == "proposed"
        ]
    return out


# The tab draws these top to bottom: Boss, Planner (the lead), Implementer, Landed.
STATIONS = ("boss", "lead", "workers", "landed")


def flow(conn: sqlite3.Connection, target: str | None = None, limit: int = 20) -> list[dict]:
    """Every recent project, goal by goal, with the station each goal is at.

    A goal sits with the **boss** while its card waits for Eli, with the
    **lead** once approved and until it is released, with the **workers**
    while any of its parts is open, and is **landed** when every part passed.
    ``stopped`` means a part failed and nothing of it is still running;
    ``dismissed`` is a goal Eli skipped or sent back.
    """
    out = []
    for p in proposals(target)[:limit]:
        c = card(p)
        for g, raw in zip(c["goals"], p["goals"], strict=True):
            rows = [ledger.get_goal(conn, i) for i in raw.get("goal_ids") or []]
            for part, row in zip(g["parts"], rows, strict=False):
                part["state"] = _part_state(conn, row)
            status = raw["status"]
            if status == "proposed":
                g["station"] = "boss"
            elif status in ("approved", "drafting"):
                g["station"] = "lead"
            elif status in ("skipped", "sent_back"):
                g["station"] = "dismissed"
            elif status == "failed":
                g["station"] = "stopped"
            elif rows and all(r["outcome"] == "pass" for r in rows):
                g["station"] = "landed"
            elif any(r["outcome"] is None for r in rows):
                g["station"] = "workers"
            else:
                g["station"] = "stopped"
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
    """Eli's answer to a card: the option he chose, or ``None`` to dismiss it."""
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
    max_drafts: int = 1,
) -> list[int]:
    """Record Eli's answers on goal cards, then draft and release approved goals.

    At most ``max_drafts`` goals are drafted per call, so one tick never waits
    on the lead for more than one goal. A goal whose ``after`` goals are not
    released yet waits for them: their part ids are what its parts wait on.
    Returns the goal ids released.
    """
    released: list[int] = []
    drafts = 0
    for p in proposals():
        if p["status"] != "open":
            continue
        changed = False
        for g in p["goals"]:
            if g["status"] == "drafting":  # the loop died mid-draft; nothing of it landed
                g["status"], changed = "approved", True
            if g["status"] != "proposed":
                continue
            d = ledger.get_decision(conn, g["decision_id"])
            if d is None or d["status"] == "open":
                continue
            yes = d["status"] == "answered" and str(d["answer"] or "").lower().startswith("approve")
            g["status"], changed = ("approved" if yes else "skipped"), True
        for i, g in enumerate(p["goals"]):
            if g["status"] != "approved" or drafts >= max_drafts:
                continue
            deps = [goal_of(p, a) for a in g["after"]]
            if any(x["status"] in ("proposed", "approved", "drafting") for x in deps):
                continue
            drafts += 1
            g["status"] = "drafting"  # marked first: a draft that dies must not run twice in one tick
            _save(p)
            try:
                g["draft"] = run.draft_goal(
                    root, conn, p["target"], g["goal"], check=g["check"], notes=g.get("notes") or "",
                    runner=runner, cat=cat, log=log, plan_context=plan_context(p["goals"], i),
                )
                after = tuple(pid for x in deps for pid in x.get("part_ids") or [])
                rows = run.release_goal(
                    root, conn, p["target"], g["draft"], plan_id=p["id"], after=after,
                    runner=runner, cat=cat, log=log,
                )
            except Exception as e:
                g["status"], g["error"], changed = "failed", str(e), True
                _save(p)
                log(f"project {p['id']} goal {g['n']} could not start: {e}")
                continue
            g["part_ids"] = sorted({r["part_id"] for r in rows})
            g["goal_ids"] = [r["id"] for r in rows]
            g["status"], changed = "released", True
            released += g["goal_ids"]
            ledger.update_plan(conn, p["id"], status="active")
            log(f"project {p['id']} goal {g['n']} released: {g['goal'][:60]} ({len(rows)} parts)")
        if changed:
            _save(p)
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


def record_closed(conn: sqlite3.Connection, target: str) -> list[dict]:
    """Settle each released goal from the ledger, and close a project once no goal is open.

    A project whose goals were all skipped or sent back is ``dismissed``; one
    where anything was built is ``closed``. Returns the projects closed now.
    """
    out = []
    for p in proposals(target):
        if p["status"] != "open":
            continue
        changed = False
        for g in p["goals"]:
            if g["status"] != "released":
                continue
            rows = [r for r in (ledger.get_goal(conn, i) for i in g.get("goal_ids", [])) if r]
            if all_goals_landed(p, rows):
                g["status"], changed = "landed", True
            elif rows and all(r["outcome"] for r in rows):
                g["status"], changed = "stopped", True
        # a goal sent back is the boss's to re-propose, so its project stays open too
        if any(g["status"] in GOAL_OPEN or g["status"] == "sent_back" for g in p["goals"]):
            if changed:
                _save(p)
            continue
        built = any(g["status"] in ("landed", "stopped", "failed") for g in p["goals"])
        p["status"] = "closed" if built else "dismissed"
        p["closed"] = ledger._now()[:10]
        ledger.update_plan(conn, p["id"], status="done")
        _save(p)
        out.append(p)
    return out


def status(conn: sqlite3.Connection, target: str) -> str:
    """What is running, waiting for Eli, and what came back lately; a few hundred tokens."""
    targets.require(target)
    record_closed(conn, target)
    out = [f"target {target}"]
    active = ledger.active_goals(conn, target)
    out.append(f"running goals: {len(active)}")
    for g in active[:10]:
        state = (
            f"attempt {g['attempt']}"
            if g["current_task"]
            else "waits for its dependencies"
        )
        out.append(f"  goal {g['id']}: {state} - {_line(g['goal'], 70)}")
    projects = proposals(target)
    open_p = [p for p in projects if p["status"] == "open"]
    out.append(f"open projects: {len(open_p)}")
    for p in open_p[:5]:
        counts = Counter(g["status"] for g in p["goals"])
        out.append(
            f"  project {p['id']}: {_line(p['headline'], 60)} - "
            + ", ".join(f"{n} {s}" for s, n in sorted(counts.items()))
        )
    wait = [(p, g) for p in open_p for g in p["goals"] if g["status"] == "proposed"]
    out.append(f"goals waiting for Eli: {len(wait)}")
    out += [f"  project {p['id']} goal {g['n']}: {_line(g['goal'], 70)}" for p, g in wait[:5]]
    back = [(p, g) for p in open_p for g in p["goals"] if g["status"] == "sent_back"]
    for p, g in back[:5]:
        out.append(f"sent back: project {p['id']} goal {g['n']} - {_line(g.get('note'), 90)}")
    asks = [d for d in ledger.list_decisions(conn, target) if d["source"].startswith("boss-ask")]
    out.append(f"your questions waiting for Eli: {len(asks)}")
    out += [f"  decision {d['id']}: {_line(d['question'])}" for d in asks[:5]]
    replaying = ledger.list_harness(conn, target, status="evaluating")
    judged = ledger.list_harness(conn, target, status="waiting")
    if replaying or judged:
        out.append(
            f"harness changes: {len(replaying)} being replayed, {len(judged)} waiting for Eli"
        )
        out += [
            f"  harness {r['id']} ({r['status']}): {_line(r['headline'], 70)}"
            for r in (replaying + judged)[:5]
        ]
    closed = [p for p in projects if p["status"] == "closed"]
    if closed:
        for p in closed[:5]:
            out.append(f"closed: {_line(p['headline'], 80)}, {p['closed'][:10]}")
    else:
        out.append("closed projects: 0")
    open_d = [
        d for d in ledger.list_decisions(conn, target)
        if not d["source"].startswith(("boss-plan", "boss-goal", "boss-ask"))
    ]
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
    from team import model_tracking

    out += model_tracking.findings(conn, jobs)
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
        f.write(
            json.dumps(
                {
                    "decision_id": decision,
                    "title": title,
                    "change": change,
                    "why": why,
                    "created": ledger._now(),
                }
            )
            + "\n"
        )
    return decision


# --- harness changes ---------------------------------------------------------------


def harness_read(root: Path, role: str) -> str:
    """A role's files as they are on main, each under its path, for the boss to rewrite."""
    root = Path(root)
    if role not in roles.names(root):
        raise BossError(f"no role {role!r}; roles: {', '.join(roles.names(root))}")
    out = []
    for p in sorted(x for x in (roles.roles_dir(root) / role).rglob("*") if x.is_file()):
        out.append(f"=== {p.relative_to(root)}\n{p.read_text()}")
    return "\n".join(out)


def propose_harness(
    root: Path,
    conn: sqlite3.Connection,
    target: str,
    headline: str,
    why: str,
    edits: list[dict],
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    cat: dict | None = None,
    log: Callable[[str], None] = run._log,
) -> dict:
    """Put a change to a worker role's files on a branch and start grading it on the replay set.

    ``edits`` are ``{path, content}``: the whole new file, for ``team/roles/<role>/SOUL.md``,
    ``role.toml`` or ``skills/<name>/SKILL.md``. The branch arm runs the set on a profile
    synced from the branch; the base arm runs it on the live profile when the set has no
    measurement for the role's current files. The result becomes a decision for Eli.
    """
    targets.require(target)
    root = Path(root)
    cfg = settings.load(root)
    headline, why = " ".join(headline.split()), why.strip()
    if not headline or len(headline) > HEADLINE_CHARS:
        raise BossError(f"headline must be one sentence of at most {HEADLINE_CHARS} characters")
    if not why:
        raise BossError("say why, with the counts from team_findings next to the base rate")
    if not edits or len(edits) > HARNESS_EDITS:
        raise BossError(f"a harness change edits 1 to {HARNESS_EDITS} files")
    touched: dict[str, list[tuple[str, str]]] = {}
    for e in edits:
        path, content = str((e or {}).get("path") or "").strip(), (e or {}).get("content")
        m = HARNESS_PATH.match(path)
        if not m:
            raise BossError(
                f"{path!r}: a harness change edits team/roles/<role>/SOUL.md, role.toml or "
                "skills/<name>/SKILL.md, nothing else"
            )
        role = m.group(1)
        if role == "boss":
            raise BossError("the boss does not change its own files; use team_suggest")
        if role not in roles.names(root):
            raise BossError(f"no role {role!r}; roles: {', '.join(roles.names(root))}")
        if not isinstance(content, str) or not content.strip():
            raise BossError(f"{path}: content is the whole new file and cannot be empty")
        if len(content) > HARNESS_FILE_CHARS:
            raise BossError(f"{path}: at most {HARNESS_FILE_CHARS} characters")
        current = root / path
        if current.exists() and current.read_text() == content:
            raise BossError(f"{path}: that is what the file already says")
        touched.setdefault(role, []).append((path, content))
    for role in touched:
        if roles.load(root, role, cat)["runs_on"]["harness"] != "hermes":
            raise BossError(
                f"{role} does not run as a Hermes worker; the replay set re-runs worker orders, "
                "so only worker roles can be graded this way. Use team_suggest for it."
            )
    s = replay.load_set(target)
    if s is None:
        try:
            s = replay.build_set(conn, target, cfg["replay.size"], runner=runner)
        except replay.ReplayError as e:
            raise BossError(str(e)) from e
        replay.save_set(s)
    review_id = ledger.create_harness(conn, target, headline, why, sorted(touched))
    try:
        path, branch = grade.create_worktree(
            root, "harness", f"harness-{review_id}", base="main", runner=runner
        )
    except grade.GradeError as e:
        ledger.update_harness(
            conn, review_id, status="failed", result={"error": str(e)}, ended=ledger._now()
        )
        raise BossError(str(e)) from e
    try:
        files = []
        for role, pairs in touched.items():
            for rel, content in pairs:
                f = path / rel
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_text(content)
                files.append(rel)
            roles.load(path, role, cat)  # the branch must still describe a loadable role
        sha = grade.commit_paths(path, f"harness {review_id}: {headline}", *files, runner=runner)
        for role in touched:
            profiles.sync(
                path, role, cat, runner=runner, profile=profiles.candidate_profile(role, review_id)
            )
        arms = ["branch"] + ([] if replay.base_is_current(s, root, sorted(touched)) else ["base"])
        ledger.update_harness(conn, review_id, branch=branch)
        goal_ids = replay.start(
            root, conn, review_id, target, s, arms, runner=runner, cat=cat, log=log
        )
    except Exception as e:
        ledger.update_harness(
            conn, review_id, status="failed", result={"error": str(e)}, ended=ledger._now()
        )
        try:
            grade.remove_worktree(root, path, branch, runner=runner)
        except grade.GradeError:
            pass
        raise BossError(f"harness change {review_id} could not start: {e}") from e
    ledger.update_harness(
        conn, review_id, result={"arms": arms, "items": len(s["items"]), "sha": sha}
    )
    log(
        f"harness change {review_id} on {branch}: replaying {len(s['items'])} orders "
        f"on {', '.join(arms)}"
    )
    return {
        "id": review_id,
        "branch": branch,
        "sha": sha,
        "arms": arms,
        "items": len(s["items"]),
        "goals": goal_ids,
    }


def _drop_candidates(r: dict) -> None:
    for role in r["roles"]:
        shutil.rmtree(
            profiles.profile_dir(profiles.candidate_profile(role, r["id"])), ignore_errors=True
        )


def settle_reviews(
    root: Path,
    conn: sqlite3.Connection,
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    cat: dict | None = None,
    log: Callable[[str], None] = run._log,
) -> list[str]:
    """Turn finished replays into a decision for Eli; merge what he adopts. Returns what happened."""
    root = Path(root)
    done: list[str] = []
    for r in ledger.list_harness(conn, status="evaluating"):
        res = replay.results(conn, r["id"])
        if res is None:
            continue
        s = replay.load_set(r["target"]) or {"target": r["target"], "items": [], "base": None}
        if "base" in res:
            s["base"] = {**res["base"], "versions": replay.versions(root, r["roles"]), "review": r["id"]}
            replay.save_set(s)
        base = {k: (s.get("base") or {}).get(k, 0) for k in ("pass", "n", "tokens")}
        rec = replay.verdict(base, res["branch"])
        line = replay.describe(base, res["branch"])
        context = (
            f"{r['why']}\n\nReplay of {res['branch']['n']} finished orders with the changed "
            f"{', '.join(r['roles'])} files: {line}.\nBranch {r['branch']}; "
            f"`git diff main...{r['branch']}` shows the change. Adopt merges it and syncs the "
            "profile; dismiss keeps the branch for later."
        )
        d = ledger.create_decision(
            conn,
            r["target"],
            f"Adopt harness change {r['id']}: {r['headline']}",
            context,
            HARNESS_OPTIONS,
            rec,
            source=f"boss-harness:{r['id']}",
        )
        ledger.update_harness(
            conn,
            r["id"],
            status="waiting",
            decision_id=d,
            result={**(r["result"] or {}), "base": base, "branch": res["branch"], "recommendation": rec},
        )
        done.append(f"harness change {r['id']} replayed: {line}; recommend {rec}")
    for r in ledger.list_harness(conn, status="waiting"):
        d = ledger.get_decision(conn, r["decision_id"]) if r["decision_id"] else None
        if d is None or d["status"] == "open":
            continue
        if d["status"] == "dismissed" or not str(d["answer"] or "").lower().startswith("adopt"):
            ledger.update_harness(conn, r["id"], status="dismissed", ended=ledger._now())
            _drop_candidates(r)
            done.append(f"harness change {r['id']} dismissed; branch {r['branch']} is kept")
            continue
        try:
            sha = grade.land(
                root, r["branch"], f"Adopt harness change {r['id']}: {r['headline']}", "main",
                runner=runner,
            )
        except grade.LandLater as e:
            log(f"harness change {r['id']}: waiting to land: {e}")
            continue
        except grade.GradeError as e:
            ledger.update_harness(
                conn, r["id"], status="failed",
                result={**(r["result"] or {}), "error": str(e)}, ended=ledger._now(),
            )
            done.append(f"harness change {r['id']} could not land: {e}")
            continue
        for role in r["roles"]:
            try:
                profiles.sync(root, role, cat, runner=runner)
            except profiles.ProfileError as e:
                log(f"harness change {r['id']}: landed as {sha[:10]}, but profile {role} did not sync: {e}")
        s = replay.load_set(r["target"])
        if s is not None and (r["result"] or {}).get("branch"):
            s["base"] = {**r["result"]["branch"], "versions": replay.versions(root, r["roles"]), "review": r["id"]}
            replay.save_set(s)
        try:
            grade.remove_worktree(
                root, grade.worktree_path("harness", f"harness-{r['id']}"), r["branch"], runner=runner
            )
        except grade.GradeError as e:
            log(f"harness change {r['id']}: adopted, but {e}")
        ledger.update_harness(
            conn, r["id"], status="adopted", merge_sha=sha, ended=ledger._now()
        )
        _drop_candidates(r)
        done.append(f"harness change {r['id']} adopted as {sha[:10]}")
    for m in done:
        log(m)
    return done
