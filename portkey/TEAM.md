# Team's Portkey

This is Portkey-AI/gateway, pinned at v1.15.2, with Team's patches on top.
Each is a commit marked `[team]`; the files and lines are listed in
`PROVENANCE.md` in Team's repository. Portkey is the back half of Team's model
gateway: LiteLLM in front sends it every call, and it holds the only copy of
every provider key.

## The handshake with LiteLLM

LiteLLM names a **config** instead of carrying a key:

```
x-portkey-config: team-model-<model>
x-portkey-trace-id: <the caller's session id>
```

| File | Does |
|---|---|
| `src/team/configByName.ts` | resolves `x-portkey-config: <name>` to `$PORTKEY_CONFIG_DIR/<name>.json` before the validator sees the request, so callers never carry provider keys; an unknown name, a missing header or an inline JSON config is a 400, so the gateway only calls what the catalog generated |
| `src/team/requestLog.ts` | when `$PORTKEY_REQUEST_LOG` is set, one JSON line per `/v1` request: config name, trace id, metadata, status, latency, the provider and model that answered, tokens, one entry per attempt; never the prompt or the answer |
| `src/index.ts` | two lines: mount the resolver on `/v1/*` ahead of the first route |
| `src/middlewares/log/index.ts` | one call after `next()`: append the request log line |
| `src/start-server.ts` | `--host=` so the server binds loopback only instead of every interface |

Provider fixes that the free tiers needed, also `[team]`:

| File | Does |
|---|---|
| `src/providers/google/chatComplete.ts` | Gemini tool round trip: tool results as `user` turns with the real tool name, thought signatures carried back, completion tokens include thinking tokens |
| `src/providers/google-vertex-ai/chatComplete.ts` | completion tokens include thinking tokens |
| `src/providers/cohere/chatComplete.ts` | `tool_calls` and `tool_call_id` survive the message transform |
| `src/providers/toolRoundTrip.test.ts` | tests for the three above |

Run the Team tests with
`npx jest --config jest.config.js src/team src/providers/toolRoundTrip`.

## Where the rest is

The configs this resolves are generated from the catalog
(`integrations/portkey/catalog.json`) by `build.py`, both in Team's repository
`elimoneybags1/hermes-agent`, which also holds LiteLLM's side
(`litellm/team/`) and the Rust connection layer. This tree is its `portkey/`
subtree.
