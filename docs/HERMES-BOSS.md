# Boss foundation and provenance

The base is the **full Hermes Agent**, including its Kanban subsystem, not a
separate "Kanban agent" reconstructed from a few markdown excerpts.

Fork: https://github.com/elimoneybags1/hermes-agent

Pinned upstream commit: `48c0c3a873bc5adaf20c632b5b7630a4fac000b4`.
Full source, upstream documentation and MIT license: `vendor/hermes-agent/`.
Earlier selected-source snapshots in `team/vendor/hermes-kanban/` remain for
historical provenance; the running agent imports the full fork.

## What goes into a boss model request

| Element | Source / purpose |
|---|---|
| Base system prompt and tool-specific guidance | Native Hermes prompt builder and AIAgent; general agent behavior, tool usage, runtime context |
| Full `SOUL.md` | Upstream file seeded into boss profile; identity and communication guidance, retained for local tuning |
| Native skill index | Full upstream library plus Team role skills; descriptions enter context, full skills are loaded on demand |
| Memory and user profile | Native profile `memories/`; persistent notes injected by Hermes |
| Project context | Native Hermes context discovery, governed by its configuration and working directory; repository instruction files may enter context |
| Full Kanban guidance | `agent/prompt_builder.py::KANBAN_GUIDANCE`, explicitly included |
| Team boss overlay | `plugin/agents/boss.md`: attended clarification, role boundaries, evidence, no independent implementation or landing |
| Team orchestration skill | `roles/boss/skills/kanban-orchestration/SKILL.md`: inherited Kanban/decomposer rules plus Team adaptations; included explicitly |
| Tool definitions | Native registry for enabled toolsets, with native deferral/discovery when applicable |
| User request, history, results | Native Hermes session history and tool results; compressed by Hermes when needed |
| Transport encoding | Team adapter adds JSON response/tool-request serialization instructions and carries the complete supplied schemas/history |

A markdown file being in the fork does not mean the entire file is transmitted
every turn. Hermes uses selective context and progressive skill loading.
`AGENTS.md` documents development/conventions; it is not the entire agent.
The actual payload depends on role, session, working directory and invoked tools.

## Native orchestration and Team adaptations

Hermes supplies the board database, structured tools, task context, comments,
handoffs, dispatch machinery and profile-based workers. Its planning guidance
requires concrete tasks, actual profile discovery, shared decisions written into
child briefs, explicit dependencies and durable handoffs.

Team adds target/role manifests, free-model policy, subscription escalation,
worktrees, check commands, an outcomes ledger and guarded landing. Boss tools
include scoped target reads and inspection, native board list/show/comment/
attachment operations, plus roster discovery and scoped planning tools. Direct creation is replaced by
`planning_dispatch`, which validates a saved goal contract. Creation
must satisfy Team's supported grading contract. The boss cannot launch arbitrary
agents, approve work, merge branches or answer Eli's outstanding decisions.

Native skills, memory, session search, todo and clarification are enabled for
the boss. Worker execution toolsets come from their role manifest, with native
learning/context tools retained. Importing all upstream tools does not grant
every role every capability.

Team's current goal/grading adapter still limits which task/dependency shapes
it can accept; importing the full runtime does not by itself implement every
upstream dependency-graph workflow. The native decomposer's policy is a source
for Team's orchestration skill, not an additional automatically launched planner.

## Evolution

Upstream files remain intact in the fork. Team adaptations live outside it.
Role-local skills, memory and personalized SOUL persist across launches; profile
sync does not erase existing skill edits. Updating the pinned upstream source
does not silently overwrite learned profile content. Reconcile upstream updates
with role-local changes deliberately.

Worker learning is enabled, but shared behavioral changes still require evidence
against a baseline before promotion. Free and subscription workers currently
retain separate role profiles. There is no automatic cross-profile learning merge.

See [HARNESS.md](HARNESS.md) for transport limits, state locations and usage.

## Scoped planning integration

See [SCOPED-PLANNING.md](SCOPED-PLANNING.md) for the current planning tools,
read budgets, persistent contracts and host-constrained native helpers. The boss
no longer exposes legacy unscoped read/list or direct kanban_create. `/plan`
adapts Hermes’s native planning prompt and prevents task dispatch for that turn.
