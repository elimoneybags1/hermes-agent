# Portkey gateway and LiteLLM, from source, free-only

The connection at the heart of Team's model routing (PLAN.md Phase 2):

```
callers ──► LiteLLM 127.0.0.1:4000 ──► Portkey 127.0.0.1:8788 ──► providers
            chooses the MODEL           chooses the PROVIDER for that model,
            (tiers, order, cooldowns,    holds the only copy of the keys,
            session pin, usage.db)       translates APIs, logs requests.jsonl
```

Everything here derives from one hand-edited file, `catalog.json`. `build.py`
turns it into the Portkey configs and LiteLLM's `config.yaml`; `--check`
refuses to let either process start on drift. Keys live only in
`$HOME/state/team/portkey/.env` (provider keys) and
`$HOME/state/team/litellm/.env` (the LiteLLM master key).

## Files

| File | What it is |
|---|---|
| `catalog.json` | the one source of truth: ports, blocked providers, providers (Portkey slug, key env, free basis, limits), tiers, models with `served_by` |
| `build.py` | catalog → `$HOME/state/team/portkey/configs/team-model-<slug>.json`, `team-tier-<t>.json`, `$HOME/state/team/litellm/config.yaml`; `--check` diffs disk against the catalog; refuses non-free, blocked and unknown providers, non-`:free` OpenRouter ids, oversized configs, empty tiers, routed models with no live provider, duplicate ids, missing keys |
| `run.sh` | `build.py --check`, rebuild `portkey/build/` when `portkey/src/` is newer, then `node portkey/build/start-server.js --port=<catalog> --host=127.0.0.1` with `PORTKEY_CONFIG_DIR` and `PORTKEY_REQUEST_LOG` set and every paid-key variable unset |
| `install-launchd.sh` | `com.fullmoney.team-portkey`, KeepAlive, log at `$HOME/state/team/portkey/gateway.log` |
| `status.sh` | launchd state, listening socket (must be loopback), `build.py --check`, `GET /`, last line of `requests.jsonl`; exit 1 on any failure |
| `../litellm/` | `run.sh`, `install-venv.sh`, `install-launchd.sh`, `status.sh`, and the callbacks `forward_headers.py`, `session_pin.py`, `usage_log.py` |
| `../../tests/team/test_portkey_build.py` | 12 unit tests for the refusals and determinism |

State, outside the repo: `$HOME/state/team/portkey/{.env, configs/, requests.jsonl, gateway.log}`
and `$HOME/state/team/litellm/{.env, config.yaml, usage.db, proxy.log}`, venv at
`$HOME/state/team/litellm-venv`. Generated files are mode 600.

## The two gateway patches (`[team]` commits in `portkey/`, listed in PROVENANCE.md)

1. **Config by name** (`a87204e7`): `x-portkey-config: team-model-gemma-4-31b`
   is resolved by a middleware on `/v1/*` to the contents of
   `$PORTKEY_CONFIG_DIR/team-model-gemma-4-31b.json` before Portkey's own
   validator and handlers see it. Cached by mtime. Unknown name → 400
   `Unknown config name: …`. Fail closed (2026-10-29): a `/v1` request with
   no `x-portkey-config` (a caller's own `x-portkey-provider` and key) or with
   an inline JSON config is also a 400, so Portkey only ever runs configs that
   `build.py` generated. The same commit adds `--host=` to the node
   server: without it `@hono/node-server` listens on every interface
   (`lsof` showed `*:8788`; with `--host=127.0.0.1` it shows `127.0.0.1:8788`).
2. **Durable request log** (`02cd4031`): one JSON line per `/v1` request to
   `$PORTKEY_REQUEST_LOG`: time, config, trace id, metadata, status, latency,
   provider and model that answered, prompt and completion tokens, and one
   entry per attempt (provider, model, status, latency). Never prompt or
   answer text.

Tests: `npx --prefix portkey jest --config portkey/jest.config.js --rootDir portkey src/team`
(10 tests). `npm --prefix portkey run test:gateway` runs `jest src/`, where 8
upstream suites already fail at v1.15.2 (API keys, and `src/utils/env.ts` uses
top-level `await`, which ts-jest's CommonJS transform rejects); the two `src/team`
suites pass inside that run.

## Facts re-verified against the subtree (v1.15.2 = `ca771294`, 2026-10-27)

- Build: `npm ci` (lockfile) then `npm run build` = `rollup -c` → `build/start-server.js` (482 KB) + `build/public/index.html`, 2.5 s.
- Start: `node build/start-server.js --port=N`; the console at `/public/` is served only when `NODE_ENV` is not `production` (`run.sh` sets `development`), has **no admin token** (`grep -rn admin_token src initializeSettings.ts` finds nothing), and streams the live log over SSE at `/log/stream`. No health route; `GET /` answers `AI Gateway says hey!`.
- `conf.json`: `src/index.ts` imports the repo's `conf.json` at build time (`plugins_enabled`, `cache`); `initializeSettings.ts` reads `./conf.json` only when `FETCH_SETTINGS_FROM_FILE=true` and only for `integrations`, which nothing else in the OSS runtime consumes. So PLAN.md's generated `conf.json` with an admin token has no consumer and is **not generated**.
- `cb_config`: preserved through `convertKeysToCamelCase` and passed to `c.get('handleCircuitBreakerResponse')`, which nothing in the OSS runtime sets. **Not emitted.**
- Config schema (`src/middlewares/requestValidator/schema/config.ts`, zod, non-strict so unknown keys such as the `_` comment are dropped): `strategy{mode, on_status_codes}`, `targets[]` nested recursively, `provider`, `api_key`, `custom_host`, `override_params`, `retry{attempts, on_status_codes}`, `request_timeout`, `forward_headers`.
- Fallback (`tryTargetsRecursively`): moves to the next target only when the response status is in `strategy.on_status_codes`. A target timeout is 408, a dead key 401/403, an exhausted trial 402, a model the provider dropped 404, so the default list is `[401, 402, 403, 404, 408, 429, 500, 502, 503, 504, 529]`. In-target `retry` defaults to `attempts 1` on `[500, 502, 503, 504]` only. Both, the per-model `mode` (`fallback` or `loadbalance` by `served_by[].weight`), the default timeout and every LiteLLM router value are in `catalog.json` `policy`, checked by `build.py` (2026-10-29). `gateway-rs build` still hard-codes the old defaults: it matches `build.py` only while `policy` holds them.
- Provider slugs in `src/providers/index.ts`: `openrouter`, `groq`, `google` (Gemini API, `generativelanguage.googleapis.com`), `mistral-ai`, `cohere`, `cerebras`, `z-ai` (`api.z.ai/api/paas/v4`); NVIDIA is `openai` + `custom_host: https://integrate.api.nvidia.com/v1`.
- Headers: `x-portkey-trace-id` is echoed back on the response; `x-portkey-metadata` is JSON and is read by the conditional router and by the request log.
- Port: PLAN.md says 8787, but `127.0.0.1:8787` is held by `com.fullmoney.myprof-gateway` (another project's node gateway, launchd, running since 2026-10-09). The catalog says **8788**; nothing else hardcodes it.

## Acceptance evidence (PLAN.md Phase 2, all run on 2026-10-27, times are EDT)

Every number below came from the command shown, not from memory. Secrets are
elided as `<key>`.

**1. Both launchd jobs running, both sockets on 127.0.0.1** (`status.sh` for each, then `lsof`, 12:46):

```
launchd  com.fullmoney.team-portkey: running pid 89004
listen   127.0.0.1:8788
configs  build.py --check: 33 files match catalog.json
http     GET / ok
requests 125 lines; last: 2026-10-27T16:44:48.301Z 200 team-model-nemotron-3-5-lightning-30b openai nvidia/nemotron-3.5-lightning-30b-a3b trace gwk_846f7f59b65be8cecf956020
portkey status rc=0
launchd  com.fullmoney.team-litellm: running pid 8037
listen   127.0.0.1:4000
configs  build.py --check: 33 files match catalog.json
http     /health/liveliness ok
usage.db 9 calls; last: 2026-10-27T16:44:48+00:00|0|||e2a7b31d-8c6b-44be-a073-be21ff17a1e7|0ms
litellm status rc=0
python3.1 8037 127.0.0.1:4000
node 89004 127.0.0.1:8788
```

**2. One chat completion to Portkey by config name.** `curl … -H 'x-portkey-config: team-tier-simple' -H 'x-portkey-trace-id: pk-smoke-3'` →
`200`, body `"model":"gemini-3.5-flash-lite","provider":"google"`, content `pong`, 32.0 s (an earlier identical call, `pk-smoke-2`, took 6.4 s; Gemini's free tier was slow all afternoon). The line `requests.jsonl` gained:

```
{"time":"2026-10-27T16:24:57.081Z","config":"team-tier-simple","trace_id":"pk-smoke-3","metadata":null,"method":"POST","path":"/v1/chat/completions","status":200,"latency_ms":31994,"provider":"google","model_requested":"tier-simple","provider_model":"gemini-3.5-flash-lite","model_answered":"gemini-3.5-flash-lite","prompt_tokens":8,"completion_tokens":1,"stream":false,"attempts":[{"provider":"google","model":"gemini-3.5-flash-lite","status":200,"latency_ms":31971}]}
```

An unknown name is refused before any provider is called: `-H 'x-portkey-config: team-model-nope'` → `400 {"status":"failure","message":"Unknown config name: team-model-nope"}`, and that too is a line in `requests.jsonl` (`"status":400,"attempts":[],"error":"Unknown config name: team-model-nope"`).

**3. One chat completion to LiteLLM, master key, `x-litellm-session-id: smoke-1`** (`model: tier-simple`, 12:38) → `200` in 1.48 s; LiteLLM chose `nemotron-3.5-lightning-30b`, Portkey chose NVIDIA. The same call, under the same id, in all three places:

- `usage.db`: `1|2026-10-27T16:38:19+00:00|smoke-1|tier-simple|simple|tier-simple/nemotron-3-5-lightning-30b|openai/nemotron-3.5-lightning-30b|24|20|1275|1|` (id, ts, session, group, tier, deployment, model, tokens in, tokens out, latency ms, ok)
- `requests.jsonl`: `"config":"team-model-nemotron-3-5-lightning-30b","trace_id":"smoke-1","metadata":{"session":"smoke-1","caller":"litellm","group":"tier-simple"},"status":200,"latency_ms":775,"provider":"openai","provider_model":"nvidia/nemotron-3.5-lightning-30b-a3b","prompt_tokens":24,"completion_tokens":20`
- console: a `curl -N http://127.0.0.1:8788/log/stream` opened before the call received exactly one `event: log` whose data names `"provider":"openai"` and `"model":"nvidia/nemotron-3.5-lightning-30b-a3b"` (the console stream also carries the target's api key in clear, upstream behaviour; it is loopback-only).

The trace id travels by `integrations/litellm/forward_headers.py`: a `CustomLogger.async_filter_deployments` hook hands LiteLLM per-request copies of the candidate deployments with `x-portkey-trace-id` and `x-portkey-metadata` merged into their `extra_headers` (a request-level `extra_headers` would replace the deployment's `x-portkey-config`).

**4. Kill one provider key, rebuild.** `NVIDIA_API_KEY=nvapi-broken-on-purpose` in Portkey's `.env`, `build.py` (Portkey re-reads configs by mtime, no restart):

- Portkey failover, a model served by two providers: `x-portkey-config: team-model-gpt-oss-20b` → `200` in 0.60 s, answered by `groq openai/gpt-oss-20b` (`pong`); the log line: `attempts:[('openai', 403, 103 ms), ('groq', 200, 490 ms)]`. Before 401/402/403/404 were added to `on_status_codes`, the same call stopped at NVIDIA's 403 (`attempts:[('openai', 403)]`, 0.40 s): Portkey falls over only on listed statuses.
- A model served by three: `team-model-gemma-4-31b` → `attempts:[('openai', 403, 185 ms), ('openrouter', 429, 381 ms), ('google', 500, 8009 ms)]` → `500` after every provider failed (OpenRouter: `poolside/laguna-s-2.1:free is temporarily rate-limited upstream … limit_source: upstream_provider_shared_pool`; Gemini: `Internal error encountered`).
- LiteLLM fallback, a tier whose first model is unavailable: `model: tier-reasoning`, `x-litellm-session-id: fallback-1` → `200` in 127.6 s, answered by `nvidia/nemotron-3-ultra-550b-a55b:free` (`pong`). `requests.jsonl` for `fallback-1`, in order: `team-model-glm-5-3` openai 403 (twice: LiteLLM's one retry), `team-model-gemini-3-8-flash` google 408 at 60 s (twice), `team-model-nemotron-3-ultra-550b` `[('openai', 403), ('openrouter', 200)]`. `usage.db` rows for `fallback-1`: `tier-reasoning/glm-5-3 ok=0 "Error code: 403"` and `tier-reasoning/nemotron-3-ultra-550b ok=1 5626 ms`. (LiteLLM wrote no row for the two Gemini 408s; Portkey's log is the complete record of attempts, which is why the join exists. The Gemini target timeout was raised to 120 s afterwards.)
- Key restored, `build.py`, `build.py --check: 33 files match catalog.json`.

**5. `build.py --check` and drift.** Clean: `build.py --check: 33 files match catalog.json`. After `printf '\n' >> team-model-command-a-plus.json`: both `integrations/portkey/run.sh` and `integrations/litellm/run.sh` printed `build.py: refusing: outputs are not what catalog.json generates: differs portkey/configs/team-model-command-a-plus.json; run build.py` and exited 1 before starting anything. `build.py` rewrote it; `--check` clean again. A stale `team-*.json` is likewise refused (`stale …`) and removed by the next build (unit-tested).

**6. Unit tests.** `tests/team/test_portkey_build.py`: 12 passed in 0.06 s (`free: no`, blocked provider, unknown provider, non-`:free` OpenRouter id, oversized config, empty tier, routed model with no live provider, missing key, duplicate id; determinism and key placement; `--check` after a build, after a hand edit, after a stale file). Run with
`TEAM_STATE=<tmp> uv run --no-project --python 3.11 --with pytest -m pytest tests/team/test_portkey_build.py -q --noconftest -c /dev/null`
until the Team venv exists (Hermes's `tests/conftest.py` imports `ruamel`). Gateway patch tests: `npx --prefix portkey jest --config portkey/jest.config.js --rootDir portkey src/team` → 2 suites, 10 passed, 0.97 s.

**7. Keys.** `grep -rl API_KEY $HOME/state/team/litellm/` → nothing (LiteLLM's `.env` holds one line, `LITELLM_MASTER_KEY=…`; its `config.yaml` says `api_key: team` and `os.environ/LITELLM_MASTER_KEY`). `grep -rl -E 'nvapi-|sk-or-|AIza|gsk_' $HOME/state/team/litellm/ $HOME/state/team/portkey/requests.jsonl` → nothing. Provider keys exist in `$HOME/state/team/portkey/.env` and the generated `configs/*.json` (mode 600) only. The gateway process's environment (`ps eww`) holds `HOME LOGNAME NODE_ENV PATH PORTKEY_CONFIG_DIR PORTKEY_REQUEST_LOG PWD SHELL SHLVL SSH_AUTH_SOCK TMPDIR USER XPC_FLAGS XPC_SERVICE_NAME`, no key at all.

**Also found and fixed on the way:** LiteLLM 1.106.4 sweeps every deployment's `/v1/models` on start and on a timer (`ProxyStartupEvent.refresh_model_info`), which Portkey forwarded to every provider: 109 such calls in `requests.jsonl` (37 × 200 openai/NVIDIA, 32 × 404 google, 16 × 401 mistral, 8 × 401 z-ai, 8 × 401 cohere, 4 × 401 groq, 4 × 404 openrouter) before `general_settings.disable_model_info_refresh: true` went into the generated config; a restart after that added none.

## The Rust rewrite (`integrations/gateway-rs/`)

`gateway-rs build [--check]` is the generator in Rust (toolchain pinned to
1.99.1 in `rust-toolchain.toml`, one dependency, `serde_json` with
`preserve_order`). Against the 33 files `build.py` wrote,
`gateway-rs build --check` reports `33 files match catalog.json`: byte-identical.
