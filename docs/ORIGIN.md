# Origin — why `team` exists and what it inherits

Condensed from a Claude Code session in `~/work/brain` on 2026-10-18. Numbers
below were read from the machine that day; re-check before relying on them.

## 1. How Hermes is used today (in `brain`)

Hermes runs as the launchd job `ai.hermes.gateway`. Of its six cron jobs, **five
are plain scripts with no model** (`state-backup`, `fair-value-warm`,
`fair-value-morning`, `brain-referee`, `brain-tick`); Hermes is their scheduler,
logger and timeout. **Only `brain-supervisor` is an agent turn** (free Nemotron
550B reads `brain-health` output, recommends one change, writes a finding).

The "agents" in the newsletter — planner, analyst, learner, researcher, Herald
(synthesizer), editorial chain, referee — are **Python stages in a fixed
pipeline**, each making one or a few model calls through `braincore/llm.py`.
Code owns the control flow; the model fills a slot; write gates check every
output; `brain.db` is the shared memory.

The loop already partly closes: the learner writes measurable claims → the
referee grades them against data and base rates → `brain-health` measures the
brain → the supervisor recommends a change. **Where it stops:** nothing acts on
the recommendation; Eli or a Claude session does.

Weak spots seen in `~/.hermes/cron/executions.db` on 2026-10-18:
- `brain-supervisor`: 13 failed of 47 runs, mostly `Request payload too large
  (413)` — its input is too big for the model. The seed of a foreman, and the
  least reliable job.
- `fair-value-morning`: 6 failed of 44, including 7200s timeouts on 10-02 and 10-16.

Two kinds of agent, kept apart:
- **Operating agents** (the pipeline stages): run unattended every day; must be
  predictable.
- **Building agents** (change code): `crew up` (Claude per git worktree, gated by
  `crew accept`) and Hermes workers via the `portfolio-spawn-worker` skill
  (`hermes chat --yolo` in herdr tabs, routed to free models by task class).
- Claude/Codex are the engineers; Hermes is the operator.

## 2. The idea Eli brought (from a Gemini conversation)

A "software factory" like Warp Factories but free: intake → foreman → role
workers (triage, spec, implement, review) → sandbox → tests → PR; per-agent
models with auto-switching; a meta-agent that reads traces and rewrites
workers' prompts, tools and skills so they get better and cheaper; tools matter
more than the model. Suggested stack: LiteLLM/TensorZero, LangGraph/CrewAI/AG2,
MCP, Docker/OpenHands, Langfuse/Phoenix.

## 3. The mapping — most of that stack already exists in `brain`

| Layer | Suggested | Already have | Missing |
|---|---|---|---|
| Gateway/routing | LiteLLM, TensorZero | `llm.py`: task classes, `ranked()` by quota/speed/latency, cooldowns, usage ledger, free-only allowlist; Hermes `fallback_providers` | Routing by **quality** — no outcome per call |
| Orchestration | LangGraph | Code-defined pipeline (`run_morning.sh` + modules) | — |
| Role definitions | manifest/SKILL.md | Hermes skills, `.agents/skills`, portfolio task routes | One versioned manifest per role |
| Sandbox | Docker, OpenHands | Docker + colima installed; Hermes `terminal.backend` supports containers (set to `local`); crew worktrees | Process isolation for workers |
| Tools | MCP | `brain`, `brain-health`, `brain-claims` CLIs; Hermes `mcp_serve.py` | — |
| Traces | Langfuse, Phoenix | `com.fullmoney.otlp-receiver` running; usage ledger; `executions.db`; Hermes trajectory tools | One table joining **call → outcome** |
| Evals | pytest, LLM judge | `measure.py` + referee (data-graded, base-rated); newsletter checks | Evals on the agents themselves |
| Foreman | meta-agent | `brain-supervisor` | Reliability; anything that acts |

**The missing part is the loop:** outcomes → proposal → evaluation → adoption.

## 4. What not to take from the Gemini plan

- LiteLLM's fallbacks would bypass the free-only gate (Cerebras is on the paid
  list; "escalate to Claude" bills).
- CrewAI/AG2 role-chat frameworks: less precise and harder to grade than a code
  pipeline.
- Its model names are dated (Llama 3.3, Gemini 2.0 Flash, Claude 3.5).
- It skips the hard part: knowing a prompt rewrite *helped*. Without a replay
  eval against a base rate, self-editing drifts.
- Its claims about Warp Factories' internals were not verified.

Worth adopting later: Docker as the Hermes terminal backend for code-executing
workers (soon); TensorZero only if a plain outcomes table proves insufficient
and only behind the free gate; Langfuse no (needs Postgres + ClickHouse);
OpenHands optional. Free models are weakest at long autonomous coding loops —
keep them on operating work and prompt-level changes.

## 5. The architecture sketch

```
foreman (Python, scheduled by Hermes cron)
  state:   typed record per job — target, role, inputs, attempt, model, outcome
  actions: fixed set — dispatch(role) · retry(model+1) · escalate_to_human
           · propose_skill_edit · propose_route_change
  model:   only to (a) classify work into a role
           (b) diagnose a failed trace -> one proposed change
  gates:   code checks every action and proposal

worker role = manifest (task class, tools, budget, grading check) + SKILL.md

self-improvement:
  1 record   every call: target, role, model, tokens, latency, pass/fail by code
  2 diagnose foreman proposes ONE change from failures
  3 evaluate replay the role on a fixed set of past inputs vs current base rate
  4 adopt    git commit if better; code changes via crew + crew accept
  5 route    ranked() weighs pass rate per (target, role, model)
```

New for `team`, beyond `brain`: **targets.** Point it at a repo → an onboarding
pass reads the repo (CLAUDE.md, tests, CI, language, conventions) and writes a
target profile; base roles get per-target skill overlays; outcomes are keyed by
target, so the foreman tunes each target separately.

## 6. Suggested order (from the brain session)

1. Fix the supervisor's 413 (a structured digest instead of raw output) — in `brain`.
2. Outcomes table — agree its columns with Eli first.
3. Manifests for existing stages, one at a time, each with a grading check.
4. Foreman v1: propose → evaluate → adopt, skill edits only.
5. Routing by outcome; then Docker-isolated code workers.

For `team` specifically, the first footing is: target attach + profile,
one role, outcomes ledger, foreman that dispatches and records. Nothing self-edits
until the replay eval exists.
