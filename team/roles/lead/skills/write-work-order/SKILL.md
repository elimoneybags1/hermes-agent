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
