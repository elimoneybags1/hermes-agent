# Choosing models per stage: measured, not guessed

Eli, 2026-10-24, asked how the stages of the line (`docs/PIPELINE.md`) pick
their models: "use TensorZero's model swapping, or have the idea of what models
work best and are most cost and speed efficient for each part of the task."

**TensorZero itself: no.** Its repo is archived (checked 2026-10-24: archived,
last release 2026.6.0 on 2026-06-04), it needs a database server, and it
routes single model calls, while Team grades whole agent runs (a spec, a
change, a review). **Its idea: yes.** Every stage is a *function*; each
candidate model is a *variant*; every graded attempt is *feedback*; an
*adaptive experiment* sends more work to the variants that do best, and keeps
trying the others a little. Team already records what this needs: `jobs`
(outcome by code, role, target) and `calls` (model, tokens, latency).

## Candidates per role

`role.toml` gains:

```toml
[models]
candidates = ["nvidia/nemotron-3-ultra-550b-a55b:free", "qwen/...:free", "claude"]
pinned = ""          # set from the dashboard to force one; "" = choose by data
```

Free candidates come from OpenRouter's live list of `:free` models, filtered
by `team/free.py` (fails closed). `claude` is Claude Code on the
subscription. Eli or the coach adds or removes candidates from `team ui`.

## Choosing, per attempt

`team/models.py`, `choose(conn, role, target, attempt, rng) -> str`:

1. `pinned` wins.
2. Otherwise score each candidate from the ledger for that role (that target's
   jobs weighted 3×, other targets 1×, last 60 days):
   `P(pass)` drawn from Beta(passes + 1, fails + 1) (Thompson sampling: an
   untried model gets real chances; a proven one wins most of the time),
   minus a **speed** cost (median minutes per attempt × `models.minute_weight`)
   and a **cost** term: free = 0, `claude` = `models.claude_cost` (the
   subscription has limits; it is scarce, not free).
3. Retries try a different candidate from the one that just failed, unless only
   one is left. Claude remains the last resort after the role's free attempts
   (as today) even when it is not chosen first.

Every choice is recorded (`calls.model` already, plus `jobs.model_choice`:
chosen, why, the scores) so the dashboard can show the reasoning.

## Settings

`models.claude_cost` (float, 0.3: how much pass-probability a Claude attempt
must beat a free one by), `models.minute_weight` (float, 0.005),
`models.explore` (bool, true: off = always take the best mean, no sampling).

## Dashboard

Each agent's Overview gains a **Models** table: candidate, attempts, passes
(`3/7`, never a bare %), median time, last used, the current score; pin and
unpin buttons. The coach (self-improvement) proposes adding a new free model
when OpenRouter lists one, and dropping one that keeps losing.

## Build

After `docs/PIPELINE.md` step 1 (roles), before step 2: `team/models.py`,
`role.toml [models]`, `settings` fields, `team run` calls `choose` for every
attempt and passes `--model`. Then the Models table in `team ui`.
