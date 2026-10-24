# Planning: from a big goal to parts that are ready to build

Eli, 2026-10-24: "if I dump in a big goal, I might not know all the things a
part of it. I should be able to talk to something that goes through these goals
and finds the other questions and decisions I need to make, and gets to a point
where it just needs to build, is satisfied with what it will do, and has all of
the ideas for the project."

Decided the same day: **the boss does the planning** (one agent to talk to), and
the planner is built **before the overseer**, which compares work against the
plan.

Stages: **plan** (boss + Eli) → **build** (`team run`, free workers, Claude on
escalation) → **watch** (overseer). Decisions found at any stage go to the same
inbox and the same plan.

## How a planning conversation goes

The procedure is the boss's skill `plugin/skills/plan/SKILL.md`.

1. **Eli gives a goal**, as rough as he likes.
2. **The boss studies before asking**: the target's code, docs, CLAUDE.md, its
   decisions log, the existing plan, and how Eli's other projects solve similar
   things.
3. **It splits the goal into parts**, each one a thing a worker can build and
   code can check. For each part: what it does, what "done" means (a check that
   can be run), what it depends on, what it assumes, what could go wrong.
4. **It answers what it can itself** (by reading, running, looking it up) and
   records those as assumptions Eli can see and overturn.
5. **It asks Eli the rest in small batches** (3 to 5 questions), each with context,
   options and a recommendation. Each answer may raise new questions; it
   follows them. This is how parts Eli didn't think of surface.
6. **It releases parts as they become ready**, and keeps planning the rest.
   Eli never has to finish planning everything before building starts.
7. **Adding to a goal later** goes through the same process: the boss works out
   which parts the addition touches and what it newly needs decided.

## Where a plan lives

Per target, in the state dir (nothing is added to the target repo):

- `$TEAM_STATE/targets/<name>/plan.md`: the narrative the boss writes and keeps
  current: goal, what done looks like for the whole, scope and out of scope,
  assumptions, risks.
- Ledger table `parts`: id, target, name, does, done_when (text), check (command
  or null: the target's check), depends_on (JSON list of part ids), status
  (`draft|ready|building|done|dropped`), goal_id (the `goals` row once
  released), created, updated.
- Ledger table `decisions` (`docs/OVERSEER.md`) gains `part_id` (nullable): an
  open decision on a part keeps it from being ready.

## Readiness, decided by code

`plan.ready(conn, part_id) -> (bool, reasons)`: a part is ready when it is
`draft`, `done_when` is not empty, no open decision names it, and every part it
depends on exists and is not `dropped`. It is **released** (a goal created for
`team run`) only when every dependency is `done`, so it branches from a main
that already has them. When `team run` lands a part's goal, the part becomes
`done` and its dependents are released on the next tick.

## Build (through `team goal team "..."` once `team run` lands)

**A. Decisions** — as in `docs/OVERSEER.md` step A, with `part_id`.

**B. Plan** — `team/plan.py`: `add_part(conn, target, name, does, done_when,
check=None, depends_on=())`, `update_part(conn, part_id, **fields)`,
`drop_part`, `ready`, `release(root, conn, target, part_id)` (creates the goal
through `team/run.py` with a brief built from the part, `plan.md` and the
decisions log), `release_ready(root, conn, target) -> list[int]`, `render(conn,
target) -> str` (markdown: plan.md, then parts by status with their open
decisions). `team run` calls `release_ready` each tick and marks parts `done`
when their goal lands. CLI: `team plan <target>` prints `render`; `team plan
<target> --release` releases now.

**C. The boss's planning skill** — `plugin/skills/plan/SKILL.md` (written by the
boss: it is the boss's own procedure), and a pointer to it in `boss.md`.

**D. Dashboard** — a **Plan** view per target (render, parts with status and
readiness reasons) and the **Inbox** (`docs/OVERSEER.md` step C), so Eli
answers decisions from the page or in conversation.

Then the overseer, `docs/OVERSEER.md` step B.
