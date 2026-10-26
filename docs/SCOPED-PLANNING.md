# Scoped planning in the Hermes boss

The boss now investigates the area relevant to a goal, saves a structured contract,
and dispatches workers through a readiness gate. It uses the existing Hermes
runtime and native child lifecycle. No upstream source is modified.

## Use

Start `team` and describe the goal and attached target normally. For a planning-only
turn, enter `/plan <goal>`; one-shot usage is `team -- --prompt "/plan <goal>"`.
The boss adapts Hermes's existing plan prompt to Team's structured state tools.
`planning_dispatch` and board comments are blocked for that planning-only turn.
Project writes and terminal execution are absent from the boss's tool surface.
Legacy unscoped read/list tools are also removed; Team-source investigation uses
the same scoped tools as any other attached target.

The model still owns interpretation and judgment: structural readiness does not
prove a plan is correct, a scope is relevant, or a human decision was authorized.

## Investigation

`planning_start` creates a goal record for an attached target. It reads root
instruction files and returns top-level names and cached subsystem hints. It does
not index all file contents. `planning_list` resumes recent plans for a target.

`planning_locate` searches filename metadata with a meaningful pattern.
`planning_scope` selects specific inspection roots; project-root scope is refused.
Later expansion must have a reason and cite previously observed evidence.
`planning_read` and `planning_search` enforce those roots, exclude common secret,
dependency and runtime paths, reject symlinks, and apply shared per-plan budgets.

Default engineering limits: 30 distinct files, 250,000 content bytes per plan,
12,000 bytes per read page. Search admits exact files within those budgets and delegates matching to the upstream
Hermes search engine, returning at most 40 matching excerpts with read offsets. It reports partial coverage; absent matches
are not proof of absence.
Instruction files count toward budgets; unchanged governing instructions may be
referenced by evidence ID after being supplied once per Planner instance; direct
instruction reads remain available. Splitting a genuinely separate goal is legitimate; creating
new plans simply to evade a budget is prohibited by the boss's instructions.

The contract separates:
- **Inspection scope:** code/interfaces that need understanding.
- **Change scope:** paths the proposed implementation owns.
- **Verification scope:** checks and behavior to verify, which may be broader.

`planning_remember` saves small subsystem navigation hints with source evidence.
These are explicitly marked for revalidation. They are not whole-repository
summaries and never replace inspection of relevant current code.

## Native helpers

`planning_helper` offers Explorer, Critic and Verifier. A helper gets a concrete
question and a snapshot of allowed roots within the plan. It can only call
planning_view/locate/read/search for its assigned plan. It cannot expand scope,
write project files or skills, mutate the board, dispatch, or spawn children.

`team/planning_delegate.py` explicitly constructs a native Hermes AIAgent, uses
Hermes's child prompt/lifecycle/lineage support, and audits effective/deferred
tools. It bypasses the upstream construction wrapper because that wrapper reads
ambient workspace instructions before child context limits are applied. It suppresses
ambient project-context loading and memory, preserves the subscription gateway,
and disables fallback credentials and background review. Scope ContextVars carry
through native tool threads. Default helper bounds are 8 iterations, 120 seconds,
and at most 2 concurrent helpers in the boss process. A timed-out request is
interrupted; an in-flight CLI request may finish later, and retains its slot.

The helper may return an expansion request to the boss. It does not expand its
own scope. The whole-goal Verifier examines recorded evidence; it cannot execute
tests and is not itself a proof of correctness.

## Contract, dispatch and completion

`planning_update` saves revisions of a structured draft. Readiness checks require
acceptance criteria, verification scope, owned paths, explicit decisions,
stop conditions, active worker roles, covered criteria and non-overlapping task
ownership. Cyclic/invalid dependencies and changed inspected source block dispatch. Existing
owned files must be in scope and individually inspected. A new file outside the
inspection roots needs an explicit scope reason and inspected source evidence.
Freshness checks use recorded file metadata; helper views do not inspect metadata
for files outside their assigned scope.
Planned Spec/Test/Review roles are not silently activated by this integration.

The native boss's direct `kanban_create` tool is replaced by `planning_dispatch`.
This creates one task through Team's existing guarded worktree/ledger path, with
a scoped contract slice in its brief. Each slice includes only its assigned
acceptance criteria and relevant inspection roots; connected interfaces can be
named explicitly in a task's inspection_scope. Dependencies must already have passed and
landed. Repeated calls reuse the recorded goal; task markers allow recovery when
creation preceded a process interruption. A dispatched contract cannot be silently
rewritten: changed work goes into a follow-up plan.

A task-specific check now stays on its goal instead of replacing the target's
default check. Workers receive file ownership instructions. For scoped contracts, grading rejects
changes outside owned paths before landing, including both sides of a rename.
Legacy goals without a contract retain legacy grading.

`planning_assess` records met/unmet/unknown acceptance criteria. A met claim must
cite inspected evidence and linked goals that passed and landed. The code verifies
references, not semantic truth. No model assessment automatically merges work or
closes a goal. Task outcomes and whole-goal assessments remain distinct.

## State and other roles

Plans, revisions, evidence metadata, read budgets, expansion reasons, helper
results, task links and assessments live in `$TEAM_STATE/planning/<UUID>.json`.
Subsystem maps live in `$TEAM_STATE/targets/<target>/project-map.json`. Nothing
is written into a target repository by these planning tools.

Both free and subscription workers receive the scoped task brief; their existing
learned profile files are preserved. The Optimizer's evidence collector now lists
recent plans and exposes their saved evidence as paginated sources, allowing it
to examine scope expansion and planning outcomes.

See ROLE-PLANNING-REVIEW.md for specific remaining improvements to Implement,
Spec, Test, Review and Optimizer. Only shared planning context propagation and
Optimizer evidence access are integrated here; specialist stage gates, automatic
activation remain incomplete. Scoped owned-file enforcement is implemented.
