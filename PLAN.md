# Team fresh start: fork Hermes at the root, Portkey and LiteLLM beside it, Team on top

## Context

The old Team repo inverted its stack twice (custom harness over `claude -p`, then the full Hermes fork imported underneath Team's wrappers) and ended with prose rules that were never code gates, skills naming tools that did not exist, and a self-improvement loop with no evaluate or adopt step. Eli archived it (tag `before-fresh-start`, commits `1e3291f`, `a7c7703`) and wrote the intent down in `IDEAS.md` and `FRESH-START.md`. The rebuild makes this repo **be** the fork of `NousResearch/hermes-agent`, with the other upstream projects forked in as subtrees and Team as a layer on top, so updating any of them is a routine `git merge` or `git subtree pull`.

Decisions already made by Eli (fixed):
- This repo, swept; hermes-agent merged at the root; the other projects as git subtrees, run from source, never installed from a registry: `portkey/` (Portkey-AI/gateway; Hermes already owns `gateway/`), `litellm/` (BerriAI/litellm), `self-evolution/` (NousResearch/hermes-agent-self-evolution).
- **LiteLLM chooses the model, Portkey chooses the provider** (Eli, 2026-10-27: "LiteLLM is made to do specifically what Portkey has some of, and can be tuned more easily; a strong connection between the two beats Portkey alone"). Every caller, Hermes profiles and Eli's other tools alike, talks to LiteLLM on 4000 by tier or model name. LiteLLM forwards each call to the open-source Portkey gateway on 8787 with a config *name*; Portkey resolves it locally, holds the only copy of the provider keys, translates the request into the provider's API, and fails over between providers of that model inside one request. Tuning lives in LiteLLM's router settings and callbacks.
- **The gateway comes first.** A free model answering through LiteLLM → Portkey, visible in Portkey's console, is the first result, before any Hermes profile exists.
- Old state moved to `~/state/team-old`; the new Team starts empty. Team's Hermes home isolated at `$HOME/state/team/hermes` (never `~/.hermes`, which belongs to another project and holds paid keys).
- Free-only is enforced by Team's own catalog → generated configs → pinned profiles → audit, fail closed. Never a paid key anywhere Team runs.
- The loop gets its first ledger number before any UI. Claude Code does the coding from outside at first.

Two small, marked patches to the forked gateway make that division clean and are recorded in `PROVENANCE.md`: (1) resolve `x-portkey-config: <name>` from `$PORTKEY_CONFIG_DIR/<name>.json` so callers never carry keys; (2) append one JSON line per request (time, config name, provider, model that answered, status, tokens, latency, trace id) to `$HOME/state/team/portkey/requests.jsonl`, since the open-source gateway has no durable log.

Verified facts this plan rests on (dated 2026-10-27):
- **Hermes** (read in the fork at 48c0c3a; upstream HEAD is e5f2b15, so each is re-grepped in Phase 1 step 8): no fail-closed hook to refuse an API request by model (`pre_api_request` is observe-only); a bundled `standalone` plugin still needs `plugins.enabled`; `pyproject` packages are an explicit include list; `providers.<name>` supports `base_url`, `api_mode`, `extra_headers` with `${VAR}` expansion; the auxiliary client only merges `model.extra_headers`; one dashboard manifest = one tab; `plugin_api.py` exposes `router = APIRouter()`; profiles live at `$HERMES_HOME/profiles/<name>`; `kanban create --model/--provider` bypasses the profile pin; upstream `.gitignore` ignores `/bin/`.
- **Portkey gateway** (repo at v1.15.2, 2026-01-12; MIT): runs with `node build/start-server.js --port=8787` after `npm i && npm run build` (also `npx`/docker, not used); no host bind flag, so loopback-only is enforced by the macOS firewall or by running it in docker with `-p 127.0.0.1:8787:8787`; routing is an inline JSON config in the `x-portkey-config` header with schema `strategy{mode fallback|loadbalance|single|conditional, on_status_codes}`, `targets[]{provider, api_key, custom_host, override_params, weight, retry{attempts,on_status_codes}, request_timeout, forward_headers}`; without a config, `x-portkey-provider` + `Authorization: Bearer <provider key>` route one request; `custom_host` (must include `/v1`) routes to any OpenAI-compatible host with `provider: openai`; provider slugs exist for openrouter, groq, gemini, mistral-ai, cohere, cerebras, z-ai, not for NVIDIA (use `openai` + `custom_host: https://integrate.api.nvidia.com/v1`); a local console at `/public/` gated by `admin_token` in `conf.json` streams logs over SSE, with no durable log store and no health route; `conf.json` `integrations` (server-side keys, `@slug`) are read by `initializeSettings.ts` only when `FETCH_SETTINGS_FROM_FILE=true` and **have no consumer elsewhere in the open-source runtime**, so server-side keys are not relied on.
- **LiteLLM proxy**: `model_list[].litellm_params` supports `model: openai/<id>`, `api_base`, `api_key`, `extra_headers: {...}`, and `os.environ/<VAR>` for any value. The archived `litellm/config.yaml` (59 free models across 8 providers, four `tier-*` groups, `session_pin`, `usage_log`, `spend_guard` callbacks) is the starting point.
- **Machine**: uv 0.12.5, Node 26.7, npm 11.19 (inside Hermes's engine ranges), Python 3.11.16 via uv, `claude auth status` = claude.ai first-party, Docker running, `gh` logged in, ports 4000/8787/9119 free, no Team processes left, LiteLLM still installed at `~/.local/bin/litellm` (will be replaced by the from-source run).

Paths Team owns in the new tree: `team/`, `plugins/team/`, `integrations/`, `tests/team/`, the subtrees `portkey/`, `litellm/`, `self-evolution/` (updated only by `git subtree pull`), `PROVENANCE.md`, `CLAUDE.md`, `.work.json`, `IDEAS.md`, `FRESH-START.md`, `PLAN.md`, plus three lines in `pyproject.toml` and a `# Team` block in `.gitignore`. Upstream files are never edited in place; a subtree is a vendor copy, and any patch to one is a marked, minimal commit recorded in `PROVENANCE.md`.

---

## How each piece works, and what each connection does in practice

### The pieces

**Hermes Agent** is the runtime. One process runs an agent loop: build the prompt (SOUL, skills index, memory, context files), call a model, execute the tools the model asked for, repeat until the model answers. Everything else is a front door to that loop or a service around it: the terminal (`hermes`, `hermes --tui`), the messaging gateway (`hermes gateway`: Telegram, Discord, Slack and so on), the web dashboard (`hermes dashboard`, port 9119), the desktop app. A **profile** is one configured agent: its own `config.yaml` (provider, model, tools, every side-task model), `SOUL.md`, skills, memory and sessions, in its own directory. Team makes one profile per role, so "the implement worker" is literally a Hermes profile named `implement`. Hermes decides which model to call by reading `model.provider` in the profile, finding that name under `providers:`, and sending an OpenAI-style request to that `base_url` with that provider's `extra_headers`.

**Kanban** is a subsystem inside Hermes: a SQLite board of tasks with status columns (`ready`, `running`, `review`, `done`, `blocked`), parent links, and a dispatcher. `dispatch` takes a ready task, creates or reuses its workspace (a git worktree when asked), and spawns a Hermes process for the task's assignee profile with the task body as the prompt and env vars naming the task, run and workspace. The worker has `kanban_*` tools: it comments, attaches files, and ends by calling `kanban_request_review`, which moves the task to `review`. Nothing in kanban runs tests or merges; it only runs agents and tracks state. Team turns `dispatch_in_gateway` and `review_dispatch` off so kanban never spawns on its own and never lets a model be the reviewer.

**Portkey gateway** is a stateless HTTP router. A client sends an OpenAI-shaped chat request to `http://127.0.0.1:8787/v1/chat/completions` with a JSON config in the `x-portkey-config` header (or, with Team's fork patch, the name of a config the gateway loads from its own directory). The config lists targets in order, each naming a provider, that provider's API key, and the model to use. Portkey tries the first target, and on a listed failure status (429 rate limit, 5xx) moves to the next, translating the request into each provider's own API shape and the response back into OpenAI shape. It keeps nothing between requests. Its `/public/` console shows the live log stream for the process, nothing durable.

**LiteLLM proxy** is also an OpenAI-shaped router, with a different center of gravity: a `model_list` of named deployments, groups of deployments under one name with fallbacks between groups, cooldowns, retries, and Python callbacks that see every request (Team's `session_pin` keeps a job on one model; `usage_log` writes every call to `usage.db`). Eli already points other tools at it on port 4000.

**Team** is the layer that makes work accountable. It owns: the catalog of allowed models and the generator that turns it into Portkey configs, LiteLLM's config and the Hermes provider blocks; the role files synced into Hermes profiles; the ledger (`jobs`, `calls`, `goals`, `decisions`, `plans`, `parts`); `team run`, the loop that dispatches, grades by running the target's check in the worktree, lands by merge, retries on the next model, escalates to Claude, and blocks for Eli; the decisions inbox; and later the replay set that decides whether a proposed change is adopted. Team never calls a model itself except through Hermes profiles or the Claude subscription adapter.

**Claude subscription adapter** is a loopback server that looks like an OpenAI endpoint to Hermes and runs `claude -p` underneath with every API-key variable removed, so the boss profile thinks with Claude on Eli's subscription while Hermes keeps owning tools, memory and sessions.

**Self-evolution** (Nous's pipeline) reads past agent sessions, proposes rewrites of skills and instructions with a reflection model, and evaluates them. Team replaces its evaluation with the replay set and its adoption with a `--no-ff` merge that only happens when the replay pass rate beats the base rate.

**herdr** is the room: it opens the Team layout (boss terminal, dashboard web pane, shell), keeps those processes alive across app restarts, and gives splits, tabs and drag. It runs nothing of Team's logic.

### The connections, as one request travels

1. **Eli → boss.** Eli types in the boss pane (`team/bin/team-hermes -p boss`). Hermes loads the `boss` profile. Its `model.provider` is `subscription`, so the request goes to the loopback adapter on 8790, which runs `claude -p` and returns the answer. The boss's side tasks (compression, title generation) go to LiteLLM `tier-simple`, so they cost nothing.
2. **Boss → Team → kanban.** The boss calls `team_part_create`. Team validates the model spec against the catalog, creates a worktree and branch, writes `jobs` and `goals` rows, and runs `kanban create` on the target's board with the task body, the `implement` assignee, the worktree, and the validated `--provider`/`--model`. The task is now `ready`. Nothing is running yet.
3. **`team run` → kanban → worker.** Each tick Team runs `kanban dispatch --max N`. Kanban spawns `hermes -p implement` in the worktree with the task as its prompt. The worker reads its SOUL and skills, edits files, runs the check itself, commits, and calls `kanban_request_review`.
4. **Worker → Hermes → LiteLLM → Portkey → provider.** Every model call the worker makes is Hermes reading the `implement` profile: provider `litellm`, base URL `http://127.0.0.1:4000/v1`, model `tier-medium`, header `x-litellm-session-id` = the kanban task id. LiteLLM picks a deployment in that tier (pinned per session after the first answer, cooled down after failures) and forwards to Portkey with `x-portkey-config: team-model-<slug>` and the session id as the trace id. Portkey resolves the config, calls the first provider that serves that model, fails over to the next provider of the same model on 429 or 5xx, and returns an OpenAI-shaped response; if every provider of that model fails, LiteLLM sees one failure and moves to the next model in the tier. The Team plugin's `post_api_request` hook writes a `calls` row with the model that actually answered, the tokens, and the task id; `usage.db` and `requests.jsonl` hold the same call under the same id. If the base URL or model were ever outside the catalog, the `calls` row is marked a violation and `team run` blocks the task.
5. **Review → grade → land.** `team run` sees the task in `review`. It merges `main` into the branch, runs the target's check command in the worktree, and reads the exit code. Pass: `kanban complete`, `git merge --no-ff` into the target's `main`, ledger `pass`. Fail: comment the failure tail on the task, `reopen-review`, attempt + 1 with the next candidate model. After the role's free attempts: one Claude attempt in the same worktree through `claude -p` in build mode (stop hook refuses to finish while the check fails; edit hook refuses files outside the task). After that: `block --kind needs_input` and a decision in Eli's inbox.
6. **Other tools → LiteLLM → Portkey.** Anything Eli already points at port 4000 keeps working unchanged and takes the same path as Team's workers; `usage.db`, `requests.jsonl` and the Portkey console show those calls alongside Team's.
7. **Dashboard.** `hermes dashboard` serves the Hermes web UI; the Team plugin adds a tab whose API reads the ledger and the kanban DB. The Connections view shows each hop above with evidence: the gateway's launchd state and last call, LiteLLM's last `usage.db` row, each profile's lock audit, the adapter's port, the ledger's pass count next to its base rate.

### What each connection buys, and what it costs

- **Hermes → LiteLLM**: Hermes holds one LiteLLM key and speaks in tier names; model choice, cooldowns, session pinning and callbacks are tuned in one `router_settings` block for every caller. Cost: one more local hop per call.
- **LiteLLM → Portkey**: keys exist only in Portkey; provider API translation, per-provider failover for the same model, circuit breaker and guardrails are Portkey's. Cost: two processes and two logs, joined by one trace id so a failure reads as one story.
- **Catalog → everything**: one file decides what models exist anywhere in the system. Cost: a new free model needs a catalog entry, a `build.py` run and a restart of both proxies. `build.py --check` on every tick means a stale config stops the loop instead of serving.
- **Kanban as the queue**: boards, worktrees, spawning, review state and the board UI come for free and keep improving upstream. Cost: kanban's worker exit is not "done"; Team's grading is, and the `review` column is the handshake.
- **Claude only through the subscription adapter or build mode**: never a paid API key in the system. Cost: text-only, no streaming, bounded per turn; the boss is attended, so that is acceptable.

---

## Phase 1: repo surgery

All from `~/work/Team` with `git -C`.

1. `mv ~/state/team ~/state/team-old`; `git worktree prune`.
2. Sweep: `git submodule deinit -f vendor/hermes-agent`; `git rm -r -q .`; `git checkout HEAD -- IDEAS.md FRESH-START.md PLAN.md`; `rm -rf .git/modules/vendor .venv .pytest_cache`; commit "Sweep for the fresh start; the old tree is tag before-fresh-start". Evidence: `git ls-files` prints three paths.
3. `git remote add upstream https://github.com/NousResearch/hermes-agent.git`; `git fetch upstream main`; record the sha. **Done 2026-10-27: `e5ea5fd9`, merged as `8ee29e6d`; 21,450 files; no filename clashes with Team's files.**
4. `git merge --allow-unrelated-histories --no-edit upstream/main` (upstream has `AGENTS.md`, not `CLAUDE.md`; no conflicts expected).
5. Subtrees, each pinned and squashed, each verified with `git ls-remote` first:
   - `git subtree add --prefix=portkey https://github.com/Portkey-AI/gateway.git v1.15.2` (full history, no squash: this subtree gets patched). The prefix is `portkey/` because Hermes already owns a top-level `gateway/` (its messaging gateway).
   - `git subtree add --prefix=litellm https://github.com/BerriAI/litellm.git v1.106.4 --squash` (latest stable on 2026-10-25)
   - `git subtree add --prefix=self-evolution https://github.com/NousResearch/hermes-agent-self-evolution.git main --squash`
6. `PROVENANCE.md`: every upstream, its URL, pinned ref and sha, the date, the update command (`git fetch upstream && git merge upstream/main`; `git subtree pull --prefix=<dir> <url> <ref> --squash`), the ownership rule, and the `before-fresh-start` tag sha.
7. `CLAUDE.md` (Team-owned), `.work.json`, `# Team` block in `.gitignore` (`*.db`, `*.db-wal`, `*.db-shm`, `portkey/build/`, `portkey/node_modules/`, `litellm/.venv/`, `plugins/team/dashboard/node_modules/`, `/self-evolution/.venv/`). Do not touch `AGENTS.md`.
8. Re-grep at e5f2b15 before writing Team code, line numbers into `PROVENANCE.md`: `[tool.setuptools.packages.find] include`; `plugins.enabled` gating in `hermes_cli/plugins.py`; `VALID_HOOKS`; `kanban create --model/--provider`; `platform_toolsets`; `_discover_dashboard_plugins` and manifest keys in `hermes_cli/web_server.py`; `extra_headers` and `${VAR}` expansion in `hermes_cli/config.py`, `agent/agent_init.py`, `agent/auxiliary_client.py`.
9. Commit "Merge hermes-agent <sha> at the root; subtrees gateway, litellm, self-evolution; provenance".

**`pyproject.toml`, three lines at the end of their sections:** `[project.scripts] team = "team.cli:main"`; append `"team", "team.*"` to the packages include; `[tool.setuptools.package-data] team = ["roles/**/*", "bin/*"]`. No new dependencies.

**`CLAUDE.md` outline:** this repo is the Hermes fork, Portkey and LiteLLM are subtrees, Team is the layer; read `IDEAS.md`, `PROVENANCE.md`, `FRESH-START.md` §3, `PLAN.md`; the ownership rule; run everything through `team/bin/team-hermes`; the free-only rule and its enforcement layers; `pytest tests/team -q` is the gate for Team changes, upstream suites only after upstream merges; conventions (real numbers with base rates, verify from processes and logs, no empty scaffolding, `$HOME` paths, no `cd`, commit as you go, never push without asking).

**`.work.json`:** layout `Team`, columns `{"label":"Team","claude":true}`, `{"label":"boss","command":"team/bin/team-hermes -p boss"}` (works after Phase 5), `{"label":"dashboard","web":"http://127.0.0.1:9119/team"}` (needs herdr's web pane; until then `command: open <url>`), `{"label":"shell"}`. Gate rules: upstream files not edited; `$HOME` paths; free-only under `$HOME/state/team`; `pytest tests/team -q` passes.

**Acceptance:** `git log --oneline -5` shows sweep, merge, three subtrees, provenance; `git ls-files | wc -l` ≈ the four upstreams' file counts plus Team's six files; `~/state/team-old` intact.

---

## Phase 2: Portkey gateway and LiteLLM from source, the free catalog

This is the first thing that runs. Nothing here needs Hermes.

> **Handed off (Eli, 2026-10-27).** This phase is being built in a separate Claude Code session, briefed by `HANDOFF-GATEWAY.md`. Eli's words: the connection between LiteLLM and Portkey "will be dope", meaning it is the strong, tuned heart of model routing for everything on this machine, not a thin forward. Once it is proven by the acceptance list below, the Team-owned connection layer (the catalog generator, the config-by-name resolver, the request log, the trace join, the status checks) is to be **rewritten in Rust** as one binary with the same catalog in and the same files out, so the same acceptance list passes against it. The gateway session owns `portkey/`, `litellm/`, `integrations/portkey/`, `integrations/litellm/`, `tests/team/test_portkey_build.py` and `$HOME/state/team/{portkey,litellm}`; this session owns everything else and will not touch those paths. Both work on `main` in this working tree and commit only their own paths.

**Layout**
```
integrations/portkey/   catalog.json  build.py  run.sh  install-launchd.sh  status.sh  README.md
integrations/litellm/   run.sh  install-launchd.sh  usage_log.py  session_pin.py  status.sh  README.md
```
State, outside the repo: `$HOME/state/team/portkey/{.env, configs/, conf.json, requests.jsonl, gateway.log, spend.json}` and `$HOME/state/team/litellm/{.env, config.yaml, usage.db, proxy.log}`. The two `.env` files are the only hand-written secrets: provider keys in Portkey's, the master key in LiteLLM's. Generated files are mode 600.

**The two gateway patches** (in the `portkey/` subtree, each one commit marked `[team]` and listed in `PROVENANCE.md` with the upstream file and line; re-applied by `git subtree pull` merges):
1. **Config by name.** In `portkey/src/handlers/handlerUtils.ts` where `x-portkey-config` is parsed: if the header value is not JSON and matches `^[a-z0-9-]+$`, read `$PORTKEY_CONFIG_DIR/<name>.json` (cached, re-read on mtime change) and use it as the config; unknown name → 400. Callers never carry keys.
2. **Durable request log.** After each request completes, append one JSON line to `$PORTKEY_REQUEST_LOG` (time, config name, provider, model requested, model that answered, status, prompt and completion tokens, latency ms, `x-portkey-trace-id`, `x-portkey-metadata`). No prompt text.
Both are a few dozen lines, each with a jest test in the subtree's own suite.

**`catalog.json` (the one hand-edited source of truth)**: `gateway {port 8787}`, `litellm {port 4000}`, `blocked_providers {anthropic, minimax, dashscope: reason}`, `providers {openrouter {portkey: openrouter, key_env OPENROUTER_API_KEY, free_basis, limits}, nvidia {portkey: openai, custom_host https://integrate.api.nvidia.com/v1, key_env NVIDIA_API_KEY}, groq, gemini, mistral, cohere, cerebras, zai}`, `tiers [simple, medium, complex, reasoning]`, `models [{id, tier, order, context, max_input, tools, reasoning, unavailable, served_by: [{provider, provider_model_id, free yes|allowance, why}]}]`: one catalog model can be served by several providers, in order. Seeded from `git show before-fresh-start:litellm/free-models.json` (59 entries, 8 providers). Portkey slugs are confirmed against `portkey/src/providers/<name>/` in the subtree.

**`build.py` (one catalog, four outputs; refusals ported from the archived `litellm/build.py`)**: every `served_by` entry `free ∈ {yes, allowance}`; provider known and not blocked; OpenRouter ids end in `:free`; every tier non-empty; no duplicate model ids; no config over 12 KB. Outputs:
1. `$HOME/state/team/portkey/configs/team-model-<slug>.json`, one per catalog model: `strategy {mode fallback, on_status_codes [429, 500, 502, 503, 529]}`, one target per `served_by` provider in order with `provider`, `api_key` (resolved from Portkey's `.env` at build time), `custom_host` where needed, `override_params {model: provider_model_id}`, `retry {attempts 1}`, `request_timeout`, `cb_config`. Portkey picks the provider.
2. `$HOME/state/team/portkey/configs/team-tier-<t>.json`: the same shape over every model of a tier in order, for callers that bypass LiteLLM (status checks, emergencies). Not the normal path.
3. `$HOME/state/team/litellm/config.yaml`: one deployment per catalog model, `model_name: <id>` and a second entry under its `tier-<t>` group, `litellm_params {model: openai/<id>, api_base: http://127.0.0.1:8787/v1, api_key: team, extra_headers: {x-portkey-config: team-model-<slug>}}`, `model_info` (cost 0, context, capabilities, `id: <slug>`); `router_settings` carried from the tag (`enable_pre_call_checks`, `num_retries 1`, `allowed_fails 1`, `cooldown_time 300`, tier fallbacks); `litellm_settings.callbacks: [session_pin.pin, usage_log.logger]`; `general_settings.master_key: os.environ/LITELLM_MASTER_KEY`. LiteLLM picks the model. Its environment holds no provider key.
4. `$HOME/state/team/portkey/conf.json`: `{"admin_token": "<generated once, kept in Portkey's .env>"}` for the console.
`--check` regenerates in memory and diffs against disk; both `run.sh` scripts and every `team run` tick refuse on drift.

**Trace join.** `session_pin.py` already keys on `x-litellm-session-id` (set by Hermes workers to the kanban task id, by the boss to its session). LiteLLM forwards it as `x-portkey-trace-id` and `x-portkey-metadata {"session": ..., "caller": "litellm"}` through `extra_headers` plus a tiny `forward_headers` callback, so `usage.db`, `requests.jsonl` and Team's `calls` table line up on one id.

**Portkey process (`integrations/portkey/run.sh`)**: `set -a; . $HOME/state/team/portkey/.env; set +a`; `unset` the paid-key names; `build.py --check`; `npm --prefix portkey run build` if `portkey/build/` is older than `portkey/src/`; `PORTKEY_CONFIG_DIR=$HOME/state/team/portkey/configs PORTKEY_REQUEST_LOG=$HOME/state/team/portkey/requests.jsonl exec node portkey/build/start-server.js --port=8787` with cwd set so the console finds `conf.json` (confirm the path the built server reads). Loopback: the server has no host flag; verify what `@hono/node-server` binds by default and, if it is all interfaces, add `--host` in patch 1 or run under docker with `-p 127.0.0.1:8787:8787`; record which. `install-launchd.sh` writes `com.fullmoney.team-portkey` (KeepAlive, log at `gateway.log`, paths from `$HOME`), ported from the archived `litellm/install-launchd.sh`.

**LiteLLM process (`integrations/litellm/run.sh`)**: own venv `uv venv ~/state/team/litellm-venv --python 3.11` and `uv pip install -e "litellm[proxy]"` from the subtree (its pins clash with Hermes's); `set -a; . $HOME/state/team/litellm/.env; set +a`; `build.py --check`; `exec litellm --config $HOME/state/team/litellm/config.yaml --host 127.0.0.1 --port 4000` with cwd `integrations/litellm` so the callbacks import. launchd `com.fullmoney.team-litellm`. `usage_log.py` and `session_pin.py` ported from the tag.

**Spend guard (`team/spend.py`, Team-owned, runs in `team run` and in `status.sh`)**: poll OpenRouter's credits endpoint (first version), snapshot to `$HOME/state/team/portkey/spend.json`; any movement writes `tripped` and `team run` refuses to dispatch until `team spend reset`, which also opens an inbox decision.

**Acceptance (the first visible result):**
- Both launchd jobs `state = running`; `lsof -iTCP:8787 -iTCP:4000 -sTCP:LISTEN` shows both on 127.0.0.1.
- `curl` one chat completion to Portkey with `x-portkey-config: team-tier-simple` returns a catalog model; `requests.jsonl` gained a line naming the provider that answered.
- `curl` one chat completion to LiteLLM (`model: tier-simple`, master key, `x-litellm-session-id: smoke-1`) answers; the same call appears in `usage.db`, in `requests.jsonl` with trace id `smoke-1`, and in the console at `http://127.0.0.1:8787/public/`.
- Kill one provider key in Portkey's `.env`, rebuild: a model served by two providers still answers through the second (Portkey failover); a tier whose first model is unavailable answers with the next (LiteLLM fallback).
- `build.py --check` passes clean and fails after a hand edit; both `run.sh` refuse to start on drift.
- Unit tests `tests/team/test_portkey_build.py`: refuses `free: no`, a blocked provider, a non-`:free` OpenRouter id, an oversized config; the four outputs are deterministic. Gateway patch tests pass in the subtree (`npm --prefix portkey run test:gateway -- handlerUtils`).
- `grep -rl API_KEY $HOME/state/team/litellm/` returns only its `.env` (the master key); provider keys appear only under `$HOME/state/team/portkey/`.

---

## Phase 3: Hermes from source, locked profiles on LiteLLM, first task

1. **Done 2026-10-27, the upstream way.** Hermes now provisions its own environment: `scripts/run-in-hermes-env CMD` runs `setup-hermes.sh --runtime-only`, downloads pinned uv, Python 3.14.9, node 26.12, ripgrep, ffmpeg, chromium and the CUA driver into `$HERMES_RUNTIME_DIR` (915 MB), and builds the dependency venv under `$HERMES_HOME/installs/<hash>/environments/<hash>/venv` (1.1 GB). No hand-made venv: `pm/AGENTS.md` forbids raw pip or uv into it. `npm --prefix ~/work/Team install` done.
2. **Done.** Wrapper `team/bin/team-hermes` (sh, symlinked into `~/.local/bin`): resolves its own symlink to the repo; exports `HERMES_HOME=$TEAM_STATE/hermes`, `HERMES_RUNTIME_DIR=$TEAM_STATE/hermes-runtime`, `HERMES_KANBAN_HOME=$TEAM_STATE/kanban`; loads `LITELLM_MASTER_KEY` from `$TEAM_STATE/litellm/.env`; unsets every Anthropic and provider key variable; `exec scripts/run-in-hermes-env python3 ./hermes "$@"`, which is what upstream's `activate` function does.
3. Locked base profile `team/roles/_base/config.yaml`, seeded as `$HERMES_HOME/config.yaml` before the first launch so onboarding never runs: one provider `litellm` (`base_url http://127.0.0.1:4000/v1`, `key_env LITELLM_MASTER_KEY`, `api_mode: chat_completions`, `models: [tier-simple, tier-medium, tier-complex, tier-reasoning]`, `extra_headers: {x-litellm-session-id: "${HERMES_KANBAN_TASK}"}` (verify `${VAR}` expansion inside `extra_headers`; otherwise the Team plugin's `llm_request` middleware sets it), `context_length` from the tier's smallest model); `model.provider: litellm`, `model.default: tier-medium`; `model.extra_headers` mirroring the session header (the auxiliary client only merges that key); `fallback_providers: []`; `plugins.enabled: [team]`; `auxiliary.free_only: true`; all 18 `auxiliary.<task>` entries (vision, compression, skills_hub, approval, review, mcp, title_generation, memory_query_rewrite, tts_audio_tags, profile_describer, goal_judge, curator, monitor, background_review, moa_reference, moa_aggregator, kanban_decomposer, triage_specifier) pinned to `litellm` / `tier-simple`; `auxiliary.background_review.enabled: false`; `curator.enabled: false`; `moa.active_preset: ""`; `kanban {dispatch_in_gateway: false, review_dispatch: false, auto_decompose: false, failure_limit: 2, orchestrator_profile: worker, default_assignee: implement, max_in_progress: 3}`; `delegation` and `cron` pinned; compression threshold ~32K (verify key); `platform_toolsets.cli: [terminal, file, todo, skills, kanban]`. The wrapper loads `LITELLM_MASTER_KEY` from `$HOME/state/team/litellm/.env` into the environment; no profile file holds it. `tests/team/test_profile_lock.py` asserts no `auto`, empty fallbacks, only `litellm`/`subscription` providers, and profile `.env` names limited to `SUBSCRIPTION_*`.
4. Team Hermes plugin `plugins/team/` first half: `plugin.yaml` (standalone; hooks `post_api_request`, `api_request_error`), `__init__.py` `register(ctx)`, `ledger_sink.py` writing a `calls` row per call (model, provider, base_url, response_model, usage, duration, session, `HERMES_KANBAN_TASK/_RUN_ID`, `HERMES_PROFILE`), marking `ok=0` with `error=off-gateway` or `non-catalog` when the base URL or model is outside the catalog; never raises out of the hook. Post-hoc audit; the config pin is the wall.
5. `team/bin/team-hermes doctor`; `team/bin/team-hermes chat -q "say ok"` answers through LiteLLM and Portkey (`usage.db`, `requests.jsonl` and `calls` each gain one row with the same session id).
6. `team/bin/team-hermes dashboard --no-open` builds `hermes_cli/web_dist` and serves 9119 with the Kanban tab.
7. Kanban smoke: `kanban boards create team`; one hand task ("run `echo hello` and request review"); `dispatch --max 1 --json`; `kanban runs`/`log` show a worker spawned under `$HERMES_KANBAN_HOME`, answered by a catalog model, task in `review`.

**Acceptance:** `team/bin/team-hermes --version` prints 0.21.x while bare `hermes` is still the machine's other install; `calls` has rows with `base_url` = LiteLLM and `ok=1`; `grep -r API_KEY $HOME/state/team/hermes/profiles/*/.env` returns nothing; the hand task reached `review` on a catalog model; 9119 listening.

---

## Phase 4: the Team layer, one role, first ledger number

**`team/` package:** `cli.py` (attach, targets, goal, run, ledger, decide, roles sync|list, gate audit, spend status|reset, hook stop|pretool, subscription serve); `ledger.py`; `catalog.py`; `grade.py`; `kanban.py`; `run.py`; `decisions.py`; `settings.py` + `orchestration.toml`; `targets.py`; `roles.py`; `profiles.py`; `claude_run.py`; `claude_roles.py`; `spend.py`; `bin/team-hermes`; `roles/_base/config.yaml`; `roles/implement/{role.toml, SOUL.md, skills/commit-before-done, skills/merge-main-first}`; `docs/RUN.md`, `docs/KANBAN.md`, `docs/PIPELINE.md` carried from the tag and cut to what runs.

Port by reading `git -C ~/work/Team show before-fresh-start:<path>`: `team/ledger.py`, `team/grade.py` (worktree create, `run_check`, `land` with `--no-ff` refusing dirty tree or non-main, abort on conflict, `remove_worktree`, merge main into branch before grading), `team/kanban.py` (subprocess wrapper over `team/bin/team-hermes kanban --board …`; add `reopen_review`, `complete`, `request_changes`, `runs`, `reclaim`), `team/run.py` (the tick), `team/decisions.py`, `team/settings.py`, `team/claude_run.py`, `team/claude_roles.py`, and their tests. Dropped on purpose: `harness.py`, `hermes_native.py`, `hermes_runtime.py`, `hermes_bridge.py`, `planning*.py`, `boss_chat.py`, `ui.py`, `deck.py`, `flow.py`, `dash.py`, `optimizer.py`, `free.py`, `proxy_*`, `models.py` (returns in Phase 7 with replay numbers).

**`team/catalog.py`:** `load()`, `resolve(spec) -> ModelChoice(provider_name, model, litellm_model_name)` for `tier-<t>` or `<provider>:<id>`, raising `GateError` otherwise; `is_free()`. `team/kanban.py::create_task` accepts a `ModelChoice` only, never a free-form `--model`.

**Ledger schema from day one:** `jobs` (+ `task_ref`, `model_choice` JSON, `part_id`), `calls` (+ `task_ref`, `session_id`, `profile`, `response_model`, `base_url`, `api_calls`), `goals` (+ `part_id`, `base`, `needs_commits`), `decisions`, `plans`, `parts` (stage `spec|test_write|implement|test_verify|review|land|done`, `depends_on`, `owned_paths`), `reviews`, `pauses`, `violations`. Indexes on `calls(job_id)`, `calls(task_ref)`, `goals(target)`, `decisions(target,status)`, `parts(plan_id)`.

**Commands:** `team attach <path> --name <n> [--check]` writes `$TEAM_STATE/targets/<n>/target.json` and creates the board. `team roles sync` creates `worker` from `_base` and each role via `profile create <role> --clone-from worker --no-alias`, then overwrites `config.yaml` (base lock + role overrides: model, `platform_toolsets.cli` from `tools`, `agent.max_turns` from budget), `SOUL.md`, and Team-owned skill dirs wholesale; other skills and `memories/` untouched; refuses a candidate that does not `catalog.resolve`; `team run` calls it each tick. `team goal <target> "<goal>" [--role] [--model] [--check]`: worktree at `$TEAM_STATE/worktrees/<target>/<slug>`, branch `team/<slug>`, kanban task with `--workspace worktree:<path> --branch --assignee <role> --max-runtime --max-retries --provider/--model` from `catalog.resolve`, `jobs` + `goals` rows, brief ends with `kanban_request_review`. `team run [--once] [--interval 60]`: `gate audit` on start, `build.py --check` and spend check each tick, dispatch, grade tasks in `review` (merge main, run check), pass → `complete` + land, fail → comment + `reopen-review`, attempt + 1 on the next untried candidate, then one Claude attempt, then `block --kind needs_input` + decision; watchdog for dead pids and over-budget runs; violation rows block the task; one line per tick on stdout. `team gate audit` runs the profile-lock assertions against the real profiles and `.env` files. `team hook stop|pretool` are the Claude Code hook commands.

**Role `implement`:** `role.toml` with `status=active`, tools `[terminal, file, todo, skills, kanban]`, `[escalation] after_failures=2 harness=claude`, `[budget] max_turns=40 run_budget_s=900`, `[models] candidates=["tier-medium", "<provider>:<id>", ...]` all resolvable. SOUL and the two skills from the tag with the "packet / inspection scope" paragraph removed. Check for target `team`: `pytest tests/team -q && ruff check team plugins/team integrations`.

**Claude escalation:** `claude_roles.generate_plugin(root, role)` → `$TEAM_STATE/claude/<role>/`; `claude_run.run(...)` with `claude -p --output-format json --max-turns --setting-sources project,local --plugin-dir --agent --allowedTools --dangerously-skip-permissions`, env from `clean_env()` plus `TEAM_CHECK`, `TEAM_ALLOWED_FILES`, `TEAM_DECISION_FILE`, `TEAM_STOP_RETRIES`, the rewritten settings file keeping Eli's deny rules and `claudeMdExcludes`. Re-check every flag against `claude --help` on the day. Records a `jobs` row (`task_ref=claude:<session>`) and a `calls` row (`provider=subscription`).

**Tests `tests/team/`:** under upstream `tests/` so its autouse HERMES_HOME isolation applies; `conftest.py` sets `TEAM_STATE=tmp_path/state`, provides `git_repo` and `FakeHermes`. Files: `test_ledger`, `test_catalog`, `test_portkey_build`, `test_grade` (real git), `test_kanban`, `test_run` (pass, fail, retry, escalate, block, watchdog, violation, spend trip), `test_profiles`, `test_profile_lock`, `test_claude_run`, `test_claude_roles`, `test_decisions`, `test_settings`, `test_cli`. Under 60 s.

**Acceptance (the first number):** `team attach ~/work/Team --name team`; `team roles sync`; `team gate audit` exit 0; `team goal team "Add team/VERSION with 0.1.0 and tests/team/test_version.py"`; `team run --once` until `team ledger` shows `outcome=pass`, `grader=check`, `model_choice` naming a catalog model, a `calls` row joined by `task_ref`, and `git log --oneline -1` is the `--no-ff` land on `main`. Then a deliberately unpassable goal shows attempt 1 fail → attempt 2 on another candidate → Claude attempt with scrubbed env → task blocked, one open decision.

---

## Phase 5: boss profile on subscription inference

- `team/subscription_gateway.py` and `team/claude_transport.py` ported from the tag unchanged in design: loopback OpenAI chat-completions adapter over `claude -p --json-schema`, text only, buffered, bearer token at `$TEAM_STATE/subscription/token` (600), fixed port 8790, per-turn limits (12 requests, 200 KB/request, stop at 120K input tokens), trace to `$TEAM_STATE/subscription/trace.jsonl`, each call also a `calls` row with `provider=subscription`. `team subscription serve` runs it; started on demand by `team boss`, launchd later.
- Profile `boss` (`team/roles/boss/`): `providers.subscription {base_url http://127.0.0.1:8790/v1, key_env SUBSCRIPTION_TOKEN, api_mode chat_completions, models [opus, sonnet]}`, `model.provider: subscription`; every `auxiliary.*` stays on LiteLLM `tier-simple`; `platform_toolsets.cli: [kanban, team]`; low compression threshold; `agent.max_turns: 40`. `.env` holds only `SUBSCRIPTION_TOKEN`. `SOUL.md` ported from the tag's `plugin/agents/boss.md`, cut to what exists.
- `team_*` tools, second half of `plugins/team/__init__.py` via `ctx.register_tool(..., toolset="team")`: `team_targets`, `team_goals`, `team_ledger`, `team_decisions`, `team_decide`, `team_plan_save`/`team_plan_show`, `team_part_create` (the only way the boss creates work), `team_read`/`team_search`/`team_tree` scoped to the target with per-plan budgets (30 files, 250 KB) in `$TEAM_STATE/planning/<plan_id>.json`. One-line tool descriptions.
- Talking to it: `team/bin/team-hermes -p boss` (the `.work.json` boss pane) or `team boss`; the dashboard Chat tab with `?profile=boss`.
- **Acceptance:** adapter on 8790, a `subscription` call row; one boss session: goal in, at most three questions, `plan.md` saved, one part created, `team run` lands it; per-turn input tokens from `trace.jsonl` recorded in `team/docs/BOSS.md` as the base rate.

---

## Phase 6: dashboard plugin and the herdr pane

- `plugins/team/dashboard/`: `manifest.json` `{"name":"team","label":"Team","icon":"Users","tab":{"path":"/team","position":"after:kanban"},"entry":"dist/index.js","css":"dist/style.css","api":"plugin_api.py"}`; `src/{index.jsx, flow.jsx, inbox.jsx, connections.jsx, style.css}`; `build.sh` (esbuild IIFE with React aliased to `window.__HERMES_PLUGIN_SDK__.React`, mirroring `plugins/kanban/dashboard/dist/index.js`); `dist/` committed. One tab, sub-views Flow / Inbox / Connections.
- `plugin_api.py` (`router = APIRouter()`): `GET /flow`, `GET /inbox`, `POST /inbox/{id}/answer|dismiss`, `GET /connections` (evidence per integration, never a bare status word: Portkey port + launchd state + last call + `build.py --check`; LiteLLM port + launchd + last `usage.db` row; catalog counts; profiles with lock-audit findings; subscription port + cached `claude auth status`; kanban DB, boards, tasks by status; ledger counts and pass `n/m · base`; self-evolution last replay or "none yet"), `GET /rates`.
- herdr dependency: the `web` pane type (Electron `WebContentsView` in the split tree, persistent session partition, prefix keys still working, `herdr pane open-url`) is a `deck` session's task; recorded in `team/docs/UI.md`.
- **Acceptance:** `/api/plugins/team/connections` returns the evidence object; the Team tab shows the Phase 4 and 5 goals in Flow; answering a decision from Inbox changes `decisions.status`; `tests/team/test_plugin_api.py` with FastAPI `TestClient`.

---

## Phase 7: self-evolution and the replay set (outline)

1. `team replay build`: freeze N finished jobs per role and target into `$TEAM_STATE/replay/<set>.json`; replays run through the normal goal/run path on a throwaway branch, graded by the same check, `jobs` rows tagged `replay:<set>`.
2. Propose: adapt `self-evolution/` to read Hermes sessions and the ledger, reflection model on a catalog tier, output = a proposed edit to a role's `SKILL.md` or `SOUL.md` on branch `team/evolve-<n>`; own venv under `$TEAM_STATE` if its pins clash.
3. Evaluate: run the set on the branch; `reviews` row with branch rate and base rate, counts shown.
4. Adopt: `team evolve accept` merges `--no-ff` only if the branch beats base; otherwise keep the branch and open a decision.
5. Only then: `models.py choose()` wired into `run.py`; `spec`/`test`/`review` activated one at a time, each with its code gate; overseer; routines via `hermes cron create … --no-agent` calling `team goal`; onboarding pass for a second target.

---

## Verification

| Phase | Proof | Lives in |
|---|---|---|
| 1 | `git log`, `git ls-files`, subtree shas | `PROVENANCE.md` |
| 2 | both launchd jobs running on 127.0.0.1; a completion through Portkey and one through LiteLLM → Portkey, seen in the console and `usage.db`; `build.py --check` both ways; refusal tests | `gateway.log`, `proxy.log`, `usage.db` |
| 3 | `team-hermes --version`, `chat -q` through Portkey, `calls` rows, hand task in `review`, 9119 listening | `team.db`, kanban DB |
| 4 | `team ledger` pass row + land sha; the escalation chain; `pytest tests/team -q` | `team.db`, `git log main` |
| 5 | 8790 listening, `subscription` call row, plan and part ids, trace token counts | `team.db`, `trace.jsonl` |
| 6 | plugin API JSON, inbox answer in DB, TestClient tests | screenshot, `team.db` |
| 7 | a `reviews` row with branch rate vs base rate | `team.db`, evolve branch |

**Definition of done for the fresh start:** `main` is the Hermes fork with Portkey, LiteLLM and self-evolution as subtrees and Team beside them, with one upstream merge and one subtree pull tried; Portkey and LiteLLM run from the subtrees under launchd, LiteLLM holds no provider keys, Portkey serves only catalog models, `team gate audit` is clean, `~/.hermes` untouched; at least one goal on target `team` landed by code with `jobs`, `calls`, `goals` rows, one escalation chain, one decision answered from the inbox; boss usable from terminal and Chat tab; Team tab shows Flow, Inbox, Connections from real data; `pytest tests/team -q` green; `team/docs/*.md` describe only what runs.

## Risks, in the order they bite

1. Portkey from source and the two patches: where the built server reads `conf.json`, what `@hono/node-server` binds by default, the exact parse point of `x-portkey-config` in `handlerUtils.ts`, and whether inline `api_key` targets behave the same on streaming. Verified at execution with real requests; recorded in `integrations/portkey/README.md`.
2. Provider slugs and NVIDIA via `custom_host`: confirm each against `portkey/src/providers/` in the subtree before writing `catalog.json`.
3. LiteLLM from source: install size and time; whether `openai/<id>` + `extra_headers` passes `x-portkey-config` and the trace headers through unchanged on streaming calls; whether a callback can set per-request forward headers. Test one streaming and one non-streaming request.
4. Upstream drift 48c0c3a → e5f2b15 in Hermes: re-grep in Phase 1 step 8.
5. Assumed Hermes config keys (`compression.threshold`, `agent.max_turns`, `${VAR}` expansion inside `extra_headers`, whether `model.extra_headers` reaches the auxiliary client): one-line greps, pinned by `test_profile_lock.py`.
6. No fail-closed API hook in Hermes: `team gate audit` on every tick greps profile configs for any provider outside the allowlist and any `*_API_KEY` in `.env`; the Portkey configs only contain catalog models.
7. Keys inside generated Portkey config files on disk (mode 600 under `$HOME/state/team/portkey/configs`, read only by the gateway process): accepted; if the open-source gateway later gains server-side integrations, move keys to `conf.json` and retire patch 1.
8. Subscription adapter fragility: attended use only, bounded per turn.
9. Three subtrees of large repos: `--squash` keeps history small; `git subtree pull` conflicts stay inside the subtree directory.
10. Free-tier quotas pause the loop; tiers fall back inside the catalog; spend guard trips on any credit movement.
11. Kanban removes clean worktrees on completion and does not enforce `--max-runtime`: `grade` re-adds from the branch; the watchdog is Team's.
12. Claude Code build-mode flags: re-check `claude --help` before Phase 4.
