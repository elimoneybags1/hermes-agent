# Dashboard contract: `team dash --json` → grid

Team owns the data; grid (`~/work/deck/grid`) shows it. This file is the
contract between them and the brief for the grid side, which is built in a
`deck` session.

## Source

`team dash --json` (on PATH via `uv tool install -e .` in this repo) prints one
JSON snapshot to stdout and exits 0. It reads only the roster (`roles/*/role.toml`
here) and the ledger (`$TEAM_STATE/team.db`, default `$HOME/state/team/team.db`).
It never creates the ledger. It is cheap enough to run every few seconds.

## Shape

```
{
  "generated_at": "2026-10-20T01:12:41+00:00",     UTC ISO seconds
  "base":  {"jobs": 4, "passed": 4, "pass_rate": 1.0},   finished jobs, every role
  "roles": [
    {
      "name": "implement",
      "title": "Implement",
      "does": "Writes the code for one job ...",
      "graded_by": "The target's test command passes in the worktree",
      "status": "active",                        active | planned
      "runs_on":    {"harness": "hermes", "model": "nvidia/nemotron-3-ultra-550b-a55b:free"},
      "escalation": {"harness": "crew", "model": "claude", "after_failures": 2} | null,
      "budget":     {"max_turns": 40, "run_budget_s": 900} | null,
      "working_on": [{"job_id": 4, "target": "team", "attempt": 1, "started": "..."}],
      "stats":      {"jobs": 1, "passed": 1, "pass_rate": 1.0}
    }
  ],
  "recent": [   last 20 ledger jobs, newest first
    {"id": 4, "target": "team", "target_commit": "5c05bed", "role": "implement",
     "role_version": "5c05bed", "attempt": 1, "started": "...", "ended": "..." | null,
     "outcome": "pass" | "fail" | "error" | "timeout" | null,
     "grader": "pytest+boss-review", "grader_detail": "..."}
  ]
}
```

`pass_rate` is `null` when there are no finished jobs. A job with `ended: null`
is still running. `recent` can hold roles that are not in the roster (the first
jobs were `builder`, Claude via crew); they count in `base`.

## What the grid panel shows

- One card per role: title, `does`, `runs_on.model`, `graded_by`, status.
  Planned roles are visibly dimmer than active ones.
- On each card, what it is working on now (`working_on`): target, job id,
  attempt, time since `started`.
- Each role's pass rate **next to the base rate**, always with counts
  (`1/1 · base 4/4`), never a bare percentage.
- A feed of `recent`: role, target, outcome, duration, grader_detail.
- `generated_at` visible, and a clear stale state if the command fails or the
  snapshot is old.

The panel only reads. It never writes the ledger or the roster.
