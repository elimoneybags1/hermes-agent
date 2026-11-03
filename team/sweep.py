"""One-sweep attempts: Team assembles everything a builder needs, one tool-free call
answers with the edits, Team applies them, commits, and grades.

A turn-based worker re-sends its whole context on every tool call. In the ledger on
2026-11-02 a passing implement attempt averaged 25 calls at 27K input tokens each, about
four reading or running calls for every editing one, for a few hundred output tokens a
call. Here the reading is Team's: the packet carries the order, the owned files in full
and the code the order cites, so the model answers in one call with whole files or exact
find/replace edits. Code applies the answer and runs the check. When the check fails,
the next call sees the files as the answer left them and what the check printed, up to
``sweep.calls_per_attempt`` calls; after that the attempt has failed like any other and
the lead, the next candidate model and Claude follow as before. A part that must run or
explore things to know what to write is ordered in ``turns`` mode by the lead and takes
the kanban worker instead (``team.run.Loop._sweep_wanted``).

This module is the packet, the answer contract and the apply step. ``team.run`` owns the
job, the calls, the check and the retries.
"""

from __future__ import annotations

import json
from pathlib import Path, PurePosixPath

from team import grade, lead, stages
from team.review import backend

MODES = ("sweep", "turns")
TASK_REF = "sweep:"
# Packet bytes spent on owned files before the order's cited excerpts get the rest.
OWNED_SHARE = 0.8
SKIP_DIRS = lead.SKIP_DIRS

ANSWER_RULES = """\
## How you work here
You have no tools and no terminal, and no one reads this conversation. Everything you
need is in the message: the order, the files you own as they are now, and the code the
order cites. Any instruction above about reading files, running commands, committing or
calling kanban tools does not apply: Team reads, applies, commits and runs the check.

Answer with one JSON object and nothing else (no prose, no code fence):
{"summary": "<one line: what you changed>",
 "files": {"<path>": "<the entire new content of that file>"},
 "edits": [{"path": "<path>", "find": "<exact text that occurs once in the file now>", "replace": "<text>"}],
 "delete": ["<path>"]}
- Use "files" for a new file or a file you rewrite whole; "edits" for small changes to a
  file shown to you. A "find" must match exactly once, whitespace included.
- Every path must be one of the files you own. Do not touch anything else.
- If the part cannot be done inside the files you own, answer {"cannot": "<why>"} instead.
- Finish the whole part in this answer. Team runs the check afterwards and, if it fails,
  shows you the files as you left them and the check's output for one more answer."""


class SweepError(ValueError):
    """An answer Team cannot apply: not the contract, outside the owned files, or an ambiguous edit."""


# --- the system prompt ------------------------------------------------------------------


def instructions(role: dict) -> str:
    """The role's SOUL and skills (what a Hermes worker of this role reads), then the answer rules."""
    parts = [role.get("soul") or ""]
    for skill in role.get("skills") or []:
        d = (role.get("skill_dirs") or {}).get(skill)
        if d is None:
            continue
        try:
            parts.append(Path(d).joinpath("SKILL.md").read_text())
        except OSError:
            continue
    parts.append(ANSWER_RULES)
    return "\n\n".join(p.strip() for p in parts if p and p.strip())


def role_from_profile(name: str, pdir: Path) -> dict:
    """A role dict for ``instructions`` from a synced profile (a replay's branch arm)."""
    soul = pdir / "SOUL.md"
    skills = sorted(p.name for p in (pdir / "skills").iterdir() if (p / "SKILL.md").exists()) if (pdir / "skills").is_dir() else []
    return {
        "name": name,
        "soul": soul.read_text() if soul.exists() else "",
        "skills": skills,
        "skill_dirs": {s: pdir / "skills" / s for s in skills},
    }


# --- the packet -------------------------------------------------------------------------


def _rel_ok(path: str) -> bool:
    p = PurePosixPath(path)
    return bool(path) and not p.is_absolute() and ".." not in p.parts


def owned_paths(tree: Path, owned: list[str]) -> list[str]:
    """The files the owned paths name: a file as is, a directory as every file under it."""
    out: list[str] = []
    for o in owned:
        o = o.rstrip("/")
        if not _rel_ok(o):
            continue
        p = tree / o
        if p.is_dir():
            for f in sorted(p.rglob("*")):
                if f.is_file() and not SKIP_DIRS & set(f.relative_to(tree).parts):
                    out.append(f.relative_to(tree).as_posix())
        else:
            out.append(o)  # a file, or one that does not exist yet
    return list(dict.fromkeys(out))


def owned_section(tree: Path, owned: list[str], budget: int) -> tuple[str, list[str]]:
    """The owned files in full, within ``budget`` bytes; (text, paths that did not fit)."""
    blocks, used, skipped = [], 0, []
    for rel in owned_paths(tree, owned):
        f = tree / rel
        if not f.exists():
            block = f"### {rel}\n(does not exist yet; create it with \"files\")"
        else:
            try:
                body = f.read_text(errors="replace")
            except OSError:
                skipped.append(rel)
                continue
            shown = body if body.endswith("\n") else body + "\n"
            block = f"### {rel} ({len(body.splitlines())} lines)\n```\n{shown}```"
        size = len(block.encode())
        if used + size > budget:
            skipped.append(rel)
            continue
        blocks.append(block)
        used += size
    return "\n\n".join(blocks), skipped


def packet(
    order: dict | None,
    goal: dict,
    tree: Path,
    owned: list[str],
    budget: int,
    *,
    attempt: int = 1,
    call: int = 1,
    failure: str | None = None,
    call_failure: str | None = None,
    answer: str | None = None,
) -> str:
    """The user message: the order, the owned files as they are, the cited code, what failed.

    ``failure`` is what the previous *attempt* left (shown on the first call of an attempt);
    ``call_failure`` is what this attempt's previous call produced after Team applied it.
    """
    o = order or {}
    out = [o.get("goal") or goal["goal"]]
    if o.get("title"):
        out.append(f"## Your part: {o['title']}")
    if o.get("steps"):
        out.append("## Steps\n" + lead._bullets(o["steps"], numbered=True))
    out.append("## Files you own (change nothing else)\n" + lead._bullets(owned))
    if o.get("interfaces"):
        out.append("## Interfaces to use or provide\n" + lead._bullets(o["interfaces"]))
    if o.get("pitfalls"):
        out.append("## Watch out for\n" + lead._bullets(o["pitfalls"]))
    done = f"## Done means\nTeam runs `{goal['check_cmd']}` in the worktree after applying your answer; it must pass."
    if o.get("acceptance_cmd") or goal.get("acceptance_cmd"):
        done += f" Team also runs `{o.get('acceptance_cmd') or goal.get('acceptance_cmd')}`: it fails now and must pass when you are done."
    if o.get("done_when"):
        done += "\n\nWhat the reviewer checks:\n" + lead._bullets(o["done_when"])
    out.append(done)
    if o.get("lead_note"):
        out.append("## From the lead, after the last attempt\n" + o["lead_note"])
    if answer:
        out.append("## Eli answered the question this goal stopped on\n" + answer)
    owned_text, skipped = owned_section(tree, owned, int(budget * OWNED_SHARE))
    note = "## The files you own, as they are in the worktree now"
    if skipped:
        note += f"\n({len(skipped)} owned files were too large for this packet: {', '.join(skipped)}. Use \"edits\" with text you are sure of, or leave them.)"
    out.append(note + ("\n\n" + owned_text if owned_text else ""))
    shown = set(owned_paths(tree, owned))
    refs = [r for r in (o.get("context") or []) if r.get("path") not in shown]
    left = max(0, budget - len("\n\n".join(out).encode()))
    text, more = lead.excerpts(refs, tree, left)
    if text:
        extra = f" ({more} more references did not fit.)" if more else ""
        out.append("## Code you need (not yours to change)" + extra + "\n\n" + text)
    if call > 1 and call_failure:
        out.append(
            f"## Call {call - 1} of this attempt: Team applied your answer and the check failed\n"
            "The files above are as your answer left them. Fix them.\n\n" + lead._tail(call_failure)
        )
    elif failure:
        out.append(f"## Attempt {attempt - 1} failed. What it left\n{lead._tail(failure)}")
    return "\n\n".join(out)


# --- the answer -------------------------------------------------------------------------


def plan_changes(text: str | None, owned: list[str], tree: Path) -> tuple[dict[str, str | None] | None, list[str], str | None]:
    """Turn an answer into ``{path: new content or None to delete}``.

    Returns ``(changes, refusals, cannot)``: ``changes`` when the answer is applicable,
    ``refusals`` (and no changes) when it is not, ``cannot`` when the model said the part
    cannot be done in its owned files.
    """
    data = stages.structured(text)
    if data is None:
        return None, ["the answer is not a JSON object"], None
    cannot = data.get("cannot")
    if cannot:
        return None, [], str(cannot).strip()
    errors: list[str] = []
    current: dict[str, str | None] = {}

    def read(path: str) -> str | None:
        if path in current:
            return current[path]
        f = tree / path
        return f.read_text(errors="replace") if f.is_file() else None

    def ok_path(path: object, what: str) -> str | None:
        p = str(path or "").strip()
        if not _rel_ok(p):
            errors.append(f"{what}: path {p!r} must be relative inside the repo")
            return None
        if grade.outside_owned([p], owned):
            errors.append(f"{what}: {p} is not a file you own ({', '.join(owned)})")
            return None
        return p

    files = data.get("files") or {}
    if not isinstance(files, dict):
        errors.append("files must be an object of path to content")
        files = {}
    for path, content in files.items():
        p = ok_path(path, "files")
        if p is None:
            continue
        if not isinstance(content, str):
            errors.append(f"files: {p} content must be a string")
            continue
        current[p] = content
    edits = data.get("edits") or []
    if not isinstance(edits, list):
        errors.append("edits must be a list")
        edits = []
    for n, e in enumerate(edits, 1):
        if not isinstance(e, dict):
            errors.append(f"edit {n} is not an object")
            continue
        p = ok_path(e.get("path"), f"edit {n}")
        if p is None:
            continue
        find, replace = e.get("find"), e.get("replace")
        if not isinstance(find, str) or not find or not isinstance(replace, str):
            errors.append(f"edit {n}: find must be non-empty text and replace a string")
            continue
        body = read(p)
        if body is None:
            errors.append(f"edit {n}: {p} does not exist; create it with files")
            continue
        hits = body.count(find)
        if hits != 1:
            errors.append(f"edit {n}: find occurs {hits} times in {p}, not once")
            continue
        current[p] = body.replace(find, replace, 1)
    deletes = data.get("delete") or []
    if not isinstance(deletes, list):
        errors.append("delete must be a list of paths")
        deletes = []
    for path in deletes:
        p = ok_path(path, "delete")
        if p is None:
            continue
        if not (tree / p).exists():
            errors.append(f"delete: {p} does not exist")
            continue
        current[p] = None
    if errors:
        return None, errors, None
    changes = {p: c for p, c in current.items() if c is None or read_original(tree, p) != c}
    if not changes:
        return None, ["the answer changes nothing"], None
    return changes, [], None


def read_original(tree: Path, path: str) -> str | None:
    f = tree / path
    return f.read_text(errors="replace") if f.is_file() else None


def apply(tree: Path, changes: dict[str, str | None]) -> list[str]:
    """Write the changes into the worktree; returns the paths touched."""
    touched = []
    for path, content in changes.items():
        f = tree / path
        if content is None:
            f.unlink(missing_ok=True)
        else:
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(content)
        touched.append(path)
    return touched


def summary_of(text: str | None) -> str:
    data = stages.structured(text) or {}
    return str(data.get("summary") or "").strip().splitlines()[0][:72] if data.get("summary") else "sweep answer"


# --- the call ---------------------------------------------------------------------------


def complete(spec: str, system: str, prompt: str, *, max_tokens: int, session: str) -> dict:
    """One tool-free completion on a free spec through the gateway: {text, usage, model}."""
    return backend.complete(spec, prompt, system=system, max_tokens=max_tokens, session=session)


def as_json(changes: dict[str, str | None]) -> str:
    return json.dumps(sorted(changes))
