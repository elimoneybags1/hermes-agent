# Team's LiteLLM

This is BerriAI/litellm, pinned at v1.106.4, with one folder added: `team/`.
Nothing of LiteLLM's own code is changed. LiteLLM is the front door of Team's
model gateway; every call it makes goes to Portkey, never to a provider.

## The handshake with Portkey

LiteLLM reaches Portkey by **config name**, so LiteLLM never holds a provider
key. Its `config.yaml` is generated from the model catalog
(`integrations/portkey/catalog.json` in the Team repository, by `build.py`):
every deployment has

```yaml
api_base: http://127.0.0.1:8788/v1
extra_headers:
  x-portkey-config: team-model-<model>
```

Portkey's side resolves that name to `$PORTKEY_CONFIG_DIR/<name>.json`
(`src/team/configByName.ts` in Team's Portkey fork) and refuses any request
without one. The same session id crosses as the trace id, so LiteLLM's
`usage.db`, Portkey's `requests.jsonl` and Team's ledger line up on one id.

## What each file in `team/` does

| File | Does |
|---|---|
| `run.sh` | starts the proxy from its own venv, loopback only; reads host and port from the catalog; scrubs every provider key from the environment so LiteLLM can only call Portkey; refuses a `config.yaml` the catalog does not generate |
| `forward_headers.py` | the handshake header per request: `x-portkey-trace-id` = the caller's `x-litellm-session-id`, plus `x-portkey-metadata` with session, caller and group |
| `session_pin.py` | keeps a session on the model that last answered it within its tier, so a job does not switch model on every call |
| `spend_guard.py` | drops any deployment whose provider is paid out of an included credit once that credit's stop is reached; the group then fails over to a free tier |
| `usage_log.py` | one row per attempt in `$HOME/state/team/litellm/usage.db`, failures included, keyed by the same session id |
| `install-venv.sh` | builds the venv from this tree |
| `install-launchd.sh` | installs the proxy as the always-on launchd agent `com.fullmoney.team-litellm` |
| `status.sh` | process, port, config drift and last call, in one screen |

`config.yaml` loads the four Python files as LiteLLM callbacks by module name;
`run.sh` starts in `team/` so they import without a package install.

## Where the rest is

The catalog, the generator, Portkey's configs and the Rust connection layer
are in Team's repository, `elimoneybags1/hermes-agent`, under `integrations/`.
This tree is its `litellm/` subtree. `PROVENANCE.md` there records how both
are updated.
