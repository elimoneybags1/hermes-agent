# Claude subscription behind Portkey

```
Hermes → LiteLLM :4000 → Portkey :8788 → subscription bridge :8790 → Claude CLI
```

The bridge uses the reviewed DirectSDK transport pinned in `setup.py`. Its
upstream source is [Hermes Official Plugins](https://github.com/NousResearch/hermes-official-plugins/tree/20048e6e2c1c48d6023e73ae6951595f58352b20/claude-subscription-directsdk).
The dependency is downloaded into state, not vendored into Hermes core.

The caller supplies messages and tool schemas on each request. The native
Claude tool inventory, skill discovery, settings sources, auto-memory and
CLAUDE.md loading are disabled by the transport. Hermes executes tool calls;
the native MCP callbacks are inert. Some native protocol annotations remain:
this is not a byte-identical raw Anthropic API request or a zero-overhead claim.
Keep stable system instructions and tool inventories within a conversation
to preserve caching. Adding tools or rewriting old context can invalidate it.

`catalog.json` now lists `claude-subscription` as the Claude provider. It uses
an OpenAI-compatible local endpoint and `SUBSCRIPTION_TOKEN`, a random local
gateway credential. No Anthropic API key is needed. There is no paid-API
fallback and no monthly API-credit spend guard on this route. Native plan
limits and account extra-usage settings still apply. Catalog prices are
comparison data, not actual subscription charges.

## Provision and start

Run from the Team checkout. Choose the same state directory as the proxy
stack. Setup preserves existing provider keys and adds the local token.

```sh
python3 -m integrations.subscription.setup --state-dir "${TEAM_STATE:-$HOME/state/team}"
claude auth status
scripts/run-in-hermes-env python3 -m integrations.subscription.server \
  --plugin-dir "${TEAM_STATE:-$HOME/state/team}/subscription/20048e6e2c1c48d6023e73ae6951595f58352b20" \
  --token-file "${TEAM_STATE:-$HOME/state/team}/subscription/token"
```

Use the CLI's subscription login. The transport rejects inherited API-key,
custom Anthropic endpoint and cloud-backend overrides; it never reads tokens
from Claude's credential files itself. Do not pass `Client(env=...)`, which
is an upstream test-only escape from that guard.

With the bridge running, regenerate the proxy configuration with
`python3 integrations/portkey/build.py`, check it with `--check`, and restart
LiteLLM using the stack's existing supervisor. Portkey reloads named configs.
All four Claude model IDs are explicit LiteLLM model groups; `tier-claude`
is also available and has no cross-tier fallback. Other tiers retain their
existing routes.

The implement role now uses `runs_on.harness = "hermes-subscription"` with
`runs_on.model = "claude-opus-5-5"`. Run `team/bin/team roles sync implement`
after setup. Sync installs a small `team-subscription` provider profile into
that role's isolated home. It sends requests to LiteLLM and promotes native
replay metadata from LiteLLM's `provider_specific_fields`, allowing Hermes to
retain it across tool rounds. Using the ordinary `litellm` profile would lose
this metadata. Subscription roles expose exactly the toolsets in their role
manifest, with no automatic learning-tool additions, and never escalate to
the separate full Claude Code worker. Auxiliary calls retain the base free
routes. No running profiles or services are changed merely by editing the
role manifest.

## Verification limits

Tests cover the local HTTP boundary's tool/history fidelity, replay-field
preservation, cache usage, streaming cleanup, the generated two-proxy route,
and Hermes replay sanitization. A synthetic tool round trip through temporary
instances of the actual LiteLLM and Portkey servers also preserved tool
schemas, tool results and native replay metadata.
An actual Max-login Opus 5.5 probe through both temporary proxies completed
one tool call and its result, with one upstream request per call. Input usage
was 750 tokens on the first call and 793 on the second, including the supplied
prompt and single tool. This demonstrates that the full Claude Code prompt
and tool stack is not required; it is not a fixed overhead measurement or a
long-context cache-hit benchmark. Live production services were not switched.

A further wire-level check on Claude Code 2.1.314 enabled the production
LiteLLM callbacks and observed the JSON body immediately before the native
relay sent it upstream, without reading or recording authorization headers.
Plain text, streaming, and a tool-call/result round trip all succeeded. The
empty-tool calls sent `tools: []`; the tool calls sent only the supplied
`mcp__hermes__only_my_tool` schema. No native coding tools or skill inventory
were present. The minimal `Reply with exactly ok.` / `Hello` request used
385 input tokens total. The one-tool request used 750, then 793 on follow-up.

The wire still contained an SDK identity sentence, native billing metadata,
and an environment block (temporary working directory, platform/shell/OS,
download-handling guidance, model identity, knowledge cutoff and date).
Therefore this route removes the full Claude Code stack, but **does not
eliminate all native input overhead**. The token counts include the supplied
prompt and protocol framing; they are not measurements of overhead alone.
The upstream transport is experimental and version-sensitive. Non-streaming
disconnections may run until native completion or timeout; streaming closes
the transport when the response is cancelled.
