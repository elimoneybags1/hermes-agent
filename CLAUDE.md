# team — an agent team you point at a repo

Started 2026-10-18, out of a conversation held in the `brain` project. The full
reasoning behind it is in `docs/ORIGIN.md`; read that first in a new session.

## What this is

A repo-agnostic agent system: a **foreman** that is mostly code, **workers** with
defined roles, tools and skills, and a **feedback loop** that tunes them. You
point it at a target repo; it learns that repo (layout, tests, conventions,
what breaks) and specializes its workers to it over time.

**Current stage: base architecture only.** Get a footing that works on one
target end to end before adding roles, targets or autonomy.

## Design principles (decided)

- **The foreman is code, not a free-roaming agent.** A typed state machine with a
  fixed action set. A model is called only for narrow decisions: classify work
  into a role, diagnose a failed run into one proposed change. Precision comes
  from having few decisions, not from a smarter model.
- **Workers are defined, not improvised.** Each role has a manifest (task class,
  allowed tools, budget, how it is graded) and a `SKILL.md` (the part the foreman
  may edit). No role exists until it has a grading check.
- **Self-improvement is propose → evaluate → adopt.** A change is only kept if it
  beats the current version's pass rate on a fixed replay set — reported next to
  that base rate. Changes land as git commits so they can be rolled back.
- **Who may change what.** Free models may propose skill edits and routing changes.
  Code changes go through Claude via `crew` and wait for `crew accept`.
- **Unattended work runs on free models, or on Claude Code through Eli's
  subscription** (Eli, 2026-10-24). Claude Code is launched only by Team, with
  the Anthropic API-key variables removed so it can never bill the API. Never a
  paid API key, never the default Hermes profile. The free side is an
  allowlist that fails closed, as in `brain`. The proven implementation is
  `~/work/brain/bigbrain/braincore/llm.py` (`FREE_POLICY`, `_assert_free`, `ranked`,
  cooldowns, usage ledger). Do not use LiteLLM as-is — its fallback chains will
  happily route to a paid provider.
- **State lives outside every repo**, at `~/state/team/`, as in `brain`.
- **Hermes is the runtime and clock**, not the brain of the system: it schedules
  the foreman and runs workers (`hermes chat -s <skills> --query-file <brief>`).

## Decisions (Eli, 2026-10-19)

1. **Team manages every project from outside it.** One target per project; its
   profile, skill overlays and outcomes live in `~/state/team/targets/<name>/`.
   Attaching Team adds nothing to the target repo.
2. **Router: copy** `brain`'s `llm.py` into this repo. Extract a shared package
   only once both copies are actually in use.
3. **First target is Team itself.** Team is built by its own agents from the
   first job. Every change to this repo is a job that the ledger records.
4. **Sandbox: crew worktrees only.** Docker via colima waits until a worker runs code.
5. **Team works autonomously, on Hermes kanban** (2026-10-24). Each target gets
   goals and recurring routines; Team splits, runs, grades, lands and escalates
   until they are done, and brings Eli only real decisions. Kanban is the queue
   and runner in Team's own kanban home; Team keeps grading, the ledger,
   escalation and the model rule. Design: `docs/KANBAN.md`.

Still open: overlap with `~/work/platform/ai` (gateway, router, agents, skills).
Nobody has read it yet; it needs a session in `platform`.

## How Team is built

- **The boss is the agent Eli talks to** (Eli, 2026-10-19): a Claude Code agent,
  `plugin/agents/boss.md`, started by plain `team`. Its role, tools and rules
  live in git so they can be tuned. Roster in `roles/*/role.toml`; what
  each agent is doing shows in `team dash`, and in grid (`docs/DASH.md`).
- **Free models do almost all the work** (Eli, 2026-10-19). Workers are Hermes
  on free models, each pinned with `-m`, working in a worktree under
  `~/state/team/worktrees/`. The boss plans, grades and lands.
- **Claude via `crew` is the escalation path** after the free attempts a role
  allows. Attended and gated by review, so the free-only rule doesn't apply.
- **Use what exists before building:** `docs/TOOLS.md` (Hermes features,
  GEPA, Inspect AI, Phoenix and others, plus the paid-model risks in Hermes).
- Jobs that ran before the ledger existed are backfilled into it.
- **The session runs each wall from start to finish without Eli:** launch, watch,
  `crew check`, `crew review`, run the tests, merge, `crew down`, ledger row.
  `crew accept` reads from a terminal, so drive it with
  `printf 'm\n' | script -q /dev/null crew accept --name <wall>`. Bring Eli only
  real decisions. Never push to a remote without asking.
- The Team window is 120 columns, too narrow for two crew panes (60-column
  minimum). Give each agent its own wall: `crew up --name <slug> ...`.

## Outcomes ledger (agreed 2026-10-19)

SQLite at `~/state/team/team.db`.
- `jobs`: id, target, target_commit, role, role_version (git sha of that role's
  files), attempt, started, ended, outcome (`pass|fail|error|timeout`), grader,
  grader_detail
- `calls`: job_id, provider, model, task_class, tokens_in, tokens_out,
  latency_ms, ok, error, ts (when the call was made, UTC ISO seconds)

A job's outcome comes from its grading check, never from a model. No cost
column: the free gate refuses any paid model.

## Conventions

Inherited from `~/.claude/CLAUDE.md`: real numbers only, verify running state
from logs/processes not markdown, no empty scaffolding, portable paths
(`$HOME`, never a hardcoded user dir), no `cd` inside Bash commands.

## Terminal

`work team` opens the herdr session. Layout in `.work.json`.
