"""Reviewer calibration cases: landed merges as good diffs, one seeded bug as a bad one, scored."""

from __future__ import annotations

from team import review_eval
from tests.team.test_catalog import CATALOG
from tests.team.test_run import world  # fixture

DIFF = """diff --git a/team/x.py b/team/x.py
--- a/team/x.py
+++ b/team/x.py
@@ -1,2 +1,5 @@
 import os
+# a comment
+def ok(a, b):
+    return a == b and os.name
+
diff --git a/tests/team/test_x.py b/tests/team/test_x.py
--- /dev/null
+++ b/tests/team/test_x.py
@@ -0,0 +1,2 @@
+from team.x import ok
+assert ok(1, 1)
"""


def test_added_lines_and_seed_pick_a_source_line_not_a_test_or_comment():
    added = review_eval.added_lines(DIFF)
    assert added[0] == ("team/x.py", 2, "# a comment") and added[-1] == ("tests/team/test_x.py", 2, "assert ok(1, 1)")
    path, line, old, new, what = review_eval.seed(DIFF)
    assert (path, line) == ("team/x.py", 4) and old == "    return a == b and os.name"
    assert new == "    return a != b and os.name" and what == "comparison inverted"
    assert review_eval.seed("diff --git a/t_test.py b/t_test.py\n--- a/t_test.py\n+++ b/t_test.py\n@@ -0,0 +1 @@\n+x = 1 == 1\n") is None


def test_judge_scores_the_two_numbers_the_plan_asks_for():
    bad = {"kind": "bad", "path": "team/x.py", "line": 4}
    good = {"kind": "good"}
    fix_near = {"verdict": "fix", "findings": [{"path": "team/x.py", "line": 5, "says": "inverted"}]}
    fix_far = {"verdict": "fix", "findings": [{"path": "team/x.py", "line": 40, "says": "x"}]}
    land = {"verdict": "land", "findings": []}
    assert review_eval.judge(bad, fix_near, []) == "hit"
    assert review_eval.judge(bad, fix_far, []) == "fix-elsewhere"
    assert review_eval.judge(bad, land, []) == "miss"
    assert review_eval.judge(bad, None, ["not json"]) == "refused"
    assert review_eval.judge(good, land, []) == "ok"
    assert review_eval.judge(good, fix_near, []) == "false-block"
    rows = [
        {"model": "m", "case": bad, "judged": "hit", "seconds": 1},
        {"model": "m", "case": bad, "judged": "miss", "seconds": 1},
        {"model": "m", "case": good, "judged": "ok", "seconds": 1},
    ]
    s = review_eval.summarize(rows)["m"]
    assert s["miss"] == 1 and s["bad"] == 2 and s["good"] == 1 and s["passes_bar"] is False
    rows[1]["judged"] = "hit"
    assert review_eval.summarize(rows)["m"]["passes_bar"] is True


def test_cases_come_from_landed_merges(world, tmp_path):
    from team import run as run_mod
    from tests.team.test_run import worker_commits

    goal = run_mod.start_goal(world["root"], world["conn"], "team", "Set VALUE to 2", runner=world["hermes"], cat=CATALOG)
    worker_commits(world, goal)
    world["loop"].tick()
    repo = world["repo"]
    assert len(review_eval.merges(repo, 5)) == 1
    cases = review_eval.build_cases(world["conn"], repo, good=1, bad=1, scratch=tmp_path / "wt")
    kinds = sorted(c["kind"] for c in cases)
    assert kinds in (["good"], ["bad", "good"])  # app.py's one changed line may fit no mutation
    assert all("+VALUE = 2" in c["diff"] for c in cases) and cases[0]["done_when"]
    for c in cases:
        review_eval._git(repo, "worktree", "remove", "--force", c["worktree"])
