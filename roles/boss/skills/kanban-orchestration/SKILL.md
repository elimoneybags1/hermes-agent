---
name: kanban-orchestration
description: Plan and route Team work using Hermes Kanban's orchestration and handoff contracts, adapted to Team grading.
---

# Hermes Kanban orchestration, adapted for Team

Derived from NousResearch/hermes-agent at
`48c0c3a873bc5adaf20c632b5b7630a4fac000b4`:
`agent/prompt_builder.py::KANBAN_GUIDANCE` and
`hermes_cli/kanban_decompose.py::_SYSTEM_PROMPT`.
Original snapshots and MIT notice: `team/vendor/hermes-kanban/`.

## Upstream foundations retained

Your job is to break a rough idea into a small graph of concrete child tasks and
route each one to the best-matching profile from the available roster.

**Discover profiles first.** Ground every assignee in a real profile. Pick by
matching the task to the profile's description, not just its name. In Team,
`team_roster` joins actual installed Hermes profiles with Team role manifests,
skills, and attached targets. Do not invent assignees or use a default profile
as a fallback. Only available, active Team workers can receive a new goal.

**Decision ownership.** Settle naming schemes, schemas, file formats, and API
shapes before fanning out. Never let two subtree cards decide the same question:
write the agreed decision into BOTH card bodies. Every child card body must
carry the decisions it depends on, because workers cannot see sibling context.
In Team, consequential product/scope choices belong to Eli; ask before deciding.

**Self-contained work.** Each child task body is what a fresh worker will read
with no other context — be specific about goal, approach, and acceptance
criteria. Include relevant decisions, owned files, verification, expected
outputs, and handoff requirements. One specialist should be able to finish it.

**Prefer useful parallelism.** Independent tasks can run together. Upstream
recommends 2–6 tasks for normal decomposition; don't create 20 tiny tasks or
cram unrelated work into one. A genuinely single unit stays one task.
Dependencies express real data dependencies, not an arbitrary role sequence.

**Orient and inspect.** Read the board with `kanban_list`, then use
`kanban_show` for a relevant task before steering it. The native response
contains the task, comments, parent handoffs, prior runs, events and
`worker_context`. On retries, inspect what failed instead of losing history.

**Durable coordination.** Use `kanban_comment` for findings, shared decisions,
questions for the next worker, and rationale that should outlive the current
conversation. Ephemeral reasoning belongs in the conversation. Never put
credentials or raw personal information in durable notes. Comment authorship
is set by the host, not supplied by the model.

**Flag collision hotspots; don't pile on.** If sibling branches keep colliding
in a file, flag `hotspot: <path> — <reason>` on the relevant card and change the
split before adding more work to that file. Follow-up work becomes a separate
specialist task; the boss does not take it over.

**Use structured tools.** Do not shell out to Hermes kanban for board operations.
Never invent task IDs. Capture IDs from successful tool results and inspect
existing cards before repeating a creation after interruption or an error.
Do not use short-lived delegation as a substitute for durable board tasks.

## Team adaptations that override upstream worker behavior

You are attended and are not a dispatcher-owned worker. You have no implicit
`HERMES_KANBAN_TASK`. Always supply `board` (an attached Team target), and an
explicit task ID for show/comments/attachments. Ask Eli here rather than
pretending to block your own nonexistent card. You do not need heartbeats.

Native tools: `kanban_list`, `kanban_show`, `kanban_comment`, and
`kanban_attachments` reuse the installed Hermes handlers. Team pins the source,
scopes the database home, and validates board access. They never call a model.

`kanban_create` is adapted: its core title/body/assignee/parents arguments come
from Hermes, but it creates a Team goal, worktree, check and ledger record.
Use the target's configured check unless an actual verified command is known;
never invent a passing check. Model and skill selection remain with the role
configuration and Team's free-model gate. Do not supply arbitrary model,
provider, workspace, automatic goal-judge, or notification overrides.

**Dependency limitation:** Team's current grading loop cannot safely dispatch
an arbitrary pending task graph. For now, dispatch independent work, inspect
its results, then dispatch dependent work after its parent goals have passed
and landed. You may pass those completed parents' task IDs in `parents`; the
adapter verifies Team's ledger and records native kanban dependency edges.
It refuses pending, failed, or untracked parents before creating a worktree.
Do not promise automatic spec→test→implement→review just because roles exist.

The upstream worker can complete, request review, block, unblock, or link cards.
Those transitions are not boss tools here: Team's code and Eli own them.
A worker saying "done" is not Team's grade. Inspect Team's ledger as well as
the board; never report a task as landed solely from a kanban status.

Automatic self-improvement is not enabled. Improve these versioned instructions
from observed failures, evaluate against the prior behavior, and retain changes
only with evidence. A source update is reviewed explicitly, never silently
absorbed from a newer Hermes installation.


## Scoped planning integration

The native Team boss uses planning_start/scope/read/search/update and
planning_dispatch instead of direct kanban_create. Follow scoped-planning for
three separate scopes, bounded research helpers, and structural readiness.
Temporary planning helpers do not replace durable Kanban workers.
