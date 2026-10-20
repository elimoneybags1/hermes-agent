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
- **Free tiers only for anything that runs unattended.** Same rule as `brain`: an
  allowlist that fails closed. The proven implementation is
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

Still open: overlap with `~/work/platform/ai` (gateway, router, agents, skills).
Nobody has read it yet; it needs a session in `platform`.

## How Team is built

- **Building agents:** `crew up` (Claude per worktree). Graded by `crew check`
  and gated by `crew accept`. Attended, so the free-only rule does not apply.
- **Operating agents:** Hermes workers on free models (the `profiler` role first).
  Graded by code.
- Until the foreman exists, the session in this repo acts as foreman: it writes
  the briefs, launches walls and records outcomes. Jobs that ran before the
  ledger existed are backfilled into it.

## Outcomes ledger (agreed 2026-10-19)

SQLite at `~/state/team/team.db`.
- `jobs`: id, target, target_commit, role, role_version (git sha of that role's
  files), attempt, started, ended, outcome (`pass|fail|error|timeout`), grader,
  grader_detail
- `calls`: job_id, provider, model, task_class, tokens_in, tokens_out,
  latency_ms, ok, error

A job's outcome comes from its grading check, never from a model. No cost
column: the free gate refuses any paid model.

## Conventions

Inherited from `~/.claude/CLAUDE.md`: real numbers only, verify running state
from logs/processes not markdown, no empty scaffolding, portable paths
(`$HOME`, never a hardcoded user dir), no `cd` inside Bash commands.

## Terminal

`work team` opens the herdr session. Layout in `.work.json`.
