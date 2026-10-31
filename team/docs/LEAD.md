# The lead: Claude plans, free models build

`team goal` sends a goal to the lead before any worker sees it. The lead is
Sonnet on Eli's subscription through Hermes → LiteLLM → Portkey → the
subscription bridge. The isolated `lead` runner exposes only `read_file` and
`search_files`, in a
clean detached checkout of the goal's base under
`$TEAM_STATE/worktrees/<target>/_lead/`. The checkout is removed after each call.

It answers with a plan: the goal as at most `lead.max_parts` parts, and for
each part a work order. Code in `team/lead.py` decides whether the plan is usable.

| Order field | What Team checks |
|---|---|
| `role` | an active `hermes` role (today: `implement`) |
| `steps` | not empty |
| `owned_paths` | relative, inside the repo; parts that can run at the same time never share one |
| `context` | each `path:start-end` exists at the base; ranges clipped to 200 lines and to the file |
| `toolsets` | not empty, and each one is in the role's `tools` (the profile's toolsets) |
| `skills` | each one is a skill of the role |
| `model` | one of the role's `models.candidates`, resolvable in the catalog |
| `depends_on` | known part keys, no cycle |

A refused plan goes back to the lead once, with the reasons. A second refusal,
a usage limit or a failed call stops `team goal` with the reason. Nothing is
created. Use `--no-lead` to go without a plan.

## What a worker gets

One goal row, branch and worktree per part. The kanban task body is the
rendered order:

- the steps;
- the files the worker owns;
- the interfaces and pitfalls;
- the **code excerpts**, read from the worktree by Team (never copied by a
  model), up to `lead.context_bytes`;
- what done means.

The task carries the order's skills (`--skill`) and toolsets (`--toolset`).
Through the `[team]` kanban patch (`PROVENANCE.md`), Hermes gives the worker
exactly those toolsets plus `kanban`, and refuses any its profile lacks.

Attempt 1 runs on the order's model; later attempts use the role's candidates.

A part with `depends_on` waits, with no worktree, until every part it depends
on has landed. Then it branches from the new base. If one of them fails, the
part fails with it.

## After a failed check

If a free retry follows (`escalation.after_failures`), the lead reads three
things in a checkout of the worker's branch: the order, the diff against the
base, and the tail of the check output. It answers with one of three actions:

- **retry**: a cause and a note, optionally with new steps and context. A new
  order version is stored with the note. The note, the steps and the new
  excerpts go on the task as a `LEAD:` comment, and the retry stays on a free
  model.
- **escalate**: Opus takes the part over through the same gateway route, with the rendered order
  as its brief.
- **ask**: the goal stops with a decision for Eli.

If the lead cannot answer (usage limit, refused twice), the retry goes ahead
without it. `lead.diagnose = false` turns this off.

## Ledger

Each physical model request records its requested model, responding model,
profile, session and input/output tokens. Planner and rescue calls attach to
their Team job without adding a duplicate aggregate. Legacy `claude:` task
references remain readable; they no longer imply a direct Claude Code run.
Boss findings show per-model attempt outcomes beside the role/route baseline.
Mixed-model attempts remain a separate group.

| Row | What it holds |
|---|---|
| `jobs` with `role = 'lead'` | one per plan or diagnosis; `pass` means Team accepted the answer; the calls carry its tokens |
| `orders` | every order version, as JSON |
| `jobs.order_id` | which attempts worked from an order |
| `diagnoses` | the lead's reading of each failed attempt |

`team ledger` prints the implement pass rate with an order and without one,
with counts. `--no-lead` goals are the base rate.

## Settings (`team/orchestration.toml`)

| Setting | Default |
|---|---|
| `lead.enabled` | true |
| `lead.diagnose` | true |
| `lead.max_turns` | 30 |
| `lead.timeout_s` | 900 |
| `lead.max_parts` | 6 |
| `lead.context_bytes` | 24000 |

## Not yet

- **No fallback to the Max plan's API credit.** The Portkey `.env` holds no
  Anthropic key, and `build.py` requires a model on an allowance provider to
  be served by that provider alone. So a credit fallback means a separate
  credit-backed model entry and its key.
- **Lead calls run synchronously** inside `team goal` and inside a `team run`
  tick, as the Claude takeover already does.
