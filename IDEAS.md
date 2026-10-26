# Team: the ideas and the workflow

Eli's own notes, pulled together 2026-10-26 from a week of design
conversations, so the next version can be built on forked projects with these
ideas layered on top. This file is about what Team should be and how working
with it should feel. It is not about any particular code.

---

## The idea

An agent team you point at a repo. You talk to one agent, the boss. It plans
your goal with you until each part is ready to build, then hands the parts to
a line of defined workers on free models. Code, not a model, decides whether
each result is done. What passes lands. What keeps failing goes to Claude on
your subscription, and after that to you. Every attempt is a row in a ledger,
and the team gets better by proposing one change at a time and keeping it
only when the numbers say it helped.

The missing part everywhere else was the loop: outcomes → proposal →
evaluation → adoption. That loop is the point of Team.

---

## The principles

1. **The foreman is code, not a free-roaming agent.** A typed state machine
   with a fixed action set: dispatch, retry on the next model, escalate,
   propose a skill edit, propose a route change. A model is called only for
   narrow decisions, like classifying work into a role or diagnosing one
   failure into one proposed change. Precision comes from having few
   decisions, not from a smarter model.

2. **Workers are defined, not improvised.** A role is a manifest (what task
   class, which tools, what budget, how it is graded, when it escalates), a
   SOUL that says who it is, and skills. **No role exists until it has a
   grading check,** and no role is active until that check is enforced by
   code, not by prose in its instructions.

3. **Done is decided by code.** A worker's word that it finished means
   nothing. The target's check runs in the worktree against current main. No
   agent grades its own work.

4. **Free models do almost all the work.** Unattended work runs only on free
   models through an allowlist that fails closed, or on Claude Code through
   Eli's subscription, launched only by Team with every API-key variable
   removed from the environment. Never a paid API key. Never a runtime's
   default profile that holds paid keys. Never a fallback chain that can
   reach a paid provider.

5. **Claude is the escalation, not the engine.** After the free attempts a
   role allows, one Claude attempt in the same worktree. Claude in build mode
   never asks a question: it works inside its task, proves its work by the
   check, and writes anything that is Eli's to decide into a decision file
   instead.

6. **Self-improvement is propose → evaluate → adopt.** A change to a skill,
   prompt or route is kept only if it beats the current version's pass rate
   on a fixed replay set, reported next to that base rate. Adopted changes
   are git commits, so they can be rolled back. Nothing self-edits until the
   replay eval exists.

7. **Who may change what.** Free models may propose skill edits and routing
   changes. Code changes to Team itself go through Claude via `crew` and
   wait for `crew accept`.

8. **Models per stage are measured, not guessed.** Each role has candidate
   models. The choice for an attempt comes from the ledger (pass rate for
   that role and target, speed, scarcity of the subscription), with some
   exploration so a new free model gets real chances. Pinnable from the UI.
   Every choice is recorded with its reasoning.

9. **Everything an agent is lives in git.** Manifest, SOUL, skills, hooks. A
   change from the UI, from the boss or from the loop is one commit with a
   subject that says who changed what. History and revert per agent.

10. **State lives outside every repo.** `~/state/team/`: ledger, boards,
    profiles, plans, worktrees. Attaching Team to a project adds nothing to
    that project. One target per project; its profile, overlays, decisions
    and outcomes live under `~/state/team/targets/<name>/`.

11. **Build on what exists.** Hermes is the runtime and the clock: profiles,
    tools, skills, memory, sessions, compression, kanban, cron, delegation.
    Team is policy on top: targets, roles, the free gate, grading, landing,
    the ledger, the loop. When a forked project already does a thing, use it
    and bend it; do not rebuild it underneath.

12. **First target is Team itself.** Team is built by its own agents from the
    first job, and every change to it is a job the ledger records.

13. **Real numbers only.** A rate is always shown next to its base rate with
    counts (`3/7 · base 18/30`), never a bare percentage. Status in a
    markdown file is not evidence; a log with a timestamp, a database that
    grew, a process that exists is.

14. **Sandbox grows with risk.** Git worktrees per job today; Docker as the
    terminal backend once a worker runs untrusted code.

---

## The workflow

How a day with Team should go.

### Opening it

`work team` opens the herdr session: the boss on the left, the control room
on the right. The control room is a full-screen terminal app with tabs:
Pipeline, Inbox, Agents, Log, Runtime. The web page (`team ui`) is for long
edits: instructions, skills, settings, and the live Flow and Talk views.
Everything refreshes live from what is on disk.

### Giving a goal

Eli gives the boss a goal, as rough as he likes. The boss **studies before
asking**: the target's code, docs, CLAUDE.md, decisions log, existing plan,
and how Eli's other projects solved similar things. Its reads are scoped and
budgeted; it starts from filenames and a subsystem, never the whole repo.

It splits the goal into parts, each a thing a worker can build and code can
check: what it does, what done means as a runnable check, what it depends
on, what it assumes, what could go wrong. It answers what it can itself and
records those as assumptions Eli can overturn.

Then it **interviews Eli one question at a time**: context, numbered options,
its recommendation marked. Enter takes the recommendation; a number picks an
option; a sentence is an answer in Eli's words. Each answer may raise new
questions; that is how the parts Eli did not think of surface. Between
questions Eli can type `goal: <text>` to start another plan. No free-form
chat unless he asks for it.

Parts are **released as they become ready**; Eli never finishes planning
everything before building starts. Adding to a goal later goes through the
same process. The plan lives per target as a narrative plus the parts on the
kanban board with their dependencies.

### Building a part

Every part goes through the line, not through the boss:

```
boss ──► spec ──► test (write) ──► implement ×N ──► test (verify) ──► review ──► land
          │            │              (parallel)          │              │
          └── decision ┴──────────── Eli's inbox ◄───────┴──────────────┘
```

| Stage | Produces | Gate, checked by code |
|---|---|---|
| spec | a contract (interfaces, behaviour, how to verify for real) and a task list where every task names the exact files it owns and the tests that prove it | valid list, no file in two tasks, files inside the repo |
| test (write) | the tests, committed first | only test files changed; the new tests **fail**; all others pass |
| implement ×N | the code, one worker per task, in parallel | touched only its files; its tests pass; merges cleanly into the part's branch |
| test (verify) | evidence from running the real thing: CLI, server, browser | the full check passes; the session shows the commands were run |
| review | `land`, or `fix` naming the task and what is wrong, or a decision for Eli | every done-when criterion met with evidence; a fix goes back to the task it names |
| land | merge into the target's main, locally | clean tree, main checked out, no conflict |

Kanban holds the graph: one task per stage, parent links so a stage starts
only when the one before is done, one task per goal for its whole life. A
worker finishes by requesting review; Team is the reviewer, grades by code,
and either completes the task or comments the failure and reopens it. The
whole history of a goal is in one place.

Retries: a role allows N free attempts, each on a different candidate model
when there is one. Then one Claude attempt. Then the task blocks and Eli
decides. Dead workers and over-budget runs are caught by a watchdog, not by
waiting.

Spawning Claude for a part means: a worktree, a generated plugin for that
role, the role's tools only, a stop hook that refuses to stop while the tree
is dirty or the check fails, an edit hook that refuses files outside the task,
Eli's own deny rules kept, Eli's global instructions and plugins excluded.

### Decisions

One inbox. Decisions come from the boss while planning, from workers that hit
a choice that is Eli's, from reviews, and from the overseer. Each has the
question, the context, options, and a recommendation. An open decision pauses
the goal it belongs to and nothing else. Answers are logged per target and
go into every later brief and review for that target, so a decision is made
once. Eli answers from the deck, the page, the boss conversation, or
`team decide`.

What agents may decide alone, inside the scope Eli set: add a goal that
serves an existing goal, comment on a task to steer a worker. What must be
asked: scope changes, new dependencies or services, anything that costs
money, choices between designs Eli would notice, deleting work, a goal that
keeps failing after escalation.

### Watching

- **Flow**: columns are agents (boss, then each role in line order, then
  landed), cards are goals. A card shows its attempt, target, title, stage,
  the model it is running on right now, and a track lit as it passes the
  stations. Independent goals run at the same time and are visible at once.
  Click a card for every attempt, outcome, model and the plan it came from.
- **Deck**: pipeline strip with counts per stage, open goals, inbox, agents
  with pass `n/m · base`, the log of what Team did, the Hermes runtime
  (sessions, calls by provider, proposals).
- **Kanban board**: Hermes's own dashboard on Team's board, for tasks.
- **Grid**: a read-only JSON snapshot for the panel on the deck machine.
- The **overseer**, on a schedule per target: gathers what happened (code),
  one read-only review against the goals (model), applies what it may
  (code), and turns the rest into inbox decisions and a five-line summary.

### Changing the team

- Agents are files. From the page: edit any field, the instructions, a
  skill, the tools; each save is a commit shown live with a toast
  (`boss changed implement: added skill run-pytest`). Revert per commit.
- The boss proposes new agents while planning: name, what it does, model,
  tools, first skills, how it is graded, where it sits in the line. On yes
  it creates the files, generates the Hermes profile, assigns the parts.
  Agents useful to any project live in the shared roster; agents for one
  project live in that target's state and are layered over the roster.
- `team optimize` is a conversation with an advisory agent that compiles the
  evidence (ledger, board history, sessions, sources) and proposes concrete
  before/after changes with uncertainty and a validation plan. It applies
  nothing. Adoption goes through the replay eval.
- Code changes to Team itself: `crew up` one wall per agent, `crew review`
  against the repo's rules, tests, `crew accept`, merge, `crew down`,
  ledger row. The session runs a wall end to end without Eli; Eli gets only
  real decisions. **Never push to a remote without asking.**

### Recurring work

Per target, routines: a goal and an interval ("run the test suite and fix
what breaks", every 24h). Hermes cron creates the goal when it is due; the
loop does the rest.

### Attaching a project

`team attach <path>`: an onboarding pass reads the repo (CLAUDE.md, tests,
CI, language, conventions, the detected build and test commands) and writes
a target profile. Base roles get per-target skill overlays. Outcomes are
keyed by target, so the loop tunes each target separately.

---

## What to fork and build on

- **Hermes Agent** (Nous Research): the runtime. Profiles (one locked
  free-only worker profile, cloned per role), tools and toolsets, skills with
  on-demand loading, memory, sessions and export, compression, hooks (block
  paid routes, log the model actually used), cron, `delegate_task`, and the
  **kanban** subsystem: boards, tasks, parent links, worktree workspaces,
  dispatch, review column, reclaim, decompose, swarm, and its board UI.
- **hermes-agent-self-evolution** (Nous Research): their DSPy + GEPA
  pipeline that evolves skills from past sessions with gates and an eval
  set. The closest existing version of Team's loop; swap its PR step for
  Team's commit-against-base-rate gate.
- **GEPA** `optimize_anything`: the propose step, reflection on a free model.
- **brain's `llm.py`**: the proven free-only policy, ranked routing by
  quota/speed/latency, cooldowns, usage ledger. Copy it.
- Ideas, not code: TensorZero's function / variant / feedback model for
  choosing models per stage; mini-swe-agent as a bash-only baseline worker
  for free models weak at tool calling; Agentless's "sample N patches, keep
  the one that passes"; Symphony's ticket → isolated run → proof of work
  state machine; Claude Code's Explore and Plan subagents (read-only, own
  context, findings returned) and Codex's distinction between a plan and a
  goal that keeps its end state, verification and stop conditions.
- Later: Inspect AI if a replay runner outgrows a small script; Phoenix when
  traces need a viewer; Docker as the Hermes terminal backend.

Leave out: LiteLLM's defaults (its fallbacks reach paid providers; a locked
free-only catalog with a generated config and a spend guard is fine),
CrewAI and AG2, Langfuse, TextGrad, promptfoo, OpenHands and SWE-agent as
runtimes, and Hermes's own paid defaults (background tasks on `provider:
auto`, the MoA preset, curator rewriting skills without approval).

---

## Lessons to build in from the first attempt

- One runtime. Hermes is the repo; Team is a layer in it. Never a second
  agent loop under or over it.
- A rule a role must obey is a code gate, or the role is not active.
- A skill may only name tools and commands that exist.
- Budget the boss's context from the first line: scoped reads, a per-turn
  request and token bound, deferred tool and skill loading.
- Versioned role files must reach every worker of that role on every
  launch; learned profile files are a separate layer that never masks them.
- The loop gets its first number before any surface is built on it.
- Docs describe what runs; anything superseded is deleted, not annotated.
- Commit as you go. Half a system uncommitted is half a system lost.

---

## How to work with Eli

- Ask before assuming when scope is ambiguous, the target is unspecified,
  several approaches are valid, or existing work is at risk. Lay out the
  choices in two or three lines, then ask.
- Just do it for obvious fixes, repo conventions, trivial choices, and
  whenever he has said "do whatever you think".
- Never invent a number. Run the command or read the file.
- Verify what is running from processes and logs, never from markdown.
- No empty scaffolding: a directory appears with its first real file.
- Paths derive from `$HOME` or the script location, never a hardcoded user.
- No `cd` inside a shell command; use `git -C` and relative paths.
