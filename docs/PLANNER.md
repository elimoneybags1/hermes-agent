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
- **Parts are kanban tasks** on the target's board (`docs/KANBAN.md`, "Use
  kanban first"): created in `triage`, dependencies as kanban parent links, the
  role as assignee, the role's skills as `--skill`. The body holds what it does
  and its done-when. Kanban already holds, shows and orders them; Team adds no
  parts table.
- Ledger table `decisions` (`docs/OVERSEER.md`) gains `task_id` (nullable): an
  open decision on a part blocks that task (`block --kind needs_input`);
  answering it unblocks it.

## Readiness, decided by code

A part leaves `triage` (Team promotes it, and `team run` creates its goal on
that task) when its body has a done-when, no open decision names it, and its
role exists. Kanban itself holds a child until its parents are done, so a part
always branches from a main that already has what it depends on.

## New agents: the boss proposes them

While planning (or when Eli asks: "make an agent for X"), the boss looks at the
parts and asks whether the team has a worker suited to each. When a group of
parts needs something the existing roles do badly or don't cover (database
migrations, scraping, a UI kit, a specific API), it proposes an agent as a
decision in the inbox: name, what it does, model, tools, first skills, how it is
graded, where it sits in the lineup (`hands_to`, or as assignee for those parts).

On yes, the boss creates it with `team.agents` (one commit per file, shown live
in `team ui`), generates its `team-<name>` Hermes profile so it gets a lane on
the board and Eli can chat with it, and assigns it those parts. It starts as
`planned` and becomes `active` after its first graded pass. Agents useful to
any project live in `roles/` in this repo; agents for one project live in
`$TEAM_STATE/targets/<name>/roles/` (Decision 1) and are layered over the shared
roster for that target only.

## Build (through `team goal team "..."` once `team run` lands)

**A. Decisions** — as in `docs/OVERSEER.md` step A, with `part_id`.

**B0. `team run` on kanban's review column** — one kanban task per goal for its
whole life: workers finish with `kanban_request_review`; Team grades tasks in
`review` and calls `complete` (then lands) or comments the failure and calls
`request-changes`; escalation counts review rounds. `team goal` and parts pass
the role's profile, `--skill`s and `--parent` links. (`docs/KANBAN.md`.)

**B. Plan** — `team/plan.py` over kanban: `add_part(target, title, does,
done_when, role="implement", parents=())` (a triage task), `ready(conn, target,
task_id) -> (bool, reasons)`, `release_ready(root, conn, target) -> list[str]`
(promote and start a goal on each ready part), `render(conn, target) -> str`
(plan.md, then the board's parts by column with their open decisions). `team
run` calls `release_ready` each tick. CLI: `team plan <target>`.

**B2. Agents** — `team agent new <name>` (and `team.agents.create`): role.toml,
SOUL.md, first skills, grading line, status `planned`; `team agent profile
<name>` generates `team-<name>` from the locked `team-worker` profile. The
boss's skill covers proposing them.

**C. The boss's planning skill** — `plugin/skills/plan/SKILL.md` (written by the
boss: it is the boss's own procedure), and a pointer to it in `boss.md`.

**D. Dashboard** — a **Plan** view per target (render, parts with status and
readiness reasons) and the **Inbox** (`docs/OVERSEER.md` step C), so Eli
answers decisions from the page or in conversation.

Then the overseer, `docs/OVERSEER.md` step B.
