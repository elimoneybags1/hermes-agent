# Tools Team builds on

What already exists that Team should use instead of building, checked
2026-10-19. "CLI" means verified from the installed command on this machine;
"docs" means read from the project's documentation, not run here. Re-check
before relying on a version or a default.

## Hermes Agent (v0.21.0, installed) — the worker runtime

| Feature | Command | Team uses it for | Checked |
|---|---|---|---|
| Per-run limits | `hermes chat --max-turns N --run-budget S` | Role `budget` in `role.toml` | CLI, used |
| Usage file | `hermes -z PROMPT --usage-file PATH` | Cost, tokens, model, api_calls per job, even on failure. Oneshot only, not `chat -Q` | CLI |
| Isolated runs | `--source tool`, `--ignore-user-config`, `--ignore-rules` | Reproducible worker runs, kept out of Eli's session list | CLI |
| Session export | `hermes sessions export - --session-id ID --format jsonl\|trace` | The ledger's `calls` row today; the replay set later | CLI, used |
| Repo-local skills | `./.agents/skills` or `./.hermes/skills` in a trusted repo; `hermes skills list\|inspect\|diff` | Worker `SKILL.md` files kept in git, so adopt and roll back are commits | CLI |
| Bundles | `hermes bundles create <name> --skill a --skill b` | One kit of skills per role | CLI |
| Curator | `hermes curator usage --json \| ledger \| rollback` | Use, view and patch counts per skill; an audit log of skill changes | CLI |
| Learning timeline | `hermes journey --json` | What Hermes learned, as input to diagnosis | CLI |
| Kanban | `hermes kanban create ... --workspace worktree --max-retries --max-runtime`, `swarm --verifier` | Overlaps the foreman; read before building `team dispatch` | CLI (help only) |
| Profiles | `hermes profile create` | An isolated free-only worker profile (see below) | CLI (help only) |
| Hooks | `pre_tool_call` (can block, fails closed), `post_api_request`, `on_session_end` | Block paid routes inside workers; log the model actually used per call | CLI + docs |
| Cron | `hermes cron create --no-agent --script`, `notepad` | Scheduling the foreman and nightly replay evals | CLI |
| Verify | `hermes verify --detect-only --json` | Finds a target's build and test commands, for the profiler and the grader | CLI (help only) |
| Sandboxes | terminal backends: docker, singularity, modal, daytona, vercel; `checkpoints` | Later: Docker for code-running workers | CLI (help only) |

### Paid-model risks found in Hermes

- **Background tasks run on `provider: auto`, and `auxiliary.free_only` is
  `false`.** Background review, curator and compression all use that route,
  so a worker pinned to a free chat model can still start them on a paid model.
- The default MoA preset is paid: `openai-codex:gpt-5.5`,
  `openrouter:deepseek/deepseek-v4-pro`, `openrouter:anthropic/claude-opus-4.8`.
- The fallback chain is `nemotron …:free` → `gemini-2.5-flash` (Google) →
  `openai/gpt-oss-120b` (Groq). Whether the last two are on a free tier
  depends on how those accounts are set up; not verified.
- Curator and background review rewrite skills with no approval
  (`write_approval: false`). That bypasses Team's evaluate-then-adopt gate.
- The machine holds a paid Anthropic key and an OpenAI Codex login. Over 7
  days, `hermes insights` showed 2 sessions on `gpt-5.6-luna` billed as
  subscription; not Team's.

## Self-improvement

| Project | Use | Verdict |
|---|---|---|
| [GEPA](https://github.com/gepa-ai/gepa) `optimize_anything` (MIT, v0.1.4) | Proposes changes to any text (prompt, `SKILL.md`, config) against our own evaluator; reads failure traces such as pytest output | **Adopt for the propose step.** Set its reflection model to a free one. Team still adopts one diff at a time, gated on the base pass rate |
| [Hermes Agent Self-Evolution](https://github.com/NousResearch/hermes-agent-self-evolution) | Nous's DSPy+GEPA pipeline that evolves Hermes skills, with gates and an eval set drawn from past sessions | **Adopt the pattern.** It's the closest existing version of Team's loop; swap its PR step for our commit gate |
| [DSPy](https://github.com/stanfordnlp/dspy) (MIT, 3.4.1) | Prompt programs plus optimizers | Only if a role becomes a DSPy program |
| TensorZero | Gateway with feedback and A/B tests | **No.** The repo is archived (last release 2026.6.0, 2026-06-04) and needs Postgres. Copy its idea instead: a job is an episode, tests passed is a boolean metric. Done in `docs/MODELS.md` (re-checked 2026-10-24: still archived) |
| TextGrad | Gradient-style prompt edits | No: stale since 2025 |

## Evaluation and replay

| Project | Use | Verdict |
|---|---|---|
| [Inspect AI](https://github.com/UKGovernmentBEIS/inspect_ai) + `inspect_swe` (MIT) | Datasets, solvers and scorers; replays a coding agent on fixed inputs, scored by commands in a sandbox | Later, if a small runner over the ledger gets too big |
| promptfoo | YAML test matrix for single prompts | No, for replaying repo jobs |

## Worker designs to borrow

| Project | Borrow | Verdict |
|---|---|---|
| [mini-swe-agent](https://github.com/SWE-agent/mini-swe-agent) (MIT) | ~100-line bash-only agent | A baseline worker for free models that are weak at tool calling |
| [Agentless](https://github.com/OpenAutoCoder/Agentless) | Localize → repair (sample N patches) → validate by tests | The "sample N patches, keep the one that passes" step: cheap on free models |
| [OpenAI Symphony](https://github.com/openai/symphony) SPEC.md | Ticket → isolated run → proof of work | Reference for the foreman's job state machine |
| OpenHands, SWE-agent | Full platforms | No, as runtimes |

## Traces

| Project | Use | Verdict |
|---|---|---|
| [Arize Phoenix](https://github.com/Arize-ai/phoenix) (Elastic License 2.0, source-available) | Local trace viewer; stores in SQLite, takes OTLP on 6006/4317 | When traces need viewing. Check for a port clash with `com.fullmoney.otlp-receiver` |
| OpenLLMetry (Apache-2.0) | OTel instrumentation for LLM SDKs | Only if Hermes doesn't already emit the spans we need |
