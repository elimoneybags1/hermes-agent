# Running the committed Team workflow

Team uses Hermes kanban as its queue. Each goal gets a branch and worktree,
and a **type** that picks its pipeline (`team/gates.py`): the stages it goes
through, each a role and a code gate, and whether it lands by itself. Team
runs each stage's gate and lands a passing branch with a `--no-ff` merge.
Boss and lead handle planning; the roster's workers build. The standard
Hermes dashboard and kanban board are available separately.

## Pipeline

| Type | Stages (a stage whose role is not active is skipped) | Landing |
|---|---|---|
| bugfix, feature | test_write → implement → test_verify → review | by itself |
| dependencies | implement → test_verify → review | Eli |
| security | implement → test_verify → security → review | Eli |
| onboarding | spec (onboard) → review | Eli |

`test_verify` is code only: Team merges the base, then runs the target's
check, the part's acceptance command and the target's `e2e` command in the
worktree; its job row has role `team` and never counts as an agent's attempt.
A code-only stage that fails sends the goal back to the builder as a failed
attempt with the output. Risk only goes up: whatever the type, a diff that
touches dependencies, lockfiles, migrations, CI, auth, billing or secrets
(`team.gates.DEFAULT_RISK_PATHS` plus the target's `risk_paths`), or more
lines than `risk.max_changed_lines`, waits for Eli: a `Land goal N?` decision
in the inbox; "land it" lands, anything else keeps the branch and fails the
goal.

Every part from the lead carries an **acceptance command**: one command that
fails before the part is built and passes after (its own new tests). The
`check` gate runs it after the target's check, and refuses a commit that
changes nothing against the base.

## Commands

Run from this checkout, using `team/bin/team` (or the installed `team` link).
Both `team` and `team-hermes` use Hermes's PM environment and the isolated state
at `${TEAM_STATE:-$HOME/state/team}`. They remove provider credentials inherited
from the shell; Hermes profiles authenticate to the existing local model proxy.

```sh
team/bin/team attach "$PWD" --name team --check "$PWD/team/bin/team-check"
team/bin/team roles sync implement
team/bin/team gate audit
team/bin/team goal team "Describe one concrete change"
team/bin/team run --once
team/bin/team ledger --target team --all
team/bin/team decide list
```

`goal` asks the lead (Claude) to split the goal into parts and write each
worker's order, then queues the parts; `goal --no-lead` queues one task with a
plain brief. See [LEAD.md](LEAD.md). `run` dispatches the work. Repeat `run --once` to advance completed
workers, or use `run` for the continuous loop. `--interval` controls the delay
between ticks. `goal` accepts `--role` and `--check`; model candidates come from
the role manifest, not a `--model` CLI argument.

`team/bin/team-check` runs the worktree's `scripts/run_tests.sh tests/team` with
the main checkout's PM test interpreter, then Team lint. Either failure makes
the check fail. Worktrees reuse the environment without installing another one.

`run` takes `--target NAME` (repeatable) to work on those targets only, so one
loop per target keeps a slow check on one from stalling the rest; `--no-boss`
leaves the boss's releases and reviews to another loop and `--boss-only` does
only those; `--pin` keeps the loop on the code it started with instead of
restarting when the checkout changes. One loop per target is enforced with
lock files under `$TEAM_STATE/run/`, and checks run one at a time machine-wide
(`check.lock`). `goal --no-lead` takes `--type` and `--acceptance`.
`targets --set NAME KEY VALUE` sets a target's `check`, `enabled`, `e2e`,
`setup`, `check_env` or `risk_paths` (lists are comma-separated).

## Review, checks, and landing

1. A worker edits and commits in its own worktree, then calls
   `kanban_request_review`.
2. Team attaches the task's calls to its ledger job and checks for catalog
   violations. It requires committed work and a clean worktree, merges the
   current base branch, enforces owned paths when supplied by a part, and runs
   the target check.
3. Passing work is completed on kanban and merged into the target's base branch.
   The goal becomes `pass`; the worktree and task branch are removed.
4. Landing refuses staged changes, a wrong checked-out base, and changes Git
   cannot merge without overwriting local edits. Unrelated unstaged edits and
   untracked files survive. Merge autostash is disabled for this operation.

Ordinary `team goal` briefs do not supply a structured owned-path gate. Scope
instructions in their text are instructions to the worker, not enforcement.
A landing failure keeps the branch and records an error; it needs explicit
recovery rather than blindly redispatching already-finished work.

## Retry, escalation, and decisions

The implement role currently allows two free attempts, first `tier-medium`,
then `tier-complex`. A failed review reopens the same task with its comments
and updates the actual kanban model override. A failed worker may instead
receive a new task on the same goal branch.

After those attempts, Team runs an isolated Hermes rescue on Opus through
LiteLLM → Portkey → the subscription bridge, with API credentials scrubbed.
The rescue exposes file and terminal tools, checks file edits against the
owned paths, and reruns the check before returning. Terminal commands are
not an OS sandbox; Team still rejects out-of-scope changes at grading. If that fails too, Team blocks the task, preserves the
branch, marks the goal failed, and opens a decision. Completed or archived
kanban tasks are not moved back to blocked.

```sh
team/bin/team decide list
team/bin/team decide answer <id> "The chosen next step"
team/bin/team decide dismiss <id>
```

Answering records the decision; it does not automatically restart a finished
goal. Create a revised goal or recover its existing branch explicitly.

## Evidence and limits

The ledger is `$TEAM_STATE/team.db`. `ledger --json --all` returns jobs and
goals; `pass N/M` counts graded job attempts, not goals. Compare with a stated
baseline before drawing conclusions about model performance. Kanban logs are
under `$TEAM_STATE/kanban/kanban/boards/<board>/logs/`.

The loop checks profile locks and catalog-generation consistency before
new dispatches. A detected free-quota exhaustion can pause a target. These
are distinct mechanisms; this layer does not implement the model proxies.
Existing in-flight work is still inspected when dispatch is paused.

Use `team/bin/team-hermes dashboard --no-open --port 9119` for Hermes's dashboard.
The `/team` plugin page and the `boss` profile are not implemented by this phase.


## Model health and token accounting

LiteLLM writes every deployment attempt to `$TEAM_STATE/litellm/usage.db`,
including failed requests and fallbacks. Rows identify the requested group,
actual model, session and deployment. Input, output and total tokens are
recorded for streaming and non-streaming responses. Cache reads, cache writes
and reasoning tokens are separate detail fields when supplied by the provider;
these details must not be added again to the totals. Raw usage is retained in
`usage_json`. `usage_reported = 0` means no raw usage was supplied (or the row
predates the migration), not that the request consumed zero tokens.

The model order lives in `integrations/portkey/catalog.json`. Health probes
must include a tool call followed by its result: plain-text responses can pass
while a provider's tool-history translation is broken. Probe results measure
current availability and basic protocol correctness, not coding quality or
sustained quota capacity. Keep comparison rates next to their baseline and
leave unknown usage or responders unknown.
