# Running the committed Team workflow

For the planned dashboard expansion, backend gaps, acceptance criteria, and
independent model comparison brief, see [Dashboard scope](DASHBOARD-SCOPE.md).

The dashboard opens on Overview, with Messages, Orchestration, Ideas, Agents,
Models, Routines, and Usage beside Work and Gateway. Use the persistent Boss
button or Messages to share an idea without choosing a project or filling a
form. The existing boss conversation gathers the specifics. Activity shows
recorded replies, runs, decisions, and findings, with new-event popups and
persistent read state. Alerts observe recorded activity; they do not schedule
new autonomous work.

Usage compares the top ten models/routes by input plus output tokens: red input
and green output share each bar. Last day/week/month select rolling 1/7/30-day
windows. A model enters when it overtakes the tenth entry (ties sort by name).
The chart groups calls by their recorded requested tier, with responding models
and their usage underneath. Tier totals include every call; the ten most-used
reported models are visible initially, with additional models expandable.
Missing responder identities and echoed tier aliases appear as "Actual model
unreported"; named requests without a tier have their own group. Current catalog
membership is never used to invent a historical tier or responder.
The LiteLLM usage callback preserves the provider-reported response model instead
of rewriting it to the requested tier alias. Gateway attempt records retain the
original tier, routed fallback group, selected deployment, response model, response
ID and token counts. Scout calls also retain their gateway session/trace ID on
success and failure. Historical aliases without that evidence remain unreported.
An authorized `POST /api/metrics/reset` starts a new dashboard measurement
period after the latest recorded call. It preserves the ledger and model-quality
evidence. Usage shows its start time and an "Include earlier history" toggle;
day/week/month filters still apply. This does not reset provider quotas or bills.
Gateway shows searchable routes and fallback paths; use Advanced controls for
the existing service actions and editors.

Run
`team/bin/team app --preview --port 8793` for an isolated comparison using a
disposable copy of saved state. Preview idea edits are temporary; execution and
configuration changes and message sending are disabled. Alert acknowledgements
are also temporary. The live app remains on port 8792.

Team uses Hermes kanban as its queue. Each goal gets a branch and worktree,
and a **type** that picks its pipeline (`team/gates.py`): the stages it goes
through, each a role and a code gate, and whether it lands by itself. Team
runs each stage's gate and lands a passing branch with a `--no-ff` merge.
Boss and lead handle planning; the roster's workers build. `team app`
(127.0.0.1:8792) is Team's one UI: Work (projects, cards, the chart, the
boss and aide), Agents (the roster with its gates and pass rates, the
pipelines, proposing a new agent), Gateway (the route as a diagram with
live rates, services, test, verify, logs, the catalog editors) and Usage.
Hermes's own dashboard with the kanban board is separate (9119).

## Dedicated My Prof workspace

`team/bin/team-workspace myprof app --port 8794` runs the dedicated My Prof
dashboard. The launcher uses `$HOME/state/team-myprof` for its own conversation,
memories, profiles, boards, ledger, findings and activity. It shares this Team
checkout and the existing local model gateway; application updates and base role
templates are shared. This is state isolation, not an OS security sandbox or an
independent Git clone. Do not enable another runner against the same target.

The initial setup attaches the existing `$HOME/work/StudyApp` checkout disabled,
enables scout evidence and provisions the standard profiles with `roles sync`.
It imports only reusable content-addressed source summaries, with hashes checked
against the current source. No old conversations, jobs, alerts, proposals or
usage records are imported. `gate audit` checks the configured model lock.
The workspace's `litellm/.env` points to the existing local gateway credential;
provider credentials remain with that gateway. Global gateway controls still
affect the shared infrastructure.

`team/bin/team-workspace myprof survey myprof --max-calls 1200` performs a
read-only discovery pass: finish cached source coverage, write the file/area map,
reason over bounded summaries per area, then prepare a prioritized build shortlist
with suggested agent responsibilities. The chosen cap covers all new source and
reasoning calls in that invocation; cache hits consume none. It stops on errors,
budget exhaustion or changed source before publishing final proposals. Rerunning
reuses source summaries; area reasoning is currently recomputed.

Overview shows the saved review checkpoint; Usage shows new calls; Ideas receives
the final proposals. Details live under the workspace's `targets/myprof/scout/`:
`map.json`, `area-analysis.json`, `review-status.json`, and `ideas.json`. Excluded
files and bounded summaries limit conclusions. Candidates require narrow source
verification and tests before implementation. The scout and survey reasoner are
utilities; the provisioned Boss, aide, lead, onboard, implement, test-write,
review and security roles support subsequent approved work. Discovery itself
neither enables the target nor edits its checkout.

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

## One-sweep attempts

A builder attempt is one sweep unless the lead orders otherwise (`team.sweep`,
`sweep.enabled`). Team assembles the packet in code: the order, every file the
part owns in full, the code the order cites, and on a retry what failed. One
tool-free call through LiteLLM on the attempt's free model answers with a JSON
object of whole files, exact find/replace edits and deletions, all inside the
owned paths, or `{"cannot": …}`. Code applies it, commits (`team sweep N.k:
…`), merges the base and runs the check and the acceptance command like any
attempt. A failed check sends the files as the answer left them and the
check's output back for another answer, up to `sweep.calls_per_attempt`
calls; `sweep.samples` asks for that many answers in parallel and keeps the
first that passes. The attempt's job has `task_ref = sweep:<goal>-<attempt>`,
no kanban task, and one `calls` row per model call. When every call is spent
the attempt has failed like a kanban worker's: the lead diagnoses, the next
candidate model tries, then Claude, then Eli.

Why: a turn-based worker re-sends its whole context on every tool call. In
the ledger on 2026-11-02 a passing implement attempt averaged 25 calls at 27K
input tokens for about 300 output tokens each, 505 reads and 443 terminal
calls against 257 edits across all implement sessions. The packet replaces the
reading; the check replaces the running.

The lead sets `mode` per part: `sweep` for a part whose files it can name and
whose change is clear from reading them; `turns` (the kanban worker with
tools) for a part that must run or explore things to know what to write. A
diagnosis may switch the mode for the retry. A goal with no owned files (a
plain `team goal` without the lead) always runs in turns.

Budgets: `worker.max_calls_per_goal` and `worker.max_input_tokens_per_goal`
are enforced when an attempt fails; a goal over either stops and asks Eli
instead of trying again. The replay verdict stays pass-count first;
`replay.adopt_tie_on_fewer_tokens` (off by default) also adopts a tie that
costs at most 80% of the base's worker input tokens. Model choice ranks
candidates by work pass rate and, among those within 0.1 of the leader, by
input tokens per attempt.

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

## Reading an existing project cheaply

`team attach "$HOME/work/StudyApp" --name myprof --disabled` registers the
existing checkout without cloning it or enabling dispatch. `team targets
--set myprof scout true` enables summary-first planning. `team scout myprof
--max-calls 24` reads tracked, modified and untracked source, excluding ignored
files, common data/dependency directories, credential filenames, symlinks,
binary files, lockfiles and oversized files. It never stashes, checks out,
commits or edits target files. Credentials embedded in ordinary source are
not detected by filename filtering; the source scope must be appropriate for
the configured free-model gateway.

Scouting uses tool-free calls to the free gateway, with a per-run call cap and
one scout lock per target. Receipts and content-addressed chunk summaries live
under `$TEAM_STATE/targets/<target>/scout/`. Re-running continues pending chunks
and reuses unchanged ones. The report states partial coverage, exclusions,
errors, actual provider token usage (or missing usage), and source/summary
character counts. Character reduction is not a measured Claude token saving.

`team ideas myprof` sends only the bounded scout summary to `tier-reasoning`,
with no file or terminal tools, and saves proposals plus usage under
`scout/ideas.json`. It does not queue builds. This is a way to use a stronger
reasoner for choosing improvements without paying for it to read bulk source.

The boss's `team_notes_read` includes the bounded scout evidence. The lead
refreshes summaries for its clean planning worktree and receives them instead
of bulk source outlines; project instructions remain authoritative. Onboarding
gets matching cached evidence first. Changed or deleted file summaries are
omitted. Model suggestions are hypotheses: the planner must verify narrow
source ranges before ordering edits, and code gates still decide correctness.
The dirty-checkout report is kept separately from clean-worktree planning.

## Model selection and routines

Role pins and the lead's explicit first-attempt model remain authoritative.
Other worker attempts use graded outcomes for that role/target (pooled by role
when target evidence is sparse), excluding infrastructure failures. Before
there is enough evidence the manifest order is used. Rankings use smoothed
pass rates; exploration gives the least-observed candidate a turn every fifth
eligible choice. The recorded reason includes pass counts and the baseline.

Opt-in routines live in `$TEAM_STATE/targets/<target>/routines.toml`:

```toml
[[routines]]
name = "survey"
action = "scout"
enabled = false
every_s = 86400
```

`action = "goal"` additionally requires `goal = "..."` and a configured target
check. Enabling such a routine authorizes that goal on the interval; it uses
the normal lead/pipeline/landing policy. No routine is enabled by default.
Disabled targets and quota pauses are respected; build routines wait for active
goals. A durable claim prevents duplicates in one interval. A crash leaves an
unresolved claim that blocks later intervals until inspected; it is never
silently retried. The `routine_runs` ledger table records outcomes and errors.
