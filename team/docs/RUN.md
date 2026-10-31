# Running the committed Team workflow

Team uses Hermes kanban as its queue. Each goal gets a branch and worktree;
Team runs the target's check and lands a passing branch with a `--no-ff` merge.
Only the `implement` role is active. Boss planning, the Team dashboard plugin,
and replay/evaluation/adoption are later phases. The standard Hermes dashboard
and kanban board are available separately.

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

After those attempts, Team tries Claude Code through the subscription with API
credentials scrubbed. If that fails too, Team blocks the task, preserves the
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
