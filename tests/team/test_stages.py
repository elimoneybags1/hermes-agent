"""Briefs and verdicts for the non-builder stages: a finding must point at a line of the diff."""

from __future__ import annotations

import json

from team import stages

DIFF = """diff --git a/app.py b/app.py
index 1..2 100644
--- a/app.py
+++ b/app.py
@@ -1 +1,2 @@
-VALUE = 1
+VALUE = 2
+# round 1
diff --git a/tests/test_app.py b/tests/test_app.py
new file mode 100644
--- /dev/null
+++ b/tests/test_app.py
@@ -0,0 +1,3 @@
+from app import VALUE
+
+assert VALUE == 2
"""

GOAL = {"id": 7, "goal": "Set VALUE to 2", "worktree": "/wt", "branch": "team/x", "check_cmd": "./check.sh"}
ORDER = {"title": "Set it", "done_when": ["VALUE is 2", "a test proves it"], "steps": ["edit app.py"], "acceptance_cmd": "pytest tests/test_app.py -q"}


def test_diff_lines_are_the_plus_side_of_each_hunk():
    assert stages.diff_lines(DIFF) == {"app.py": {1, 2}, "tests/test_app.py": {1, 2, 3}}
    assert stages.diff_lines("") == {}


def test_parse_verdict_accepts_grounded_findings_only():
    ok, errors = stages.parse_verdict(json.dumps({"verdict": "land", "checked": ["VALUE is 2: app.py:1"]}), DIFF)
    assert errors == [] and ok["verdict"] == "land" and ok["checked"] == ["VALUE is 2: app.py:1"]
    fenced = "Here you go:\n```json\n" + json.dumps({"verdict": "fix", "findings": [{"path": "app.py", "line": 2, "says": "drop the comment"}]}) + "\n```"
    ok, errors = stages.parse_verdict(fenced, DIFF)
    assert errors == [] and ok["findings"] == [{"path": "app.py", "line": 2, "says": "drop the comment"}]
    _, errors = stages.parse_verdict(json.dumps({"verdict": "fix", "findings": [{"path": "app.py", "line": 9, "says": "x"}]}), DIFF)
    assert any("not a line the diff added" in e for e in errors)
    _, errors = stages.parse_verdict(json.dumps({"verdict": "fix", "findings": [{"path": "other.py", "line": 1, "says": "x"}]}), DIFF)
    assert any("not a file the diff touched" in e for e in errors)
    _, errors = stages.parse_verdict(json.dumps({"verdict": "fix", "findings": []}), DIFF)
    assert "a fix verdict needs at least one finding" in errors
    _, errors = stages.parse_verdict("looks fine to me", DIFF)
    assert errors == ["the summary is not a JSON object"]
    _, errors = stages.parse_verdict(json.dumps({"verdict": "ship it"}), DIFF)
    assert any("verdict must be one of" in e for e in errors)


def test_parse_report_needs_a_summary_and_severities():
    ok, errors = stages.parse_report(json.dumps({"summary": "nothing dangerous", "findings": []}), DIFF)
    assert errors == [] and ok == {"summary": "nothing dangerous", "findings": []}
    ok, errors = stages.parse_report(
        json.dumps({"summary": "one", "findings": [{"path": "app.py", "line": 1, "severity": "HIGH", "says": "x"}]}), DIFF
    )
    assert errors == [] and ok["findings"][0]["severity"] == "high"
    _, errors = stages.parse_report(json.dumps({"summary": "", "findings": [{"path": "app.py", "line": 1, "severity": "scary", "says": "x"}]}), DIFF)
    assert any("severity" in e for e in errors) and "summary is empty" in errors
    assert stages.findings_text(ok["findings"]) == "- app.py:1 [high] x"


def test_briefs_carry_the_order_and_say_how_to_answer():
    t = stages.tests_brief(GOAL, ORDER, ["app.py", "tests/test_app.py"])
    assert "Change only test files" in t and "pytest tests/test_app.py -q" in t and "tests/test_app.py" in t
    assert "VALUE is 2" in t and "kanban_request_review" in t
    r = stages.review_brief(GOAL, ORDER, DIFF, "check passed; acceptance passed")
    assert "+VALUE = 2" in r and "VALUE is 2" in r and '"verdict": "land" | "fix"' in r and "kanban_request_review" in r
    rc = stages.review_brief(GOAL, ORDER, DIFF, None, claude=True)
    assert "kanban_request_review" not in rc and "Your final message" in rc
    s = stages.security_brief(GOAL, None, DIFF)
    assert "+VALUE = 2" in s and '"severity"' in s and "Eli reads this report" in s
    long = stages.clip_diff("x" * 70000, limit=100)
    assert long.startswith("x" * 100) and "clipped" in long
