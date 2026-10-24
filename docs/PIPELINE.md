# The pipeline: every goal goes through the team, not through the boss

Eli, 2026-10-24: "even in one section of the build it should go through
different agents, from the foreman, to planning, to actually executing. Get the
majority of the work off the foreman, but not all of it to the decision of one
agent. Whatever agent is coding should know exactly what it needs to do, and
multiple can be spawned to code different parts. Before it gets to the coder it
goes through planning; after it is coded, testing, and an agent making sure what
is built follows the goals."

Today the boss writes every spec and brief and one coder does the rest. From
here, the boss only plans with Eli (`docs/PLANNER.md`) and hands over **parts**;
everything after that is the line below, run by `team run` on kanban.

## The line, for one part

```
boss ──► spec ──► test (write) ──► implement ×N ──► test (verify) ──► review ──► land
          │            │              (parallel)          │              │
          └── decision ┴──────────── Eli's inbox ◄───────┴──────────────┘
```

| Stage | Agent | Gets | Produces | Gate (checked by code before the next stage starts) |
|---|---|---|---|---|
| hand-off | **boss** | Eli's goal, the plan | one part: what, why, done-when, the decisions that apply | the part has a done-when |
| spec | **spec** | the part, the code, the plan, the decisions log | a contract (interfaces, behaviour, how to verify for real) and a split into code tasks, each with the exact files it owns and the tests that prove it | valid task list; every task names files and tests; no file in two tasks; files inside the repo |
| test (write) | **test** | the contract | the tests, committed first | only test files changed; the new tests are collected and **fail**; every other test still passes |
| implement | **implement** ×N, one per code task, in parallel | its own task from the contract: files it owns, tests it must pass, nothing else | the code | it touched only its files; its tests pass; Team merges it into the part's branch (a conflict fails it) |
| test (verify) | **test** | the merged part and the contract's "how to verify" | evidence: commands run against the real thing (CLI, server, browser), output | full check passes; the agent's verdict is pass and its session shows it ran them |
| review | **review** | the diff, the part, the plan, the goals, the decisions log, the project's rules | `land` or `fix` per criterion of done-when, drift, and any decision Eli must make | verdict is `land` with every criterion met; `fix` goes back to the implement task it names, a decision goes to Eli's inbox |
| land | Team (code) | | merge into the target's main | as today |

No agent grades its own work, and no single agent decides alone: spec is checked
by code and then by tests that must fail; code by those tests; the whole by a
real run; the result against the goals by a reviewer that did not write it; and
anything only Eli can decide goes to Eli.

## How it maps onto kanban

One part = a small graph on the target's board, linked with kanban parents so
kanban starts each stage only when the one before is done:
`spec` → `test:write` → `impl:1..N` (siblings, all children of `test:write`) →
`test:verify` (child of every `impl`) → `review`. Each task is assigned to its
role's profile (`team-spec`, `team-test`, ...), so each stage is a lane on the
board and each agent can be chatted with. Every task ends in kanban's `review`
column and Team's gate decides (`docs/RUN.md`, Round 3): pass → `complete`;
fail → comment + `reopen-review`, attempt + 1, then Claude, then Eli.

Branches: the part has `team/<part>`; spec and test commit there; each
implement task works on `team/<part>--<n>` from it and is merged back by Team.
The spec and its task list live in `$TEAM_STATE/targets/<name>/specs/<part>.md`
and `.json` (nothing added to the target repo, Decision 1), and are put in
every later stage's brief.

## Roles

`roles/spec`, `roles/test` (new, replaces nothing), `roles/implement`,
`roles/review` (now: conformance to the goals and the rules); `roles/triage`
folds into spec's first step and is dropped. Each gets a `SOUL.md` and skills
written from the ledger's real failures (big files, not committing, tests that
use fakes for the thing under test, not merging main). Which model runs each
stage is measured, not fixed: `docs/MODELS.md` (candidates per role, chosen
from the ledger by pass rate, speed and cost; pinnable in `team ui`).

## Settings

`pipeline.max_parallel_impl` (int, 3): most implement tasks of one part running
at once. `pipeline.enabled` (bool, true): off sends a goal straight to one
implement task, as today.

## Build

1. Roles: `spec`, `test`, `review` manifests, SOUL.md and first skills; drop
   `triage`. (boss: role definitions are agent edits.)
2. `team/pipeline.py`: build a part's graph on kanban; the gate for each stage;
   merging implement branches into the part branch. `team run` steps parts
   through it. `team part <target> "<part>"` replaces `team goal` for planned
   work (`team goal` stays as the one-task path).
3. Dashboard: the pipeline strip shows each part's stage.
