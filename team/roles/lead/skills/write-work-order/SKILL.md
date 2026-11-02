---
name: write-work-order
description: How to cut a goal into parts and write orders a free model can follow.
---
1. Read the target's CLAUDE.md or AGENTS.md first; its rules go into pitfalls.
2. Find the code the goal touches with Grep, then Read only those regions.
3. One part = one set of owned files + the target's check passing. Prefer one
   part for a small goal; split only where pieces are independent or one
   needs another's interface first (then use depends_on).
4. For each part, list context ranges as `path`, `start`, `end` (1-based,
   inclusive), at most 200 lines each. Ranges are checked against the files:
   a range past the end of a file is refused.
5. Steps are imperative and concrete: "Add `def parse(text: str) -> Plan` to
   team/plan.py after `load`", not "implement parsing".
6. Give the plan a `type` (`bugfix`, `feature`, `dependencies`, `security`,
   `onboarding`): it picks the stages and whether the result lands by itself
   or waits for Eli. Anything touching dependencies, lockfiles, migrations,
   CI, auth, billing or secrets is Eli's to land whatever you choose.
7. Every part needs an `acceptance_cmd`: one command, run in the worktree,
   that fails before the part is built and passes after. Name the part's own
   new tests (`pytest tests/team/test_x.py -q`, `node --test tests/x.test.ts`);
   never the whole suite, which already passes. Team runs it after the check;
   a commit that changes nothing fails it. And `done_when`: the observable
   facts a reviewer checks, one per line, each something a diff or a command
   can show.
