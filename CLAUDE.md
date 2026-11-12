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

`~/.local/bin/hermes` is a three-line script that execs
`$HOME/work/Team/integrations/bin/hermes`, so plain `hermes` works anywhere.
It must not be a symlink or name this repo's path literally: Hermes's PM
"repairs" any launcher there that points into the checkout, replacing it with
`.hermes/bin/hermes`, which runs this code against `~/.hermes` (another
install). The old `~/.hermes` launcher is `hermes-home`.

`-p default` is the Team home: with the wrapper's `HERMES_HOME`, Hermes
resolves `default` to it (Desktop sends `gateway ensure -p default`). The
wrapper asks Hermes before letting it through and refuses it if it would ever
resolve elsewhere, such as `~/.hermes`. Plain `hermes` with no flag is still
the normal way in.

Hermes Desktop: open it only with `hermes desktop`. The wrapper sets
`HERMES_DESKTOP_HERMES` to itself so Desktop runs this checkout through it.
Clicking `Hermes.app` in Finder or the Dock starts it with no Team
environment, against `~/.hermes`. Never its "Install Hermes locally", never
"Update" / "Update all instances": both pull stock upstream over this fork.

## The Hermes factory: base layer, Team profile, project profiles

- **Locked base** — `integrations/hermes/base/config.yaml`, loaded as Hermes's
  managed layer (`HERMES_MANAGED_DIR`) on top of every profile: the `litellm`
  provider at `http://127.0.0.1:4000/v1` with its tiers, empty fallbacks,
  every auxiliary lane on `litellm`, `free_only`. No profile can change these
  keys; edit the file here to change them for all.
- **Team = the base home, no profile** — Hermes launched in this repo or any of its
  worktrees. Its skills (`$HOME/state/team/hermes/skills`) are shared live and
  read-only with every project through `skills.external_dirs`.
- **Project profiles** — any other directory maps to a profile named by
  `integrations/bin/hermes-project` (the git root; under `~/work` the path joined
  by `-`). The first launch there clones `default` (config, SOUL, memory), empties
  its skills so the shared ones stay live, and writes `skills.external_dirs`.
  After that the profile evolves on its own; skills it learns stay local.
  Promote one to every project by moving it into the default profile's skills.
- An explicit `-p <name>` or the `profile` subcommand skips the automatic choice.
- Hermes's background gateway keeps an in-memory profile list: remove a
  profile with `hermes profile delete <name>`, not `rm`, or it recreates an
  empty shell.

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

## Automatic model choice: tier-auto

Hermes's main model, subagents and cron name `tier-auto`: LiteLLM's
complexity router (in the fork, `litellm/litellm/router_strategy/complexity_router/`)
scores each new ask locally and sends it to the simple, medium, complex or
reasoning tier, with a reasoning effort per tier (`auto.efforts`); tool-call
turns replay that decision. On any tier call carrying tools,
`litellm/team/agent_gate.py` keeps only tool-capable models and drops those
with a bad recent record on tool requests (`policy.agent_gate`, `usage.db`). Its settings are the
catalog's `auto` section; never `tier-claude`. Each decision is in `usage.db`
(`route_json`). A catalog row's `reasoning_efforts` lists what its provider
accepts, and `litellm/team/effort_clamp.py` moves other values into it.

## Looking at and changing the gateway

`integrations/bin/gateway show` — every tier in routing order, each model's
published facts (catalog `params_b`, `active_b`, `benchmarks`, each benchmark
with its source) beside its usage.db record (ok excluding rate limits, tool
calls on fresh asks, latency) and the tier's base rate; flags weak models.
`integrations/bin/gateway apply` — after a catalog edit: build, check,
restart, status, one test call per tier that changed against HEAD. It never
commits. `integrations/bin/lite` — LiteLLM's CLI with the key loaded; it
refuses login, the agent launchers (`claude`, `up`, …) and anything that needs
LiteLLM's database or would add models outside the catalog.

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
