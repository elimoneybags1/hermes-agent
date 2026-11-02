"""Briefs and verdicts for the pipeline stages that are not the builder's (team.gates).

The builder's brief is the lead's rendered order (``team.lead.render``) or a plain
``run.brief``. The other worker stages get theirs here, from the same order:

- test-write: write the part's tests first, only test files, and see them fail;
- review: read the diff against what the order promised and answer with a verdict;
- security: read the diff for what must never land unnoticed and report.

A reviewer's or scanner's answer is the summary it hands to ``kanban_request_review``
(or, on a Claude attempt, its final text). Code parses it: a finding has to point at a
line the diff actually touched, or the answer is refused. Nothing a model says lands by
itself: a 'land' verdict only lets the pipeline go on to the landing policy.
"""

from __future__ import annotations

import json
import re
from typing import Any

VERDICTS = ("land", "fix")
SEVERITIES = ("info", "low", "medium", "high", "critical")
MAX_DIFF_CHARS = 60000
MAX_FINDINGS = 20

_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")
_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


def _bullets(items: list[str], numbered: bool = False) -> str:
    return "\n".join((f"{n}. {s}" if numbered else f"- {s}") for n, s in enumerate(items, 1))


def clip_diff(diff: str, limit: int = MAX_DIFF_CHARS) -> str:
    if len(diff) <= limit:
        return diff
    return diff[:limit] + f"\n… diff clipped at {limit} characters; read the files for the rest."


# --- briefs ------------------------------------------------------------------------------


def _answer_how(claude: bool) -> str:
    """How the worker hands its answer back: a kanban summary, or (Claude, no board) its final text."""
    if claude:
        return "Your final message"
    return "Call `kanban_request_review` with a summary that"


def _done_text(goal: dict, claude: bool = False) -> str:
    text = (
        f"Workspace: {goal['worktree']} (branch {goal['branch']}). Run commands there. "
        "Commit on the branch: uncommitted work is lost and does not count."
    )
    if not claude:
        text += (
            " When you are done, call `kanban_request_review` with your summary. Never call "
            "`kanban_complete`: Team grades."
        )
    return text


def tests_brief(goal: dict, order: dict | None, owned: list[str] | None, *, failure: str | None = None, attempt: int = 1, claude: bool = False) -> str:
    """The test-write stage: the part's tests, written first, failing on purpose."""
    o = order or {}
    out = [o.get("goal") or goal["goal"]]
    if o.get("title"):
        out.append(f"## The part these tests are for: {o['title']}")
    if o.get("done_when"):
        out.append("## What the tests must prove, one test per fact where it fits\n" + _bullets(o["done_when"]))
    if o.get("steps"):
        out.append("## What the builder will do afterwards (test the result, not the steps)\n" + _bullets(o["steps"], numbered=True))
    if o.get("interfaces"):
        out.append("## Interfaces the tests may rely on\n" + _bullets(o["interfaces"]))
    acceptance = o.get("acceptance_cmd") or goal.get("acceptance_cmd")
    rules = [
        "Change only test files (a tests/ or __tests__/ directory, test_*.py, *.test.ts, *.spec.js and "
        "the like). Any other file changed fails this stage.",
        "The tests must fail now, because the part is not built yet, and must pass once it is. Do not "
        "stub, skip or mark them expected-failure; do not write tests that pass against the current code.",
    ]
    if acceptance:
        rules.append(
            f"Run `{acceptance}` before you finish: it must exit non-zero because of your new tests "
            "(collection errors from a missing module count as failing). If it exits zero, your tests do "
            "not test the change."
        )
    if owned:
        rules.append("Files you may create or change: " + ", ".join(p for p in owned if _looks_like_test(p)) or "the test files the order names")
    out.append("## Rules\n" + _bullets(rules))
    out.append("## Done means\n" + _done_text(goal, claude) + " Say which tests you wrote and the acceptance command's last line.")
    if failure:
        out.append(f"## Attempt {attempt - 1} failed. What it left\n{failure[-4000:]}")
    return "\n\n".join(out)


def _looks_like_test(path: str) -> bool:
    from team.gates import is_test_path

    return is_test_path(path)


def review_brief(goal: dict, order: dict | None, diff: str, verify_detail: str | None, *, failure: str | None = None, attempt: int = 1, claude: bool = False) -> str:
    """The review stage: a verdict on the diff, grounded in lines of it."""
    o = order or {}
    out = [o.get("goal") or goal["goal"]]
    if o.get("title"):
        out.append(f"## The part under review: {o['title']}")
    if o.get("done_when"):
        out.append("## What done means, as the lead wrote it (check every line)\n" + _bullets(o["done_when"]))
    if o.get("steps"):
        out.append("## What the builder was told to do\n" + _bullets(o["steps"], numbered=True))
    if verify_detail:
        out.append("## What Team's own checks found (they passed)\n" + verify_detail[-2000:])
    out.append("## The diff against the base branch\n```diff\n" + clip_diff(diff) + "\n```")
    out.append(
        "## How to answer\n"
        "You change nothing. Read the diff, and the files around it when a hunk is not enough. "
        f"{_answer_how(claude)} is only this JSON object:\n"
        '{"verdict": "land" | "fix", "checked": ["<done_when line>: how you verified it"], '
        '"findings": [{"path": "<file in the diff>", "line": <line number in the new file>, "says": "<what is wrong and what to do>"}]}\n'
        "- `land` when every done_when line is met by the diff and nothing in it is wrong. `fix` otherwise, "
        "with at least one finding.\n"
        "- A finding must point at a line the diff added or changed (the + side). A finding about a line "
        "the diff did not touch is refused.\n"
        "- Style is not a finding. Wrong behaviour, a missed done_when line, a test that does not test, "
        "a change outside the part, a secret or a dangerous default is.\n"
        "- No prose outside the JSON."
    )
    if failure:
        out.append(f"## Your previous answer was refused\n{failure[-2000:]}")
    return "\n\n".join(out)


def security_brief(goal: dict, order: dict | None, diff: str, *, failure: str | None = None, attempt: int = 1, claude: bool = False) -> str:
    o = order or {}
    out = [o.get("goal") or goal["goal"]]
    out.append("## The diff against the base branch\n```diff\n" + clip_diff(diff) + "\n```")
    out.append(
        "## How to answer\n"
        "You change nothing. Read the diff for what must never land unnoticed: secrets or keys, "
        "injection (shell, SQL, template, path), unsafe deserialisation, disabled verification or auth, "
        "new network endpoints or dependencies, permissions widened, data leaving the machine, "
        f"unsafe defaults. {_answer_how(claude)} is only this JSON:\n"
        '{"summary": "<one line>", "findings": [{"path": "<file in the diff>", "line": <line in the new file>, '
        '"severity": "info" | "low" | "medium" | "high" | "critical", "says": "<what and why>"}]}\n'
        "- A finding must point at a line the diff added or changed. An empty findings list is a fine answer "
        "when there is nothing.\n- No prose outside the JSON. Eli reads this report before anything lands."
    )
    if failure:
        out.append(f"## Your previous answer was refused\n{failure[-2000:]}")
    return "\n\n".join(out)


# --- the diff's own lines ---------------------------------------------------------------


def diff_lines(diff: str) -> dict[str, set[int]]:
    """Per new-file path, the line numbers the diff added or changed (the + side of each hunk)."""
    out: dict[str, set[int]] = {}
    path, line = None, 0
    for raw in diff.splitlines():
        if raw.startswith("+++ "):
            name = raw[4:].strip()
            path = None if name == "/dev/null" else name.removeprefix("b/")
            out.setdefault(path, set()) if path else None
            continue
        m = _HUNK.match(raw)
        if m:
            line = int(m.group(1))
            continue
        if path is None or raw.startswith("--- ") or raw.startswith("diff ") or raw.startswith("index "):
            continue
        if raw.startswith("+"):
            out[path].add(line)
            line += 1
        elif raw.startswith("-") or raw.startswith("\\"):
            continue
        else:
            line += 1
    return out


# --- parsing answers --------------------------------------------------------------------


def structured(text: str | None) -> Any:
    """The JSON object in a worker's summary, or None (whole text, then fences, then the outer braces)."""
    if not text or not isinstance(text, str):
        return None
    candidates = [text.strip(), *(m.group(1).strip() for m in _FENCE.finditer(text))]
    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        candidates.append(text[start : end + 1])
    for c in candidates:
        try:
            value = json.loads(c)
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    return None


def _check_findings(found: Any, touched: dict[str, set[int]], *, severities: bool) -> tuple[list[dict], list[str]]:
    errors: list[str] = []
    clean: list[dict] = []
    if not isinstance(found, list):
        return [], ["findings must be a list"]
    if len(found) > MAX_FINDINGS:
        errors.append(f"{len(found)} findings; at most {MAX_FINDINGS}")
    for n, f in enumerate(found[:MAX_FINDINGS], 1):
        if not isinstance(f, dict):
            errors.append(f"finding {n} is not an object")
            continue
        path = str(f.get("path") or "")
        says = str(f.get("says") or "").strip()
        line = f.get("line")
        if path not in touched:
            errors.append(f"finding {n}: {path!r} is not a file the diff touched ({', '.join(sorted(touched)) or 'none'})")
            continue
        if not isinstance(line, int) or isinstance(line, bool) or line not in touched[path]:
            errors.append(f"finding {n}: line {line!r} of {path} is not a line the diff added or changed")
            continue
        if not says:
            errors.append(f"finding {n}: says nothing")
            continue
        item = {"path": path, "line": line, "says": says}
        if severities:
            sev = str(f.get("severity") or "").lower()
            if sev not in SEVERITIES:
                errors.append(f"finding {n}: severity must be one of {SEVERITIES}")
                continue
            item["severity"] = sev
        clean.append(item)
    return clean, errors


def parse_verdict(text: str | None, diff: str) -> tuple[dict | None, list[str]]:
    """A reviewer's answer: ({verdict, checked, findings}, []) or (None, reasons it was refused)."""
    data = structured(text)
    if data is None:
        return None, ["the summary is not a JSON object"]
    verdict = str(data.get("verdict") or "").lower()
    errors: list[str] = []
    if verdict not in VERDICTS:
        errors.append(f"verdict must be one of {VERDICTS}")
    findings, ferrs = _check_findings(data.get("findings") or [], diff_lines(diff), severities=False)
    errors += ferrs
    if verdict == "fix" and not findings and not ferrs:
        errors.append("a fix verdict needs at least one finding")
    checked = [str(c).strip() for c in (data.get("checked") or []) if str(c).strip()]
    if errors:
        return None, errors
    return {"verdict": verdict, "checked": checked, "findings": findings}, []


def parse_report(text: str | None, diff: str) -> tuple[dict | None, list[str]]:
    """A security scanner's answer: ({summary, findings}, []) or (None, reasons)."""
    data = structured(text)
    if data is None:
        return None, ["the summary is not a JSON object"]
    findings, errors = _check_findings(data.get("findings") or [], diff_lines(diff), severities=True)
    summary = str(data.get("summary") or "").strip()
    if not summary:
        errors.append("summary is empty")
    if errors:
        return None, errors
    return {"summary": summary, "findings": findings}, []


def onboard_brief(goal: dict, target: dict, *, failure: str | None = None, attempt: int = 1, claude: bool = False) -> str:
    """The onboarding stage: find how this repo is checked and prove the check can fail."""
    known = {k: target.get(k) for k in ("check", "test_runner", "e2e", "setup") if target.get(k)}
    out = [goal["goal"], f"## The repo\n{target['path']} (target `{target['name']}`), checked out in your worktree."]
    if known:
        out.append("## What Team already has for it (confirm, correct or replace)\n" + "\n".join(f"- {k}: `{v}`" for k, v in known.items()))
    out.append(
        "## What to find\n"
        "- `check`: the one command that decides done: build, lint and tests as the repo's own instructions "
        "run them. It must pass in the worktree as it stands.\n"
        "- `test_runner`: how to run one test file, with `{path}` where the file goes; empty if there is no way.\n"
        "- `e2e`: a command that exercises the real thing (a browser test, a smoke run), or empty.\n"
        "- `setup`: what a fresh worktree needs before the check can run (install, generate), or empty.\n"
        "- `break_cmd`: one shell command that corrupts something the check covers, undoable by "
        "`git checkout -- .`. Team runs the check, then this, then the check again: the second run must fail.\n"
        "- `notes`: what you could not settle."
    )
    out.append(
        "## How to answer\n"
        f"{_answer_how(claude)} is only this JSON object:\n"
        '{"check": "<command>", "test_runner": "<command with {path}>" | "", "e2e": "<command>" | "", '
        '"setup": "<command>" | "", "break_cmd": "<command>", "notes": "<text>"}\n'
        "Commit any file you added (a smoke script) before you answer. No prose outside the JSON."
    )
    if not claude:
        out.append("## Done means\n" + _done_text(goal, claude))
    if failure:
        out.append(f"## Attempt {attempt - 1} was refused\n{failure[-3000:]}")
    return "\n\n".join(out)


ONBOARDING_KEYS = ("check", "test_runner", "e2e", "setup", "break_cmd", "notes")


def parse_onboarding(text: str | None) -> tuple[dict | None, list[str]]:
    """An onboarding answer: ({check, test_runner, e2e, setup, break_cmd, notes}, []) or (None, reasons)."""
    data = structured(text)
    if data is None:
        return None, ["the summary is not a JSON object"]
    out: dict[str, str] = {}
    errors: list[str] = []
    for key in ONBOARDING_KEYS:
        value = data.get(key)
        value = "" if value is None else str(value).strip()
        if "\n" in value and key != "notes":
            errors.append(f"{key} must be one line")
        out[key] = value
    if not out["check"]:
        errors.append("check is empty")
    if not out["break_cmd"]:
        errors.append("break_cmd is empty: a check that cannot be shown to fail proves nothing")
    if out["test_runner"] and "{path}" not in out["test_runner"]:
        errors.append("test_runner must contain {path} where the test file goes")
    if errors:
        return None, errors
    return out, []


def findings_text(findings: list[dict]) -> str:
    return "\n".join(
        f"- {f['path']}:{f['line']}" + (f" [{f['severity']}]" if f.get("severity") else "") + f" {f['says']}"
        for f in findings
    )
