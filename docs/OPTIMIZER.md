# Conversational Team Optimizer

Run `team optimize` to compile recent Team evidence and discuss concrete changes
with the advisory agent. It uses the full Hermes runtime and Claude subscription,
with its own role instructions and persistent conversation history.

```sh
team optimize
team optimize --prompt "Review implementation retries and propose the strongest fix"
team optimize --resume <printed-session-id>
team optimize --list
```

Interactive startup makes an initial evidence review, then accepts follow-up
messages. `--prompt` performs one assignment and exits. `--list` reads saved
recommendations without calling a model. `/quit` exits. No background scheduler
is installed: evidence collection happens when the agent is invoked or refreshes
its evidence during discussion.

The default evidence window is the latest 100 jobs (up to 200 per collection),
with an optional target filter. It includes goal/decision records, sample outcome
counts and a source catalog. Detail tools retrieve model calls, task/board
history, role session messages, implementation source, role policies and local
memory/skill changes. Large sources and sessions are paginated; clipped fields
are marked. Missing records are surfaced, not treated as successful or failed
work. Source hashes identify current local contents; the ledger's role commit
alone does not identify uncommitted or subsequently learned profile changes.

The optimizer should produce a concrete before/after recommendation for a named
agent or workflow, scoped to the relevant task/project, with a short explanation,
evidence, uncertainty and a validation plan. It may say the evidence is insufficient.
The host validates required fields and that citations were actually retrieved;
it cannot prove the model's causal reasoning or proposed improvement is correct.

Recommendations are saved as private JSON files under
`$TEAM_STATE/optimizer/proposals/`. Each contains its evidence snapshot and status
`proposed`. Revisions reference `supersedes`; older proposals remain intact.
Discussion, including rejection or clarification, lives in the Hermes session.
There is no automatic approval, evaluation, adoption or implementation action.

This agent exposes only evidence collection, source/session inspection, proposal
history, proposal creation and clarification. It has no terminal, project-write,
board-mutation, dispatch or merge tools. Background self-review is disabled.
Full upstream skills are seeded as for other roles, but broad skill-management
and memory-write tools are not granted to this advisory role. Its task-specific
procedure is injected from `roles/optimizer/skills/evidence-to-proposal/SKILL.md`.

Sources: `team/optimizer.py`, `roles/optimizer/role.toml`,
`roles/optimizer/SOUL.md`, and the existing native Hermes launch integration.
