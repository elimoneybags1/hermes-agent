# Team docs — the working run loop

Team is an agent team you point at a git repo. It works from outside the repo:
the target is only ever read (attach records its commit), every change reaches it
through a merge into `base` after the target's check passes, and all state —
ledger SQLite, kanban boards, Hermes profiles — lives under `$TEAM_STATE`
(default `~/state/team`), never inside the target and never in `~/.hermes`.

One goal = one branch (`team/<slug>`) + one worktree
(`$TEAM_STATE/worktrees/<target>/<slug>`). The loop can stop and restart at any
point: what it tracks lives in the ledger, so `team run` is resumable.

"Done" is decided by code, never by a model: base merged into the branch in the
worktree, the worktree clean, only owned files changed, the work committed, then
the target's check run in the worktree.

## Commands

```
team attach <path> [--name N] [--check CMD]   register a git repo as a target
team targets                                   list targets
team roles list|sync [ROLE]                    roster; sync a role's Hermes profile
team goal <target> "<goal>" [--role R] [--model SPEC] [--check CMD]
                                               hand Team a goal on a target
team run [--once] [--interval S]               the loop: one tick, or repeating
team ledger [--target T] [--all] [--json]      jobs and goals
team decide list|answer <id> "<text>"|dismiss <id>
                                               Eli's inbox of open decisions
team gate audit                                every profile against the free-only lock
team settings get|set <key> <value>            orchestration.toml (committed per change)
team hook stop|pretool                         Claude Code hooks (stdin JSON)
```

### 1. Attach a target

```
team attach /Users/fullmoney/work/Team --name team --check "team/bin/team-check"
```

The name defaults to the repo directory name. The check command decides "done"
for the target; omit it and the orchestration default applies
(`grading.default_check`, `pytest -q`). A target record goes to
`$TEAM_STATE/targets/<name>/target.json`; the repo itself is not touched.

### 2. Sync the implement profile

```
team roles list
team roles sync implement
team gate audit
```

`roles sync implement` makes `$TEAM_STATE/hermes/profiles/implement` match
`team/roles/implement/role.toml`: `config.yaml` (the `_base` lock plus the
role's overrides — litellm provider, candidate model `tier-medium`→`tier-complex`,
`max_turns 40`, `run_budget_s 900`), `SOUL.md`, and the role's skills. Anything
the profile learned on its own (memories, other skills, sessions) is left alone.

`gate audit` is the lock: it reports any way a profile could reach a paid or
unpinned model — provider not in `{litellm, subscription}`, non-loopback base
URL, a literal `api_key` value, `auxiliary.free_only` not true, any auxiliary
task without provider/model, `background_review` or `curator` or `moa` on,
`kanban.dispatch_in_gateway` / `review_dispatch` / `auto_decompose` not false,
the `team` plugin not enabled. `team run` does not dispatch while findings
exist; fix them, commit, sync again.

### 3. Create a goal

```
team goal team "document the Team run loop" --role implement --check "team/bin/team-check"
```

This creates branch `team/document-the-working-team-run-loop` from `base`
(`main`) in a fresh worktree, creates kanban task attempt 1 on that branch
(assignee `implement`, model from the role's candidates — `tier-medium` first,
then `tier-complex` on retry — resolved through `team/catalog`, so nothing
outside the free catalog can be dispatched), opens a ledger job, and starts the
first attempt. The worker's brief tells it exactly which files it owns: change
nothing else.

Default role is `implement`; default check comes from the target record.

### 4. Tick the loop

```
team run --once        # one tick and exit
team run               # repeat every loop.interval_s (60s)
```

One tick, settings re-read fresh each time:

1. **Preflight** — profile lock audit (`gate audit`) and the portkey catalog
   build check (`integrations/portkey/build.py --check`). On a finding, log it
   and do not dispatch this tick.
2. **Dispatch** — `kanban dispatch` spawns workers for ready tasks, capped at
   `loop.max_new_per_tick` (3), unless the target is paused (free quota).
3. **Step each active goal** — for the goal's current kanban task:
   - `review` or `done`: grade it (see §review → check → merge).
   - `running`: watch the worker — dead pid or run older than
     `run_budget_s` (900s) + 300s grace gets terminated; a dead worker with a
     free-quota tail pauses the target until UTC midnight, otherwise the job
     fails and the next attempt starts.
   - `blocked`/`crashed`: if kanban gave up, finish the job (`error`/`timeout`)
     and start the next attempt; otherwise finish job and goal (`kanban <status>`).
   - still `queued`: kanban dispatches it next tick.

### 5. Inspect the ledger

```
team ledger            # last 30 jobs, open goals, pass rate
team ledger --all      # every goal
team ledger --json     # machine-readable jobs and goals
```

`pass N/M` is the goal pass rate; each goal row shows outcome (or `attempt N
<task>`), role, branch, and the first line of the goal; each job row shows
outcome, model choice and task ref. The ledger is SQLite at
`$TEAM_STATE/team.db` — outcomes are written by grading, never by a model.

### 6. Answer decisions (Eli's inbox)

```
team decide list                # open decisions: question, options, recommendation
team decide answer 3 "merge main first"
team decide dismiss 3
```

Decisions appear when the loop needs a human: all free attempts failed and the
Claude attempt failed too (`ask.block_after_claude_fails`), or Claude Code
wrote a decision file asking Eli a question. `dismiss` closes one without
answering.

### 7. review → check → merge

When the worker has done the work:

1. It commits on the branch (uncommitted work is lost and does not count) and
   calls `kanban_request_review` — never `kanban_complete`: **Team grades**.
2. The loop attaches the task's model calls to the job, runs the free gate
   (any call outside the catalog fails the job), then grades:
   - merge `base` into the worktree branch; a conflict aborts cleanly and is
     reported — the worker runs `git merge <base>` first, resolves keeping both
     sides' intent, runs the check, and commits the merge;
   - require a clean worktree;
   - require only owned files changed (`grade.outside_owned`);
   - run the target's check in the worktree (`grading.check_timeout_s`, 900s).
3. Pass → `kanban complete` on the task → **land**: `--no-ff` merge into
   `base`, worktree removed, branch deleted, goal `pass`.
4. Fail → comment `FAILED: <detail>` on the task and retry (below).

On a merge conflict at grading time the check reports
`merge conflict with main: <files>`; resolve with `git merge main` in the
worktree, keep both sides, run the check, commit — then review again.

### 8. Retry and Claude escalation

A failing free attempt is retried on the same task: kanban reopens the review
task (comments stay in its context) with attempt `N+1` and the next model
candidate, up to `escalation.after_failures` (**2 for `implement`**: attempts 1
and 2 on `tier-medium`, `tier-complex`). After that:

- If `escalation.claude` is on, one attempt on **Claude Code (subscription)**
  (`claude_max_turns` 60, `claude_timeout_s` 1800). If Claude asks a question,
  the question becomes a decision in `team decide` and the goal waits on Eli.
  If Claude passes, it lands like a free attempt. If Claude fails, the goal
  gives up.
- Otherwise, or after Claude fails too: block the task, open a decision for
  Eli (`retry with a new brief` / `drop the goal` / `take it over by hand`),
  and mark the goal `fail`. The branch `team/<slug>` is kept — nothing is lost.

The worker's own budget is `run_budget_s` (900s); the loop terminates an
over-budget worker itself (kanban does not enforce `--max-runtime`).

### 9. Worktree isolation

Each goal runs in its own worktree, so concurrent goals never clobber each
other's files, and `team run` can stop mid-tick and resume where the ledger
says. Kanban removes a clean worktree when a task completes; grading re-adds
it from the branch when missing (`grade.ensure_worktree`). Worktrees live under
`$TEAM_STATE/worktrees/<target>/<slug>`, not inside the target repo. The target
repo is read-only to Team: attach runs `git rev-parse` only; the merged result
arrives through `grade.land`'s `--no-ff` merge into `base`.

### 10. Not implemented yet

**boss** (plans/parts), **UI**, and **replay** are not implemented. The ledger
has `plans`, `parts` and `reviews` tables, but nothing releases parts or drives
a boss loop; there is no dashboard or web UI for Team (the CLI is it); and past
runs cannot be replayed — only the ledger's record of them exists.

## Example session

```sh
# 1. Register the repo you want Team to work on.
team attach /Users/fullmoney/work/Team --name team --check "team/bin/team-check"

# 2. Make the implement profile match team/roles/implement, then prove it is locked.
team roles sync implement
team gate audit          # expect: gate audit: clean

# 3. Hand Team a goal. It creates branch team/document-run-loop and a worktree.
team goal team "document the Team run loop in team/docs/RUN.md"

# 4. Tick once: dispatch, grade anything in review, step running goals.
team run --once

# 5. See where things stand.
team ledger --target team

# 6. If Claude needed a decision, answer it and tick again.
team decide answer 2 "yes, merge main"
team run --once

# 7. When the worker requests review, Team grades: merge base, ownership,
#    check. Pass lands (--no-ff merge into base, worktree removed).
team ledger --target team   # goal pass, landed <sha>
```

The worker finishes its task with `kanban_request_review` plus a summary of
what it did and the check's last line; `team run` does the rest.