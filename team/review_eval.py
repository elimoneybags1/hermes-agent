"""Reviewer calibration (BUILD-PLAN spike 3): does a model, as the review role, catch a bug?

Cases come from the target's own history: every ``Merge team/…`` on the base branch is a
diff that passed every gate (a *good* case, expected ``land``); a *bad* case is one of those
diffs with one bug seeded into a source line it added (a flipped comparison, ``and``/``or``
swapped, a return replaced by ``None``, ``+ 1`` made ``- 1``), expected ``fix`` with a
finding on that line. The reviewer sees exactly what it sees in the pipeline: the stage
brief with the diff, read-only tools in a worktree where the mutated file really reads that
way, no check to run.

The score is the two numbers the plan asks for: **misses** (a bad diff judged ``land``: the
reviewer would have let a bug land) and **false blocks** (a good diff judged ``fix``). A
third column, refused, counts answers that were not a grounded verdict at all. The pass
bar from the plan: no misses, under 20% false blocks. Everything is written to
``$TEAM_STATE/review-eval/<stamp>.json`` with every verdict, so the numbers can be read back.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from pathlib import Path

from team import hermes_run, ledger, stages, targets

TEST_PATH = re.compile(r"(^|/)(tests?|__tests__|spec|specs|e2e)(/|$)|(^|/)test_[^/]*\.py$|[._-](test|spec)\.[a-z]+$")
_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")
GOAL_IN_SUBJECT = re.compile(r"\(goal (\d+)\)\s*$")

# (pattern, replacement, what a reviewer should say); applied to the first added source line that matches
MUTATIONS: tuple[tuple[re.Pattern, str, str], ...] = (
    (re.compile(r"(?<![=!<>])==(?!=)"), "!=", "comparison inverted"),
    (re.compile(r"\breturn (?!None\b)(?!$)[^#\n]+"), "return None", "returns None instead of the value"),
    (re.compile(r"\band\b"), "or", "and/or swapped"),
    (re.compile(r"\+ 1\b"), "- 1", "+ 1 became - 1"),
    (re.compile(r"\bif not\b"), "if", "condition negated"),
    (re.compile(r"(?<![<>=!])>=(?!=)"), "<", ">= became <"),
    (re.compile(r"\bTrue\b"), "False", "True became False"),
)


def _git(repo: Path, *args: str) -> str:
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=False)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or f"git {' '.join(args)} failed")
    return r.stdout


def merges(repo: Path, limit: int) -> list[tuple[str, str]]:
    """(sha, subject) of the newest ``Merge team/…`` commits: diffs that landed through the gates."""
    out = []
    for line in _git(repo, "log", "--merges", "--format=%H %s", "-n", str(limit * 3)).splitlines():
        sha, _, subject = line.partition(" ")
        if subject.startswith("Merge team/"):
            out.append((sha, subject))
        if len(out) >= limit:
            break
    return out


def added_lines(diff: str) -> list[tuple[str, int, str]]:
    """(path, new-file line number, text) for every line a diff adds, in order."""
    out, path, line = [], None, 0
    for raw in diff.splitlines():
        if raw.startswith("+++ "):
            name = raw[4:].strip()
            path = None if name == "/dev/null" else name.removeprefix("b/")
            continue
        m = _HUNK.match(raw)
        if m:
            line = int(m.group(1))
            continue
        if path is None or raw.startswith(("--- ", "diff ", "index ")):
            continue
        if raw.startswith("+"):
            out.append((path, line, raw[1:]))
            line += 1
        elif raw.startswith(("-", "\\")):
            continue
        else:
            line += 1
    return out


def seed(diff: str) -> tuple[str, int, str, str, str] | None:
    """(path, line, old text, new text, what) for the first added source line a mutation fits."""
    for path, line, text in added_lines(diff):
        if TEST_PATH.search(path) or not path.endswith((".py", ".ts", ".tsx", ".js", ".go", ".rs")):
            continue
        stripped = text.strip()
        if not stripped or stripped.startswith(("#", "//", '"', "'", "import", "from")):
            continue
        for pattern, repl, what in MUTATIONS:
            if pattern.search(text):
                new = pattern.sub(repl, text, count=1)
                if new != text:
                    return path, line, text, new, what
    return None


def done_when_for(conn, subject: str) -> list[str]:
    """The lead's done_when for the goal a merge subject names, or the subject itself."""
    m = GOAL_IN_SUBJECT.search(subject)
    if m:
        order = ledger.latest_order(conn, int(m.group(1)))
        if order and order["order"].get("done_when"):
            return list(order["order"]["done_when"])
    return [subject.split(": ", 1)[-1].strip()]


def build_cases(conn, repo: Path, good: int, bad: int, scratch: Path) -> list[dict]:
    """Worktrees and diffs for ``good`` landed merges and ``bad`` seeded variants of landed merges."""
    scratch.mkdir(parents=True, exist_ok=True)
    cases: list[dict] = []
    pool = merges(repo, good + bad + 10)
    for n, (sha, subject) in enumerate(pool):
        if len([c for c in cases if c["kind"] == "good"]) >= good and len([c for c in cases if c["kind"] == "bad"]) >= bad:
            break
        diff = _git(repo, "diff", f"{sha}^1", sha)
        if not diff.strip():
            continue
        want_bad = len([c for c in cases if c["kind"] == "bad"]) < bad and (n % 3 != 0 or len([c for c in cases if c["kind"] == "good"]) >= good)
        wt = scratch / f"case-{len(cases) + 1}"
        _git(repo, "worktree", "add", "--detach", "-q", str(wt), sha)
        title = subject.split(": ", 1)[-1]
        base = {"sha": sha, "subject": subject, "title": title, "worktree": str(wt), "done_when": done_when_for(conn, subject)}
        if want_bad:
            s = seed(diff)
            if s is None:
                _git(repo, "worktree", "remove", "--force", str(wt))
                continue
            path, line, old, new, what = s
            target = wt / path
            lines = target.read_text().splitlines(keepends=True)
            if line - 1 >= len(lines) or lines[line - 1].rstrip("\n") != old:
                _git(repo, "worktree", "remove", "--force", str(wt))
                continue
            lines[line - 1] = new + ("\n" if lines[line - 1].endswith("\n") else "")
            target.write_text("".join(lines))
            cases.append({**base, "kind": "bad", "path": path, "line": line, "seeded": what, "old": old, "new": new,
                          "diff": _git(wt, "diff", f"{sha}^1")})
        else:
            cases.append({**base, "kind": "good", "diff": diff})
    return cases


def judge(case: dict, verdict: dict | None, errors: list[str]) -> str:
    """One word per case: hit / miss / fix-elsewhere / ok / false-block / refused."""
    if verdict is None:
        return "refused"
    if case["kind"] == "good":
        return "ok" if verdict["verdict"] == "land" else "false-block"
    if verdict["verdict"] == "land":
        return "miss"
    near = any(f["path"] == case["path"] and abs(int(f["line"]) - case["line"]) <= 3 for f in verdict["findings"])
    return "hit" if near else "fix-elsewhere"


def run_case(root: Path, case: dict, model: str, *, max_turns: int = 8, timeout_s: int = 420) -> dict:
    goal = {"id": 0, "goal": case["title"], "worktree": case["worktree"], "branch": "(landed)", "check_cmd": "(already passed)"}
    order = {"title": case["title"], "done_when": case["done_when"], "steps": []}
    brief = stages.review_brief(goal, order, case["diff"], "check passed; acceptance passed", claude=True)
    t0 = time.time()
    result = hermes_run.run(case["worktree"], brief, max_turns=max_turns, timeout_s=timeout_s, role="review", root=root, model=model, check=None)
    text = str(result.get("result") or "")
    verdict, errors = stages.parse_verdict(text, case["diff"]) if result.get("ok") else (None, [f"run failed: {text[:200]}"])
    return {
        "case": {k: case[k] for k in case if k != "diff"}, "model": model, "ok": bool(result.get("ok")),
        "seconds": round(time.time() - t0, 1), "usage": result.get("usage"), "verdict": verdict, "errors": errors,
        "judged": judge(case, verdict, errors), "answer": text[-1500:],
    }


def summarize(rows: list[dict]) -> dict[str, dict]:
    """Per model: the counts the plan asks for, next to the case counts they are out of."""
    out: dict[str, dict] = {}
    for r in rows:
        m = out.setdefault(r["model"], {"bad": 0, "good": 0, "hit": 0, "miss": 0, "fix-elsewhere": 0, "ok": 0, "false-block": 0, "refused": 0, "seconds": 0.0})
        m[r["case"]["kind"]] += 1
        m[r["judged"]] += 1
        m["seconds"] += r["seconds"]
    for m in out.values():
        m["passes_bar"] = m["miss"] == 0 and (m["good"] == 0 or m["false-block"] * 5 < m["good"])
    return out


def evaluate(root: Path, conn, target: str, models: list[str], good: int = 10, bad: int = 20, log=print) -> Path:
    """Build the cases once, judge each with every model, write the JSON, return its path."""
    repo = targets.repo_path(target)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out_dir = ledger.state_dir() / "review-eval"
    scratch = out_dir / f"{stamp}-worktrees"
    cases = build_cases(conn, repo, good, bad, scratch)
    log(f"{len(cases)} cases: {sum(c['kind'] == 'good' for c in cases)} good, {sum(c['kind'] == 'bad' for c in cases)} bad; models {', '.join(models)}")
    rows: list[dict] = []
    path = out_dir / f"{stamp}.json"
    try:
        for model in models:
            for i, case in enumerate(cases, 1):
                row = run_case(root, case, model)
                rows.append(row)
                log(f"{model} {i}/{len(cases)} {case['kind']:<4} -> {row['judged']} ({row['seconds']}s)" + (f": {'; '.join(row['errors'])[:120]}" if row["errors"] else ""))
                path.write_text(json.dumps({"target": target, "cases": len(cases), "rows": rows, "summary": summarize(rows)}, indent=1))
    finally:
        for case in cases:
            try:
                _git(repo, "worktree", "remove", "--force", case["worktree"])
            except RuntimeError:
                pass
    summary = summarize(rows)
    for model, m in summary.items():
        log(f"{model}: misses {m['miss']}/{m['bad']} bad, false blocks {m['false-block']}/{m['good']} good, hits {m['hit']}, fix elsewhere {m['fix-elsewhere']}, refused {m['refused']}; "
            + ("passes the bar" if m["passes_bar"] else "does not pass the bar"))
    return path
