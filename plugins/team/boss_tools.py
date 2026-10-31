"""The boss's tools (toolset ``team``): thin wrappers over ``team.boss``.

Only a profile with ``team`` in its toolsets sees them (the boss). Each opens
the ledger, does one thing, and returns short text: the boss pays for every
character it reads, so nothing here dumps raw rows. There is deliberately no
tool that approves or releases a goal; that answer is Eli's.
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
    project = args.get("project")
    with _conn() as conn:
        p = boss.propose(
            _REPO,
            conn,
            target,
            str(args.get("headline") or ""),
            str(args.get("summary") or ""),
            list(args.get("goals") or []),
            notes=str(args.get("notes") or ""),
            project=int(project) if project not in (None, "", 0) else None,
        )
    new = p["goals"][-len(args.get("goals") or []):]
    nums = ", ".join(str(g["n"]) for g in new)
    return (
        f"project {p['id']}: goal{'s' if len(new) > 1 else ''} {nums} are cards in Eli's inbox now. "
        "He approves, skips or sends each back; an approved goal is drafted by the lead and starts "
        f"within minutes. Add more with project={p['id']}. Do not wait for him."
    )


def ask(args: dict) -> str:
    target = _target(args)
    project = args.get("project")
    with _conn() as conn:
        d = boss.ask(
            conn,
            target,
            str(args.get("question") or ""),
            list(args.get("options") or []),
            str(args.get("recommendation") or ""),
            context=str(args.get("context") or ""),
            project=int(project) if project not in (None, "", 0) else None,
        )
    return f"question {d} is a card in Eli's inbox; his answer arrives as your next message. End your turn."


def proposals(args: dict) -> str:
    rows = boss.proposals(_target(args))[:10]
    if not rows:
        return "(no projects)"
    out = []
    for p in rows:
        out.append(f"project {p['id']} {p['status']}: {p['headline']}")
        out += [
            f"  goal {g['n']} {g['status']}: {g['goal'][:70]}" + (f" (sent back: {g['note']})" if g.get("note") else "")
            for g in p["goals"]
        ]
    return "\n".join(out)


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


def harness_read(args: dict) -> str:
    return boss.harness_read(_REPO, str(args.get("role") or ""))


def propose_harness(args: dict) -> str:
    target = _target(args)
    with _conn() as conn:
        r = boss.propose_harness(
            _REPO,
            conn,
            target,
            str(args.get("headline") or ""),
            str(args.get("why") or ""),
            list(args.get("edits") or []),
            log=lambda _m: None,
        )
    return (
        f"harness change {r['id']} is on {r['branch']}: replaying {r['items']} finished orders "
        f"on {' and '.join(r['arms'])}. The counts go to Eli's inbox when the replay finishes; "
        "team_status shows it meanwhile."
    )


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
        "Open a project, or add goals to an open one (project=<id>). Each goal becomes a card in "
        "Eli's inbox at once; he approves, skips or sends it back. The lead drafts an approved goal "
        "into worker orders and Team builds it; goals with no `after` build at the same time.",
        {
            "target": _TARGET,
            "project": {"type": "integer", "description": "An open project's id, to add goals to it."},
            "headline": {"type": "string", "description": "One sentence: what this project does. Required for a new project."},
            "summary": {"type": "string", "description": "The deeper idea in a short paragraph: why, what changes, what done looks like. Required for a new project."},
            "goals": {
                "type": "array",
                "description": "1 to 5 at a time. Each goal is one change a check can verify, small enough for one worker.",
                "items": {
                    "type": "object",
                    "properties": {
                        "goal": {"type": "string"},
                        "after": {"type": "array", "items": {"type": "integer"}, "description": "Numbers of earlier goals in this project that must land first. Leave empty when it can build alongside them."},
                    },
                    "required": ["goal"],
                },
            },
            "notes": {"type": "string", "description": "What the lead should know that the repo does not say."},
        },
        ["goals"],
        propose,
    ),
    (
        "team_ask",
        "Ask Eli one question as a card with option buttons and your recommendation. His answer "
        "arrives as your next message, so end your turn after asking. Use it for what only he can "
        "decide; decide small things yourself.",
        {
            "target": _TARGET,
            "question": {"type": "string", "description": "One sentence."},
            "options": {"type": "array", "items": {"type": "string"}, "description": "2 to 4 short options."},
            "recommendation": {"type": "string", "description": "One of the options."},
            "context": {"type": "string", "description": "Two or three lines: what hangs on it."},
            "project": {"type": "integer", "description": "The open project it belongs to, if any."},
        },
        ["question", "options", "recommendation"],
        ask,
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
    (
        "team_harness_read",
        "A worker role's files as they are now (SOUL.md, role.toml, skills), to rewrite for team_propose_harness.",
        {"role": {"type": "string", "description": "The role, e.g. implement."}},
        ["role"],
        harness_read,
    ),
    (
        "team_propose_harness",
        "Propose a change to a worker role's own files. Team puts it on a branch, re-runs the replay set of finished "
        "worker orders on both versions, and sends Eli the pass counts and worker tokens next to the base rate. "
        "Nothing changes until Eli adopts it.",
        {
            "target": _TARGET,
            "headline": {"type": "string", "description": "One sentence: what changes for the role."},
            "why": {"type": "string", "description": "The evidence from team_findings: counts next to the base rate."},
            "edits": {
                "type": "array",
                "description": "The whole new content of each file: team/roles/<role>/SOUL.md, role.toml or skills/<name>/SKILL.md.",
                "items": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                    "required": ["path", "content"],
                },
            },
        },
        ["headline", "why", "edits"],
        propose_harness,
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
