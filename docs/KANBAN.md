# Team on Hermes kanban: autonomous work per project

Decided by Eli, 2026-10-24: Team works on its own. For each attached project it
takes goals ("build X", "keep the tests green", "run Y every night"), breaks
them into jobs, runs them on free models, uses the Claude Code subscription
when a job needs it, grades by code, lands what passes, and keeps going until
the goal is met. Eli steps in only for real decisions.

Hermes kanban is the queue and the runner. Team keeps the parts kanban lacks
(see the comparison at the end).

## What runs where

| Part | Owner | How |
|---|---|---|
| Boards, tasks, dependencies, worktrees, spawning, timeouts, crash reclaim | Hermes kanban | Team's own kanban home, `HERMES_KANBAN_HOME=$HOME/state/team/kanban`; one board per target |
| Dispatch loop | Team (`team run`) | `hermes -p team-worker kanban --board <target> dispatch`, then grading, every tick. Never the Hermes gateway |
| Splitting a goal into tasks | kanban decomposer, run by Team | `hermes -p team-worker kanban --board <target> decompose <id>`: free model, only when Team calls it |
| Workers | Hermes profiles, one per role | `team-<role>`, generated from `roles/<role>/` (SOUL.md, skills, model, tools), all cloned from the locked `team-worker` |
| Claude Code | Team | Runs `claude -p` in the task's worktree with `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN` and `ANTHROPIC_BASE_URL` removed from the environment, so it bills the subscription, never the API |
| "Done" | Team's grader | A task the worker marks complete is re-checked by code: the target's test command in the worktree. Pass: merge to the target's main locally, ledger `pass`. Fail: back to the worker with the failure output, attempt + 1 |
| Escalation | Team | After a role's `escalation.after_failures` free attempts, the task's next attempt runs on Claude Code |
| Recurring work | Team routines | Per target, `routines.toml` in `$TEAM_STATE/targets/<name>/`: a goal and an interval ("run the test suite and fix what breaks", every 24h). Team creates the task when it is due |
| Outcomes | Team ledger | One `jobs` row per attempt, linked to the kanban task id and the Hermes or Claude session (model, tokens) |
| Views | `team ui` for the agents; `hermes dashboard` with `HERMES_KANBAN_HOME` set for the boards | |

## Model rule

Allowed for unattended work, and nothing else:

1. Free models (`:free` or the allowlist in `team/free.py`), through `team-*`
   Hermes profiles that hold only the OpenRouter key.
2. Claude Code on Eli's subscription, launched only by Team with the API-key
   variables removed.

Never the default Hermes profile, never a paid API key, never MoA. The guard
fails closed on anything it cannot read.

## Settings in the `team-worker` profile

`kanban.dispatch_in_gateway: false`, `kanban.auto_decompose: false`,
`kanban.review_dispatch: false` (Team grades), `kanban.max_in_progress: 3`,
`kanban.default_assignee` and `orchestrator_profile: team-worker`.

## Build order

1. Team's kanban home and the `team-worker` kanban settings; one board per target.
2. `team run`: dispatch, grade, land, escalate, ledger, one tick at a time; then a launchd agent to keep it running.
3. `team-<role>` profiles generated from `roles/`.
4. Claude Code runner and the guard rule for it.
5. Routines.
6. `team goal <target> "<goal>"`: the way Eli and the boss hand Team work.

## What kanban does not do (why Team stays)

- A task cannot carry a check that must pass: "done" is the worker's word.
- Runs do not record model, tokens, commit, role version or grader.
- No escalation to Claude.
- Default worktrees go inside the target repo and are kept until pushed;
  Team creates worktrees under `$TEAM_STATE/worktrees/` and passes
  `--workspace worktree:<path> --branch <branch>`.
- Its board shows tasks, not agents.
