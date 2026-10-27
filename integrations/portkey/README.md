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
   `Unknown config name: …`. The same commit adds `--host=` to the node
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
- Fallback (`tryTargetsRecursively`): moves to the next target only when the response status is in `strategy.on_status_codes`. A target timeout is 408, a dead key 401/403, an exhausted trial 402, a model the provider dropped 404, so the generated list is `[401, 402, 403, 404, 408, 429, 500, 502, 503, 504, 529]`. In-target `retry` is `attempts 1` on `[500, 502, 503, 504]` only.
- Provider slugs in `src/providers/index.ts`: `openrouter`, `groq`, `google` (Gemini API, `generativelanguage.googleapis.com`), `mistral-ai`, `cohere`, `cerebras`, `z-ai` (`api.z.ai/api/paas/v4`); NVIDIA is `openai` + `custom_host: https://integrate.api.nvidia.com/v1`.
- Headers: `x-portkey-trace-id` is echoed back on the response; `x-portkey-metadata` is JSON and is read by the conditional router and by the request log.
- Port: PLAN.md says 8787, but `127.0.0.1:8787` is held by `com.fullmoney.myprof-gateway` (another project's node gateway, launchd, running since 2026-10-09). The catalog says **8788**; nothing else hardcodes it.

## Acceptance evidence

(filled in below as each line of the PLAN.md Phase 2 list passes)
