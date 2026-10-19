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

## Open decisions — ask Eli, do not assume

1. Where a target's specialization lives: in the target repo (`.team/`) or in
   `~/state/team/targets/<name>/`.
2. Reuse `brain`'s router by copying it, or extract it into a shared package.
3. Overlap with `~/work/platform/ai` (gateway, router, agents, skills) — not yet
   read; it may already hold pieces of this.
4. First target repo (`brain` is the natural one: it has real evals already).
5. Sandbox: git worktrees (crew) only, or Docker via colima as the Hermes
   terminal backend for workers that execute code.

## Conventions

Inherited from `~/.claude/CLAUDE.md`: real numbers only, verify running state
from logs/processes not markdown, no empty scaffolding, portable paths
(`$HOME`, never a hardcoded user dir), no `cd` inside Bash commands.

## Terminal

`work team` opens the herdr session. Layout in `.work.json`.
