"""The boss's tools (toolset ``team``): thin wrappers over ``team.boss``.

Only a profile with ``team`` in its toolsets sees them (the boss). Each opens
the ledger, does one thing, and returns short text: the boss pays for every
character it reads, so nothing here dumps raw rows. There is deliberately no
tool that approves or releases a proposal; that answer is Eli's.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from team import boss, ledger, run, targets

_TARGET = {"type": "string", "description": "The target's name, e.g. team."}


def _conn():
    conn = ledger.connect()
    ledger.init(conn)
    return conn


def _target(args: dict) -> str:
    name = str(args.get("target") or "").strip()
    if name:
        return name
    enabled = [t["name"] for t in targets.list_targets() if t.get("enabled", True)]
    if len(enabled) == 1:
        return enabled[0]
    raise boss.BossError(f"say which target: {enabled}")


def _safe(fn):
    def handler(args: dict[str, Any], **_kw) -> str:
        try:
            return fn(args or {})
        except (boss.BossError, run.RunError, targets.TargetError, ValueError) as e:
            return f"refused: {e}"

    return handler


def status(args: dict) -> str:
    target = _target(args)
    with _conn() as conn:
        return boss.status(conn, target)


def findings(args: dict) -> str:
    target = _target(args)
    with _conn() as conn:
        return boss.findings(conn, target)


def notes_read(args: dict) -> str:
    return boss.read_notes(_target(args)) or "(no project notes yet)"


def notes_write(args: dict) -> str:
    n = boss.write_notes(_REPO, _target(args), str(args.get("text") or ""))
    return f"saved, {n} characters"


def propose(args: dict) -> str:
    target = _target(args)
    with _conn() as conn:
        p = boss.propose(
            _REPO,
            conn,
            target,
            str(args.get("headline") or ""),
            str(args.get("summary") or ""),
            list(args.get("goals") or []),
            notes=str(args.get("notes") or ""),
            log=lambda _m: None,
        )
    lines = [f"proposal {p['id']} is in Eli's inbox: {p['headline']}"]
    for g in boss.card(p)["goals"]:
        lines.append(f"goal {g['n']}: {g['plan']}")
        for part in g["parts"]:
            lines.append(
                f"  - {part['title']} [{part['model']}; {', '.join(part['toolsets'])}; "
                f"owns {', '.join(part['owned_paths'])}]"
            )
    return "\n".join(lines)


def proposals(args: dict) -> str:
    rows = boss.proposals(_target(args))[:10]
    if not rows:
        return "(no proposals)"
    return "\n".join(f"proposal {p['id']} {p['status']}: {p['headline']}" for p in rows)


def suggest(args: dict) -> str:
    target = _target(args)
    with _conn() as conn:
        d = boss.suggest(
            conn,
            target,
            str(args.get("title") or ""),
            str(args.get("change") or ""),
            str(args.get("why") or ""),
        )
    return f"suggestion is decision {d} in Eli's inbox"


TOOLS = [
    (
        "team_status",
        "What is running, what waits for Eli, and the latest results on a target.",
        {"target": _TARGET},
        [],
        status,
    ),
    (
        "team_findings",
        "Where attempts fail, what the lead diagnosed, and tokens per landed goal. Read before suggesting an improvement.",
        {"target": _TARGET},
        [],
        findings,
    ),
    (
        "team_notes_read",
        "Read the project notes: purpose, decisions, the larger plan, open questions.",
        {"target": _TARGET},
        [],
        notes_read,
    ),
    (
        "team_notes_write",
        "Replace the project notes with this text (capped; keep it a tight summary, not a log).",
        {"target": _TARGET, "text": {"type": "string"}},
        ["text"],
        notes_write,
    ),
    (
        "team_propose",
        "Put a plan in Eli's inbox. The lead drafts each goal into worker orders first (takes minutes). "
        "Eli approves or dismisses it; Team releases what he approves.",
        {
            "target": _TARGET,
            "headline": {"type": "string", "description": "One sentence: what this plan does."},
            "summary": {"type": "string", "description": "The deeper idea in a short paragraph: why, what changes, what done looks like."},
            "goals": {
                "type": "array",
                "description": "In order. Each goal is one change a check can verify.",
                "items": {
                    "type": "object",
                    "properties": {
                        "goal": {"type": "string"},
                        "after": {"type": "array", "items": {"type": "integer"}, "description": "Numbers (1-based) of earlier goals that must land first."},
                    },
                    "required": ["goal"],
                },
            },
            "notes": {"type": "string", "description": "What the lead should know that the repo does not say."},
        },
        ["headline", "summary", "goals"],
        propose,
    ),
    (
        "team_proposals",
        "Your recent proposals and whether Eli approved them.",
        {"target": _TARGET},
        [],
        proposals,
    ),
    (
        "team_suggest",
        "Propose a change to how Team works (a role's instructions or skills, a setting), with the evidence from team_findings. Goes to Eli's inbox.",
        {
            "target": _TARGET,
            "title": {"type": "string"},
            "change": {"type": "string", "description": "Exactly what to change, in which file."},
            "why": {"type": "string", "description": "The evidence: counts from the ledger, next to the base rate."},
        },
        ["title", "change", "why"],
        suggest,
    ),
]


def register(ctx) -> None:
    for name, description, properties, required, fn in TOOLS:
        parameters: dict[str, Any] = {"type": "object", "properties": properties}
        if required:
            parameters["required"] = required
        ctx.register_tool(
            name=name,
            toolset="team",
            schema={"name": name, "description": description, "parameters": parameters},
            handler=_safe(fn),
            description=description,
        )


def schemas() -> str:
    """The tool schemas as JSON, for measuring what they add to every boss call."""
    return json.dumps([{"name": n, "description": d, "parameters": p} for n, d, p, _, _ in TOOLS])
