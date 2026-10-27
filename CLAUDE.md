# Team, built on a fork of Hermes Agent

This repository **is** Nous Research's Hermes Agent, forked at the root, with
Portkey's gateway, LiteLLM and Nous's self-evolution pipeline forked in as
subtrees, and Team layered on top. Read, in this order: `IDEAS.md` (what Team
is and how Eli wants to work), `PROVENANCE.md` (what is pinned and how it is
updated), `FRESH-START.md` §3 (Eli's decisions), `PLAN.md` (the build plan and
where it stands). `HANDOFF-GATEWAY.md` is the brief for the session that owns
the LiteLLM and Portkey connection.

## What Team is, in one paragraph

An agent team you point at a repo. Eli talks to one agent, the boss. It plans a
goal with him until each part is ready, then hands parts to defined worker
roles on free models. Code, not a model, decides whether a result is done: the
target's check runs in a git worktree, what passes lands, what keeps failing
goes to Claude on Eli's subscription and then to Eli. Every attempt is a row in
a ledger, and the team improves by proposing one change at a time and keeping
it only when a replay set says it beat the base rate.

## Ownership rule

Upstream files are never edited in place. Team lives in `team/`,
`plugins/team/`, `integrations/`, `tests/team/`, and the Team-owned files at
the root. A change to an upstream file is a marked `[team]` commit listed in
`PROVENANCE.md` with file and line, and is kept minimal. Update Hermes with
`git fetch upstream main && git merge upstream/main`; update a subtree with
`git subtree pull`. Hermes owns the top-level `gateway/` (its messaging
gateway); the Portkey fork is `portkey/`.

## Running anything

Everything Team runs goes through `team/bin/team-hermes`, which sets
`HERMES_HOME=$HOME/state/team/hermes` and `HERMES_KANBAN_HOME=$HOME/state/team/kanban`,
scrubs every paid-provider and Anthropic variable from the environment, and
executes the checkout through `scripts/run-in-hermes-env` and Hermes's PM-owned dependency environment. Never bare `hermes`, never
`~/.hermes`: that is another project's install and holds paid keys.

## Free-only, fail closed

Unattended work runs only on free models or on Claude Code through Eli's
subscription with every `ANTHROPIC_*` variable removed. Enforced in layers:
`integrations/portkey/catalog.json` is the only source of allowed models; its
generator refuses anything not free; Portkey holds the only copy of provider
keys; every Hermes profile is pinned (no `auto`, no fallbacks, every side task
on a free tier); the Team plugin logs every model call into the ledger and
marks anything off-catalog as a violation; `team gate audit` runs on every
tick. A new provider or model is a catalog edit, never a config edit.

## Tests and gates

`team/bin/team-check` is the gate for Team changes and the per-attempt check
for the `team` target. It runs `scripts/run_tests.sh tests/team` with the main
checkout's PM test interpreter, then Team lint, preserving both exit codes.
A worktree runs its own tests and code without provisioning another environment.
Run focused upstream tests through `scripts/run_tests.sh` when patching Hermes;
run its full suite after upstream merges. Subtrees keep their own suites.

## State

Everything mutable is under `$HOME/state/team/`: the ledger `team.db`, the
Hermes home and profiles, the kanban home, worktrees, Portkey and LiteLLM
state. The old Team's state is at `$HOME/state/team-old/`, read-only reference.

## Conventions (from Eli's global rules)

Real numbers only: run the command or read the file; a rate always next to its
base rate with counts. Verify what is running from processes and logs, never
from a markdown file. No empty scaffolding: a directory appears with its first
real file. Paths derive from `$HOME` or the script location, never a user name.
No `cd` inside a shell command: `git -C`, `npm --prefix`, relative paths.
Commit as you go. Never push to a remote without asking. Ask before assuming
when scope is ambiguous or existing work is at risk; otherwise just do it.

## Terminal

`work team` opens the herdr session from `.work.json`: a Claude pane, the boss
pane, the dashboard, a shell. The Team window is 120 columns.
