# Hermes, with its own model gateway

This repository **is** Nous Research's Hermes Agent, forked at the root, with
Portkey's gateway, LiteLLM and Nous's self-evolution pipeline forked in as
subtrees. On top of that sits one thing: the connection between LiteLLM and
Portkey, generated from one catalog, that every model call from this Hermes
goes through. Hermes itself operates that gateway, through a skill. Read
`PROVENANCE.md` for what is pinned, what is patched and how each piece is
updated; `integrations/portkey/README.md` for how the gateway works and was
verified; `integrations/hermes/skills/model-gateway/SKILL.md` for how Hermes
reads and changes the routing.

The boss/worker "Team" layer that used to live here was stripped on
2026-11-09. It is at tag `before-strip`; its state is at
`$HOME/state/team-boss-old/`. Do not rebuild it.

## Ownership rule

Upstream files are never edited in place. This repository's own code lives in
`integrations/`, `tests/team/`, `litellm/team/`, `portkey/src/team/`, `TEAM.md`
in each subtree, and the owned files at the root (`CLAUDE.md`,
`PROVENANCE.md`, `.work.json`). A change to an upstream file is a marked
`[team]` commit listed in `PROVENANCE.md` with file and line, and is kept
minimal. Update Hermes with `git fetch upstream main && git merge
upstream/main`; update a subtree with `git subtree pull`. Hermes owns the
top-level `gateway/` (its messaging gateway); the Portkey fork is `portkey/`.

## Running Hermes

Always `integrations/bin/hermes <args>`. It sets
`HERMES_HOME=$HOME/state/team/hermes` and `HERMES_KANBAN_HOME=$HOME/state/team/kanban`,
scrubs every paid-provider and Anthropic variable from the environment,
exports the LiteLLM master key as the only credential, links the gateway skill
into the home, and executes the checkout through `scripts/run-in-hermes-env`
and Hermes's PM-owned dependency environment. Never bare `hermes`, never
`~/.hermes`: that is another project's install and holds paid keys.

The default profile's provider is `litellm` at `http://127.0.0.1:4000/v1`,
with the tiers `tier-simple`, `tier-medium`, `tier-complex`, `tier-reasoning`
(and `tier-claude`, the subscription bridge) as its models.

## Free-only, fail closed

Every model call goes LiteLLM → Portkey → provider; Portkey holds the only
copy of provider keys, in `$HOME/state/team/portkey/.env`. Claude is reached
through Eli's Max subscription by the loopback bridge in
`integrations/subscription/`, as a catalog provider, with every `ANTHROPIC_*`
variable removed. Never an Anthropic key with a payment method.
`integrations/portkey/catalog.json` is the only source of allowed models; its
generator refuses anything not free; `build.py --check` runs before either
proxy starts and refuses drift. A new provider or model is a catalog edit,
never a config edit.

## Tests and gates

`integrations/bin/check` is the gate: `build.py --check`, then
`scripts/run_tests.sh tests/team`, then the Portkey patch suites under jest.
Run focused upstream tests through `scripts/run_tests.sh` when patching
Hermes; run its full suite after upstream merges. Subtrees keep their own
suites.

## State and services

Everything mutable is under `$HOME/state/team/`: the Hermes home and
profiles, the kanban home, LiteLLM (`config.yaml`, `usage.db`, `.env`),
Portkey (`configs/`, `requests.jsonl`, `.env`), the subscription bridge.
Three launchd jobs: `com.fullmoney.team-litellm` (4000),
`com.fullmoney.team-portkey` (8788), `com.team.subscription` (8790). Status:
`sh litellm/team/status.sh`, `sh integrations/portkey/status.sh`.

## Conventions (from Eli's global rules)

Real numbers only: run the command or read the file; a rate always next to its
base rate with counts. Verify what is running from processes and logs, never
from a markdown file. No empty scaffolding: a directory appears with its first
real file. Paths derive from `$HOME` or the script location, never a user name.
No `cd` inside a shell command: `git -C`, `npm --prefix`, relative paths.
Commit as you go. Never push to a remote without asking. Ask before assuming
when scope is ambiguous or existing work is at risk; otherwise just do it.

## Terminal

`work team` opens the herdr session from `.work.json`: a Claude pane, a
Hermes pane, a shell. The Team window is 120 columns.
