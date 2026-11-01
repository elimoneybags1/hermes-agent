# Hand-off: the LiteLLM + Portkey connection (Phase 2 of PLAN.md)

Written 2026-10-27 by the Team session for a second Claude Code session. Eli is
splitting the work: this session continues Hermes, roles and the Team layer;
the gateway session builds Phase 2 and, after it is proven, rewrites the
Team-owned connection layer in Rust.

## What you are building

Read `PLAN.md` first: "How each piece works" and all of "Phase 2". The short
version:

```
Hermes profiles ──┐
other tools ──────┼──► LiteLLM :4000 ──► Portkey :8787 ──► providers
                  │    chooses the MODEL   chooses the PROVIDER for it,
                  │    tiers, cooldowns,   holds the only copy of the keys,
                  │    session pin,        translates APIs, circuit breaker,
                  │    usage.db, callbacks durable request log, console
```

Eli's intent, in his words: the connection between these two "will be dope".
It is the heart of model routing for everything on this machine, tuned in
LiteLLM's router settings, with Portkey as the provider layer and key vault.
It is not a thin forward and not a stopgap.

## Ground rules

- Free-only, fail closed. `catalog.json` is the only source of what models
  exist; `build.py` refuses anything not free; no paid provider key anywhere
  (`anthropic`, `minimax`, `dashscope` are blocked by name). Never a paid API
  key in any environment you start.
- Keys live in exactly one place: `$HOME/state/team/portkey/.env` (provider
  keys) and `$HOME/state/team/litellm/.env` (the LiteLLM master key). Nothing
  under the repo holds a key. Generated files are mode 600.
- Both proxies run from source in this repo, under launchd, on 127.0.0.1 only.
- Upstream files are never edited in place. The two Portkey patches are the one
  exception: marked `[team]` commits inside `portkey/`, listed in
  `PROVENANCE.md` with file and line. Nothing else in `portkey/` or `litellm/`
  changes.
- Real numbers only; a rate next to its base rate; verify from processes and
  logs, never from a markdown file; `$HOME`-derived paths, never a user name;
  no `cd` in shell commands (`git -C`, `npm --prefix`); commit as you go;
  never push without asking.
- This repo is also being worked on by the Team session. You own:
  `portkey/`, `litellm/`, `integrations/portkey/`, `litellm/team/`,
  `tests/team/test_portkey_build.py`, `$HOME/state/team/{portkey,litellm}`.
  Do not touch anything else; `git add` only your paths; both sessions commit
  on `main` in this working tree.

## Starting state (verified 2026-10-27)

- `main` = Hermes upstream `e5ea5fd9` merged at the root (`8ee29e6d`).
  Hermes owns a top-level `gateway/` (its messaging gateway), so the Portkey
  subtree prefix is `portkey/`, not `gateway/`.
- Subtrees not yet added. Pins: Portkey `v1.15.2` (full history, no squash),
  LiteLLM `v1.106.4` (`--squash`). `git ls-remote` both before adding.
- Old Team state is at `~/state/team-old/` (read-only reference; its
  `litellm/.env` has the provider keys to copy into the new Portkey `.env`).
  The archived LiteLLM setup is `git show before-fresh-start:litellm/<file>`:
  `free-models.json` (59 entries, 8 providers, researched 2026-10-26),
  `build.py` (the refusal rules), `session_pin.py`, `usage_log.py`,
  `spend_guard.py`, `run.sh`, `install-launchd.sh`.
- Machine: Node 26.7, npm 11.19, uv 0.12.5, Python 3.11 via uv, Docker
  running, ports 4000 and 8787 free, no Team launchd jobs loaded.
- Open-source Portkey facts already checked: `x-portkey-config` inline JSON
  schema (strategy, targets, provider, api_key, custom_host, override_params,
  retry, cb_config); provider slugs for openrouter, groq, gemini, mistral-ai,
  cohere, cerebras, z-ai; NVIDIA via `provider: openai` + `custom_host`;
  `conf.json` integrations have no consumer in the OSS runtime (only
  `admin_token`); console at `/public/`; no durable log; no health route;
  port by `--port=`, no host flag. Re-verify against the subtree.

## Order

1. Subtrees + `PROVENANCE.md` entries for both. Commit.
2. Portkey from source: build, run on 8787, console login. Patch 1
   (config by name from `$PORTKEY_CONFIG_DIR`), patch 2 (durable
   `requests.jsonl`), each with a jest test. Commit each separately.
3. `integrations/portkey/`: `catalog.json` (seeded from the archive, with
   `served_by` per model), `build.py` with its refusals and `--check`,
   `run.sh`, `install-launchd.sh`, `status.sh`, `README.md` with what you
   verified and when.
4. `litellm/team/`: own venv from the subtree, generated
   `config.yaml` pointing every deployment at Portkey by config name,
   `session_pin.py`, `usage_log.py`, trace-id forwarding, `run.sh`,
   `install-launchd.sh`, `status.sh`.
5. The acceptance list in `PLAN.md` Phase 2, every line, with the evidence
   pasted into `integrations/portkey/README.md`.
6. Then the Rust rewrite of the Team-owned connection layer: catalog
   generator, config-by-name resolver, request log, trace join, status
   checks, as one binary under `integrations/gateway-rs/` (own Cargo
   workspace), same catalog in, byte-identical files out, same acceptance
   list. Keep the Python and TS versions until the Rust one passes it.

## Report back

When Phase 2's acceptance list passes, write the evidence into
`integrations/portkey/README.md`, commit, and tell Eli. The Team session's
Phase 3 (Hermes profiles on `tier-*` names through LiteLLM) starts from that.
