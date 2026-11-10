---
name: model-gateway
description: Operate the local model gateway this Hermes runs on - LiteLLM (model and tier routing) in front of Portkey (provider failover, key vault) - read what answered, change the catalog, rebuild, restart, verify.
version: 1.0.0
author: Eli Weiss + Hermes Agent
license: MIT
platforms: [macos]
metadata:
  hermes:
    tags: [gateway, litellm, portkey, routing, models, free-tier]
    category: devops
    requires_toolsets: [terminal, file]
---

# The model gateway

Every model call this Hermes makes goes through two local proxies that run
from this checkout under launchd. You are allowed and expected to configure
them. Nothing here needs a paid key; adding one is forbidden.

```
Hermes (provider "litellm") -> LiteLLM 127.0.0.1:4000 -> Portkey 127.0.0.1:8788 -> providers
                               picks the MODEL           picks the PROVIDER for that model,
                               tiers, order, cooldowns,  holds the only copy of the keys,
                               session pin, usage.db     translates APIs, logs requests.jsonl
```

Paths below are relative to the checkout root (`REPO`), which is the
directory holding `integrations/`, `litellm/` and `portkey/`. State lives
under `$TEAM_STATE` (default `$HOME/state/team`).

| What | Where |
|---|---|
| The one source of truth | `integrations/portkey/catalog.json` (hand-edited JSON) |
| Generator | `python3 -I integrations/portkey/build.py` writes the Portkey configs and LiteLLM's `config.yaml`; `--check` reports drift and exits 1 |
| Portkey configs | `$TEAM_STATE/portkey/configs/team-model-<slug>.json`, one per model, plus `team-pair-*` probes |
| LiteLLM config | `$TEAM_STATE/litellm/config.yaml` (generated, never hand-edit) |
| Provider keys | `$TEAM_STATE/portkey/.env` (never read it aloud, never copy a value anywhere) |
| LiteLLM master key | `$TEAM_STATE/litellm/.env`, exported to Hermes as `LITELLM_MASTER_KEY` |
| Call log, LiteLLM side | `$TEAM_STATE/litellm/usage.db`, table `calls` |
| Call log, Portkey side | `$TEAM_STATE/portkey/requests.jsonl`, one line per request with every attempt |
| Service logs | `$TEAM_STATE/litellm/proxy.log`, `$TEAM_STATE/portkey/gateway.log` |
| Status | `sh litellm/team/status.sh` and `sh integrations/portkey/status.sh` (exit 1 on any failure) |
| launchd jobs | `com.fullmoney.team-litellm`, `com.fullmoney.team-portkey`, `com.fullmoney.team-subscription` (Claude bridge on 8790) |

## How routing works

**Tiers.** LiteLLM model groups `tier-simple`, `tier-medium`, `tier-complex`,
`tier-reasoning`, `tier-claude`. Each catalog model row with a `tier` and an
`order` is one deployment in that group. LiteLLM tries order 1 first and moves
down only while lower orders are cooling down or failing. `tier_fallbacks` in
the catalog says which tier a request moves to when a whole tier fails.
Hermes profiles name a tier (or a catalog model id) as their model, so a
"model swap" is either LiteLLM moving to the next deployment in the tier, or a
tier fallback. Both are visible in the logs below.

**Providers.** Each model's `served_by` lists the providers that serve it, in
Portkey failover order. `policy.portkey.on_status_codes` lists the statuses
that move Portkey to the next provider. `policy.litellm` holds the router
settings (retries, cooldown, allowed fails).

**Free only.** `build.py` refuses a `served_by` whose `free` is not `yes` or
`allowance`, a blocked or unknown provider, and an OpenRouter id that is not a
`:free` variant. Claude is served by `claude-subscription`, a local bridge to
Eli's Claude Max plan on 8790, never by an Anthropic key.

## Read what actually answered

Which model answered the last 20 calls, and whether it was what was asked for:

```sh
sqlite3 -header -column "$TEAM_STATE/litellm/usage.db" \
  "select ts, \"group\", deployment, response_model, ok, rate_limited, latency_ms, error
   from calls order by id desc limit 20"
```

Swaps inside a tier show as `deployment` differing from the tier's order-1
model. Tier fallbacks show as `routed_group` differing from `group`. Rate limits
are `rate_limited = 1`.

The provider side, with every attempt Portkey made for a request:

```sh
tail -20 "$TEAM_STATE/portkey/requests.jsonl" | python3 -c '
import json,sys
for l in sys.stdin:
    r=json.loads(l); print(r["time"], r["status"], r["config"], r.get("provider"), r.get("provider_model"),
      [(a.get("provider"),a.get("status"),a.get("latency_ms")) for a in r.get("attempts",[])])'
```

Failure rates per model over the last day, next to the base rate of all calls:

```sh
sqlite3 -header -column "$TEAM_STATE/litellm/usage.db" \
  "select model, count(*) n, sum(ok=0) failed, round(100.0*sum(ok=0)/count(*),1) fail_pct
   from calls where ts > datetime('now','-1 day') group by model order by n desc"
sqlite3 "$TEAM_STATE/litellm/usage.db" \
  "select count(*), sum(ok=0) from calls where ts > datetime('now','-1 day')"
```

Always report a rate with its count and next to the base rate.

## Change the routing

1. Edit `integrations/portkey/catalog.json`. Typical edits: move a model's
   `order` within its tier, change a model's `tier`, reorder a `served_by`
   list, set `tier: null` with an `unrouted` reason to take a model out, add a
   model row (id, context, tools, vision, reasoning, `served_by` with
   `provider`, `provider_model_id`, `free` and the evidence for free).
   Only providers already in `providers` may be used. Never add a provider
   that bills.
2. `python3 -I integrations/portkey/build.py` then
   `python3 -I integrations/portkey/build.py --check`. A refusal names the
   rule; fix the catalog, do not work around the rule.
3. Restart LiteLLM so it reloads `config.yaml`:
   `launchctl kickstart -k gui/$(id -u)/com.fullmoney.team-litellm`.
   Portkey reads configs by name on each request and needs no restart; a
   change to `policy.portkey` or to `portkey/src` does:
   `launchctl kickstart -k gui/$(id -u)/com.fullmoney.team-portkey`.
4. Verify: both status scripts exit 0, then one real call per changed tier:
   ```sh
   curl -s -m 90 http://127.0.0.1:4000/v1/chat/completions \
     -H "Authorization: Bearer $LITELLM_MASTER_KEY" -H 'Content-Type: application/json' \
     -d '{"model":"tier-medium","messages":[{"role":"user","content":"Reply with the word ok"}],"max_tokens":50}'
   ```
   then read the new row in `usage.db` and the new line in `requests.jsonl`
   to confirm which model and provider answered.
5. `integrations/bin/check` runs the catalog check, the gateway tests and the
   Portkey patch tests. Commit the catalog change with a message that says
   what moved and the evidence (the log lines) for why.

## Probe a provider or model before routing it

Portkey `team-pair-<model>--<provider>.json` configs exist for models that are
listed but not yet routed. Call Portkey directly with one:

```sh
curl -s -m 90 http://127.0.0.1:8788/v1/chat/completions \
  -H 'x-portkey-config: team-pair-<model-slug>--<provider>' -H 'Content-Type: application/json' \
  -d '{"model":"<provider_model_id>","messages":[{"role":"user","content":"Reply with the word ok"}],"max_tokens":20}'
```

A 2xx with a sensible answer is the evidence to put in the catalog row; a
`402`, `404` or `410` is the evidence for `unavailable`.

## Profiles and the locked base layer

Hermes runs one profile per project. The Team repo itself uses the `default`
profile; any other project gets its own profile, cloned from `default` the
first time Hermes is launched there (`integrations/bin/hermes-project` picks
the name). The gateway connection is locked for every profile by
`integrations/hermes/base/config.yaml` in the Team repo (Hermes's managed
layer): `hermes config set` refuses those keys. To change them for every
project, edit that file in the Team repo and commit it. Everything else, such
as the model per tier, toolsets and personality, is each profile's own.
Skills in the `default` profile are shared live and read-only with every
project; a skill a project learns stays in that project's profile.

## Hermes's own side

The Hermes provider is `litellm` in `$HERMES_HOME/config.yaml`
(`base_url http://127.0.0.1:4000/v1`, the master key from the environment, the
four tiers in `models`, `x-litellm-session-id` as the affinity header so one
conversation stays on one deployment). Every auxiliary lane in that file is
pinned to a tier through the same provider. To put a profile on a specific
model, name a catalog id (for example `gemini-3.6-flash`) instead of a tier;
every catalog model with a tier is also its own LiteLLM model group.

## Rules

- Keys never appear in a message, a log you quote, or a file in the repo.
- Never edit `$TEAM_STATE/litellm/config.yaml` or a Portkey config by hand;
  `--check` will refuse to start the proxies on drift.
- A number comes from a command or a log line, never from memory.
- Do not add an Anthropic key, MiniMax or DashScope: blocked in the catalog
  and in the generator for a reason recorded there.
