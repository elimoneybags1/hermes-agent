---
name: scoped-planning
description: Investigate only the relevant subsystem, save a concrete goal contract, and dispatch bounded work through Team's readiness gate.
---

# Goal-directed investigation

The goal determines what you read. Do not preload or summarize the whole project.
Use planning_start for the attached target and requested outcome. Its root
instruction files govern the work; its top-level filenames and saved subsystem
map are navigation hints, not evidence of current implementation.

Use planning_locate with meaningful filename patterns. Select a specific
subsystem with planning_scope, then use planning_read/search within it. Follow
interfaces, callers and tests only when they matter to this change. Read/search
results state coverage and pagination: a partial search is not proof of absence.
There are shared per-plan content/file budgets; do not create another plan merely
to evade an exhausted budget. Narrow the question or discuss a genuinely separate
goal. Never read credentials, dependency installs or repository metadata.

Keep three scopes distinct:
- inspection_scope: code and interfaces needed to understand the change;
- change_scope: paths the implementation may change;
- verification_scope: behavior/tests that must remain correct, potentially broader.

An expansion needs a concrete reason and observed evidence. Record it with
planning_scope before following the dependency. A helper cannot expand its own
scope. Consequential product/scope choices belong to Eli; ask rather than infer.

# Focused helpers

Use planning_helper only when a concrete independent question benefits from its
own context. Supply relevant paths within the current scope and an expected result.
Explorer investigates behavior, interfaces or tests. Critic challenges the draft
contract and split. Verifier assesses whole-goal acceptance evidence after work.
These helpers are short-lived, read-only, budgeted native Hermes children. They
cannot implement, dispatch, alter shared skills, or create more agents. Keep
straightforward investigation local instead of spawning helpers by default.

# Durable contract and dispatch

Save with planning_update. Each success criterion needs an id, observable behavior
and verification. Specify change_scope, verification_scope, exclusions,
stop_conditions, and decisions (question/answer; unanswered decisions block readiness).
Do not invent an answer or treat your own preference as Eli's decision. If your
reply identifies a consequential unconfirmed choice, it must also appear in the
saved decisions with an empty answer, so the plan is not marked ready.

Each task needs id, title, body, an actual active worker role, exact owned file paths,
acceptance criterion IDs, a concrete check command and depends_on IDs. Give a
fresh worker enough context to work without guessing. Do not generate speculative
full implementations or arbitrarily tiny tasks. Split by meaningful subsystem or
interface boundaries, not by reading every directory. Avoid overlapping ownership. Read each existing owned file before readiness. A new
file outside the current inspection roots needs a scope_reason and inspected
file evidence in its task; do not expand inspection to nonexistent files.

planning_view reports structural readiness and stale inspected source. Readiness
is not proof that the plan is good or that Eli approved a new scope. Use a critic
for complex interfaces or uncertainty, and ask Eli only for material unresolved
choices. /plan is planning-only: save the contract and report it; do not dispatch.

Use planning_dispatch for execution. The default boss has no direct kanban_create
bypass. Dependencies are dispatched only after their parents pass and land.
Each worker receives a scoped plan slice and the goal's acceptance context in its
brief; source role files are not rewritten. Task checks do not change the project's
default check. A plan with dispatched tasks keeps its contract; changed scope
requires a follow-up plan rather than rewriting the existing assignment.

# Completion and reuse

After work, inspect planning_view's actual linked outcomes and acceptance evidence.
Use a verifier when useful, then planning_assess to distinguish met, unmet and
unknown criteria. Met criteria must cite inspected evidence and landed goals.
This is a model assessment, not an independent execution of tests or automatic
permission to close/merge anything. Never equate all task passes with whole-goal success.

Save small subsystem navigation hints with planning_remember: responsibilities,
entry points, interfaces and test locations backed by observed evidence. Future
work must verify relevant source again. Do not store a repository dump.


# Minimal worker context

Task bodies are precise work instructions: current behavior, requested change,
relevant interfaces, and what constitutes completion. Include optional
`recommended_toolsets` (names from the role manifest) and `required_skills`
(exact SKILL.md paths under that role's skills or vendor/hermes-agent/skills).
Select only procedures needed for this assignment; do not list the library.
The library stays installed and workers load relevant skills on demand.
These fields guide selection; they do not restrict runtime tool permissions.
Never ask workers to read the agent runtime source to learn available tools.
Keep connected edits together when splitting them would repeat the same context.
