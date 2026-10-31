"""The lead: Claude plans each goal and steers the free workers that build it.

A goal goes to the lead before any worker sees it. The lead reads the target
(read-only, in a clean worktree at the goal's base) and returns a plan: the
goal split into parts, and for each part a work order. An order holds the
steps, the files the worker owns, the exact line ranges it must read, the
interfaces to use, what to watch for, and which toolsets, skills and model
the worker gets. A free worker starts from the order instead of exploring the
repo and inventing its own plan.

Code, not the lead, decides whether an order is usable: ``validate_plan``
refuses toolsets the role's profile does not enable, skills the role does not
have, models outside the role's catalog candidates, owned files two parallel
parts share, line ranges that do not exist, and dependency cycles. The lead
gets the refusals back once to fix them. ``render`` turns an order into the
task body; the code excerpts are read from disk by Team, never copied by a
model, and capped at ``lead.context_bytes``.

After a failed check the lead reads the order, the diff and the check output
and says what went wrong and what the retry should do differently
(``diagnose``); the retry stays on a free model unless it says to escalate.

Every lead call uses an isolated Hermes worker through LiteLLM and Portkey
on the subscription (``team.hermes_run``), with read/search tools only.
The prompt carries the JSON schema; Team validates the returned answer.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Any

from team import catalog, claude_run, grade, hermes_run, roles
from team.ledger import state_dir

Runner = Callable[..., subprocess.CompletedProcess[Any]]

ROLE = "lead"
MAX_REF_LINES = 200
DIFF_CHARS = 12000
FAILURE_CHARS = 4000

_REF = {
    "type": "object",
    "required": ["path", "start", "end", "why"],
    "properties": {
        "path": {"type": "string"},
        "start": {"type": "integer"},
        "end": {"type": "integer"},
        "why": {"type": "string"},
    },
}
_STRS = {"type": "array", "items": {"type": "string"}}

PLAN_SCHEMA = {
    "type": "object",
    "required": ["summary", "parts"],
    "properties": {
        "summary": {"type": "string"},
        "parts": {
            "type": "array",
            "items": {
                "type": "object",
                "required": [
                    "key",
                    "title",
                    "role",
                    "depends_on",
                    "owned_paths",
                    "steps",
                    "context",
                    "toolsets",
                    "skills",
                    "model",
                ],
                "properties": {
                    "key": {"type": "string"},
                    "title": {"type": "string"},
                    "role": {"type": "string"},
                    "depends_on": _STRS,
                    "owned_paths": _STRS,
                    "steps": _STRS,
                    "context": {"type": "array", "items": _REF},
                    "interfaces": _STRS,
                    "pitfalls": _STRS,
                    "toolsets": _STRS,
                    "skills": _STRS,
                    "model": {"type": "string"},
                },
            },
        },
    },
}

DIAGNOSIS_SCHEMA = {
    "type": "object",
    "required": ["cause", "note", "action"],
    "properties": {
        "cause": {"type": "string"},
        "note": {"type": "string"},
        "action": {"type": "string", "enum": ["retry", "escalate", "ask"]},
        "steps": _STRS,
        "context": {"type": "array", "items": _REF},
        "question": {"type": "string"},
    },
}


class LeadError(RuntimeError):
    """The lead could not produce a usable answer; ``usage_limited`` when Claude refused on quota."""

    def __init__(
        self, message: str, usage_limited: bool = False, result: dict | None = None
    ):
        super().__init__(message)
        self.usage_limited = usage_limited
        self.result = result


# --- what the lead may choose from ----------------------------------------------


def menu(root: Path, cat: dict | None = None) -> dict[str, dict]:
    """Per active hermes role: the toolsets, skills (with descriptions) and models an order may name."""
    out = {}
    for name in roles.names(root):
        role = roles.load(root, name, cat)
        if role["runs_on"]["harness"] != "hermes" or role["status"] != "active":
            continue
        out[name] = {
            "does": role["does"],
            "toolsets": sorted(set(role.get("tools") or [])),
            "skills": roles.skill_summaries(root, name),
            "models": list(role["models"]["candidates"]),
        }
    return out


# --- a clean place for the lead to read -----------------------------------------


@contextmanager
def workspace(
    repo: Path, target: str, name: str, ref: str, runner: Runner = subprocess.run
) -> Iterator[Path]:
    """A detached worktree of ``ref``, removed afterwards; the lead reads only here."""
    path = state_dir() / "worktrees" / target / "_lead" / name
    if path.exists():
        grade._git(repo, "worktree", "remove", "--force", str(path), runner=runner)
    grade._git(repo, "worktree", "prune", runner=runner)
    path.parent.mkdir(parents=True, exist_ok=True)
    grade._must(
        grade._git(repo, "worktree", "add", "--detach", str(path), ref, runner=runner),
        "git worktree add failed",
    )
    try:
        yield path
    finally:
        grade._git(repo, "worktree", "remove", "--force", str(path), runner=runner)


# --- validation -------------------------------------------------------------------


def _rel_ok(path: str) -> bool:
    p = PurePosixPath(path)
    return bool(path) and not p.is_absolute() and ".." not in p.parts


def _overlaps(a: str, b: str) -> bool:
    a, b = a.rstrip("/"), b.rstrip("/")
    return a == b or a.startswith(b + "/") or b.startswith(a + "/")


def _line_count(path: Path) -> int:
    with path.open("rb") as f:
        return sum(1 for _ in f)


def check_refs(refs: list[dict], tree: Path, where: str) -> tuple[list[dict], list[str]]:
    """Context refs clipped to real lines; errors for paths or ranges that do not exist."""
    out, errors = [], []
    for ref in refs:
        path = str(ref.get("path") or "")
        if not _rel_ok(path):
            errors.append(f"{where}: context path {path!r} must be relative inside the repo")
            continue
        f = tree / path
        if not f.is_file():
            errors.append(f"{where}: context path {path} does not exist")
            continue
        n = _line_count(f)
        start, end = int(ref.get("start") or 1), int(ref.get("end") or 0)
        if start < 1 or start > max(n, 1) or end < start:
            errors.append(f"{where}: {path}:{start}-{end} is not a range in a {n}-line file")
            continue
        end = min(end, n, start + MAX_REF_LINES - 1)
        out.append({"path": path, "start": start, "end": end, "why": str(ref.get("why") or "")})
    return out, errors


def validate_plan(
    data: Any, tree: Path, roles_menu: dict[str, dict], max_parts: int, cat: dict
) -> tuple[list[dict], list[str]]:
    """The plan's parts, cleaned, in dependency order; and every reason to refuse it."""
    if not isinstance(data, dict) or not isinstance(data.get("parts"), list):
        return [], ["the answer must be an object with a parts list"]
    parts, errors = data["parts"], []
    if not parts:
        return [], ["the plan has no parts"]
    if len(parts) > max_parts:
        errors.append(f"{len(parts)} parts; at most {max_parts}")
    keys = [str(p.get("key") or "") for p in parts]
    if len(set(keys)) != len(keys) or "" in keys:
        errors.append("every part needs a unique, non-empty key")
    clean: list[dict] = []
    for p in parts:
        key = str(p.get("key") or "?")
        where = f"part {key}"
        role = str(p.get("role") or "")
        m = roles_menu.get(role)
        if m is None:
            errors.append(f"{where}: role {role!r} is not one of {sorted(roles_menu)}")
            continue
        steps = [str(s).strip() for s in p.get("steps") or [] if str(s).strip()]
        if not steps:
            errors.append(f"{where}: no steps")
        owned = [str(o).strip() for o in p.get("owned_paths") or [] if str(o).strip()]
        if not owned:
            errors.append(f"{where}: owns no files")
        errors += [f"{where}: owned path {o!r} must be relative inside the repo" for o in owned if not _rel_ok(o)]
        toolsets = list(dict.fromkeys(str(t) for t in p.get("toolsets") or []))
        bad = [t for t in toolsets if t not in m["toolsets"]]
        if bad or not toolsets:
            errors.append(f"{where}: toolsets {bad or '[]'} not allowed; choose from {m['toolsets']}")
        skills = list(dict.fromkeys(str(s) for s in p.get("skills") or []))
        bad = [s for s in skills if s not in m["skills"]]
        if bad:
            errors.append(f"{where}: skills {bad} do not exist; choose from {sorted(m['skills'])}")
        model = str(p.get("model") or "")
        if model not in m["models"]:
            errors.append(f"{where}: model {model!r} is not one of {m['models']}")
        else:
            try:
                catalog.resolve(model, cat)
            except catalog.GateError as e:
                errors.append(f"{where}: model {model!r} refused by the catalog: {e}")
        refs, ref_errors = check_refs(list(p.get("context") or []), tree, where)
        errors += ref_errors
        deps = [str(d) for d in p.get("depends_on") or []]
        errors += [f"{where}: depends on unknown part {d!r}" for d in deps if d not in keys]
        clean.append(
            {
                "key": key,
                "title": str(p.get("title") or key).strip(),
                "role": role,
                "depends_on": deps,
                "owned_paths": owned,
                "steps": steps,
                "context": refs,
                "interfaces": [str(s) for s in p.get("interfaces") or []],
                "pitfalls": [str(s) for s in p.get("pitfalls") or []],
                "toolsets": toolsets,
                "skills": skills,
                "model": model,
            }
        )
    if errors:
        return [], errors
    ordered = _topo(clean)
    if ordered is None:
        return [], ["depends_on has a cycle"]
    errors += _parallel_overlaps(ordered)
    return ([], errors) if errors else (ordered, [])


def _topo(parts: list[dict]) -> list[dict] | None:
    by_key, done, out = {p["key"]: p for p in parts}, set(), []
    while len(out) < len(parts):
        ready = [p for p in parts if p["key"] not in done and set(p["depends_on"]) <= done]
        if not ready:
            return None
        for p in ready:
            done.add(p["key"])
            out.append(by_key[p["key"]])
    return out


def _parallel_overlaps(parts: list[dict]) -> list[str]:
    """Two parts that can run at the same time must not own the same file."""
    after: dict[str, set[str]] = {}
    for p in parts:  # dependency order, so every dependency is already filled in
        after[p["key"]] = set(p["depends_on"]).union(*(after[d] for d in p["depends_on"]))
    errors = []
    for i, a in enumerate(parts):
        for b in parts[i + 1 :]:
            if a["key"] in after[b["key"]] or b["key"] in after[a["key"]]:
                continue
            shared = [x for x in a["owned_paths"] for y in b["owned_paths"] if _overlaps(x, y)]
            if shared:
                errors.append(
                    f"parts {a['key']} and {b['key']} can run in parallel but both own {sorted(set(shared))}"
                )
    return errors


def validate_diagnosis(data: Any, tree: Path) -> tuple[dict | None, list[str]]:
    if not isinstance(data, dict):
        return None, ["the answer must be an object"]
    action = data.get("action")
    if action not in ("retry", "escalate", "ask"):
        return None, [f"action {action!r} must be retry, escalate or ask"]
    refs, errors = check_refs(list(data.get("context") or []), tree, "diagnosis")
    if action == "ask" and not str(data.get("question") or "").strip():
        errors.append("action ask needs a question for Eli")
    if errors:
        return None, errors
    return {
        "cause": str(data.get("cause") or "").strip(),
        "note": str(data.get("note") or "").strip(),
        "action": action,
        "steps": [str(s) for s in data.get("steps") or [] if str(s).strip()],
        "context": refs,
        "question": str(data.get("question") or "").strip(),
    }, []


# --- rendering an order for a free worker -------------------------------------------


def excerpts(refs: list[dict], tree: Path, budget: int) -> tuple[str, int]:
    """The referenced lines, read from ``tree``, up to ``budget`` bytes; (text, refs left out)."""
    blocks, used, skipped = [], 0, 0
    for ref in refs:
        f = tree / ref["path"]
        try:
            lines = f.read_text(errors="replace").splitlines()
        except OSError:
            skipped += 1
            continue
        body = "\n".join(lines[ref["start"] - 1 : ref["end"]])
        block = (
            f"### {ref['path']}:{ref['start']}-{ref['end']}"
            + (f" ({ref['why']})" if ref["why"] else "")
            + f"\n```\n{body}\n```"
        )
        if used + len(block.encode()) > budget:
            skipped += 1
            continue
        blocks.append(block)
        used += len(block.encode())
    return "\n\n".join(blocks), skipped


def _bullets(items: list[str], numbered: bool = False) -> str:
    return "\n".join(f"{i}. {s}" if numbered else f"- {s}" for i, s in enumerate(items, 1))


def render(
    order: dict,
    goal: dict,
    tree: Path,
    budget: int,
    *,
    failure: str | None = None,
    attempt: int = 1,
    claude: bool = False,
) -> str:
    """The task body: the order, the code it needs, and what done means."""
    out = [f"{order.get('goal') or goal['goal']}", f"## Your part: {order['title']}"]
    out.append("## Steps\n" + _bullets(order["steps"], numbered=True))
    out.append(
        "## Files you own (change nothing else)\n" + _bullets(order["owned_paths"])
    )
    if order.get("interfaces"):
        out.append("## Interfaces to use or provide\n" + _bullets(order["interfaces"]))
    if order.get("pitfalls"):
        out.append("## Watch out for\n" + _bullets(order["pitfalls"]))
    text, skipped = excerpts(order.get("context") or [], tree, budget)
    if text:
        note = (
            "## Code you need\nCopied from the worktree when this task was created. "
            "This is what you need; do not search the repo for more."
        )
        if skipped:
            note += f" ({skipped} more references did not fit; read them if a step needs them.)"
        out.append(note + "\n\n" + text)
    done = (
        f"## Done means\nWorkspace: {goal['worktree']} (branch {goal['branch']}). "
        f"Run commands there. The check `{goal['check_cmd']}` passes there, and your "
        "work is committed on the branch: uncommitted work is lost and does not count."
    )
    if not claude:
        done += (
            " Then call `kanban_request_review` with what you did and the check's last "
            "line. Never call `kanban_complete`: Team grades."
        )
    out.append(done)
    if order.get("lead_note"):
        out.append("## From the lead, after the last attempt\n" + order["lead_note"])
    if failure:
        out.append(f"## Attempt {attempt - 1} failed. What it left\n{_tail(failure)}")
    return "\n\n".join(out)


def _tail(text: str | None, limit: int = FAILURE_CHARS) -> str:
    text = text or ""
    return text if len(text) <= limit else "…" + text[-limit:]


# --- prompts --------------------------------------------------------------------------


def plan_prompt(
    goal: str,
    check: str,
    roles_menu: dict[str, dict],
    max_parts: int,
    *,
    notes: str = "",
    refused: list[str] | None = None,
    previous: Any = None,
    plan_context: dict | None = None,
) -> str:
    lines = [f"Goal from Eli:\n{goal}"]
    if notes:
        lines.append(f"What the boss knows that the repo does not say:\n{notes}")
    before = (plan_context or {}).get("before") or []
    alongside = (plan_context or {}).get("alongside") or []
    if before:
        lines += [
            "Earlier goals in the same plan, already planned. They land before your workers "
            "start, so everything below will exist in the code even though this checkout does "
            "not have it yet. Do not plan any of it again: build on it, and use the interfaces "
            "it names.",
            json.dumps(before, indent=1)[:12000],
        ]
    if alongside:
        lines += [
            "Goals in the same plan that run at the same time as this one. Their workers own "
            "these files; do not give any of them to your parts:",
            json.dumps(alongside, indent=1)[:6000],
        ]
    lines += [
        f"The check that decides done for every part: `{check}`.",
        f"You are in a clean read-only checkout of the target at the goal's base. "
        f"Plan the goal as at most {max_parts} parts and write each part's work order.",
        "Workers you can assign, and what each may be given:",
        json.dumps(roles_menu, indent=2),
    ]
    if refused:
        lines += [
            "Team refused your previous plan. Fix exactly these and answer again:",
            _bullets(refused),
            "Your previous plan:",
            json.dumps(previous, indent=2)[:20000],
        ]
    return "\n\n".join(lines)


def diagnose_prompt(
    order: dict,
    failure: str,
    diff: str,
    attempt: int,
    *,
    refused: list[str] | None = None,
) -> str:
    lines = [
        f"Attempt {attempt} at this work order failed Team's check.",
        "The order:",
        json.dumps({k: v for k, v in order.items() if k != "lead_note"}, indent=2),
        f"What the check printed (tail):\n{_tail(failure)}",
        f"The worker's diff against the base (may be cut):\n{diff[:DIFF_CHARS]}",
        "You are in a read-only checkout of the worker's branch. Find the cause. "
        "Answer retry (a free model tries again with your note, and your steps and "
        "context if you give them), escalate (Claude takes the part over), or ask "
        "(only Eli can decide; give the question).",
    ]
    if refused:
        lines += ["Team refused your previous answer:", _bullets(refused)]
    return "\n\n".join(lines)


# --- calling the lead -------------------------------------------------------------------


def call(
    root: Path,
    cwd: Path,
    prompt: str,
    schema: dict,
    cfg: dict,
    runner: Runner = subprocess.run,
) -> dict:
    """One gateway-backed Hermes run as the lead; raises LeadError when it gives no structured answer."""
    result = hermes_run.run(
        cwd,
        prompt,
        max_turns=cfg["lead.max_turns"],
        timeout_s=cfg["lead.timeout_s"],
        runner=runner,
        role=ROLE,
        root=root,
        json_schema=schema,
    )
    if not result["ok"]:
        raise LeadError(
            f"lead call failed: {_tail(str(result['result']), 500)}",
            usage_limited=claude_run.usage_limited(result),
            result=result,
        )
    if result.get("structured") is None:
        try:
            result["structured"] = json.loads(result["result"])
        except (TypeError, ValueError):
            raise LeadError("lead gave no structured answer", result=result) from None
    return result


def ask(
    root: Path,
    cwd: Path,
    make_prompt: Callable[[list[str] | None, Any], str],
    schema: dict,
    check: Callable[[Any], tuple[Any, list[str]]],
    cfg: dict,
    runner: Runner = subprocess.run,
    on_call: Callable[[dict], None] | None = None,
) -> Any:
    """Ask, validate, and ask once more with the refusals; the validated answer or LeadError."""
    refused, previous = None, None
    for _ in range(2):
        try:
            result = call(
                root, cwd, make_prompt(refused, previous), schema, cfg, runner
            )
        except LeadError as exc:
            if on_call and exc.result is not None:
                on_call(exc.result)
            raise
        if on_call:
            on_call(result)
        value, refused = check(result["structured"])
        if not refused:
            return value
        previous = result["structured"]
    raise LeadError("Team refused the lead's answer twice: " + "; ".join(refused))
