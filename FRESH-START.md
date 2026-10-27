# Fresh start: everything Team wanted to be, and what got built

Written 2026-10-26 from this repo's docs, code, git history and the ledger at
`~/state/team/team.db`. This is the carry-over document for the rebuild. The
new repo is a **fork of the Nous Research projects with Team's work layered on
top**, not Team's code with Nous's work vendored underneath. Everything below
is either a decision Eli made (dated), a thing that exists (with evidence), or
a thing that was only designed. Nothing in here is a claim about what runs
right now; check processes and logs for that.

---

## 1. The idea, in one paragraph

A repo-agnostic agent team you point at a repo. A **boss** Eli talks to plans
goals with him until each part is ready to build. A **foreman that is code**
dispatches parts to **defined worker roles** on free models, grades the result
by running the target's check, lands what passes, escalates to Claude Code on
Eli's subscription when free attempts run out, and brings Eli only real
decisions through one inbox. Every attempt is a row in an outcomes ledger, and
a **self-improvement loop** proposes one change at a time, keeps it only if it
beats the current pass rate on a replay set, and lands it as a git commit.
Origin: `docs/ORIGIN.md` (2026-10-18).

---

## 2. What to fork, what to copy, what to leave out

### Fork from Nous Research (the base of the new repo)

| Project | Why | Pinned today |
|---|---|---|
| `NousResearch/hermes-agent` | The runtime: `AIAgent`, prompt builder, tools, skills, memory, sessions, compression, profiles, cron, `delegate_task`, the **kanban** subsystem (`hermes_cli/kanban_db.py`, `kanban_decompose.py`, `tools/kanban_tools.py`, `KANBAN_GUIDANCE` in `agent/prompt_builder.py`) and its board UI (`plugins/kanban/dashboard`) | Fork `elimoneybags1/hermes-agent`, upstream commit `48c0c3a873bc5adaf20c632b5b7630a4fac000b4`, MIT |
| `NousResearch/hermes-agent-self-evolution` | Nous's own DSPy + GEPA pipeline that evolves Hermes skills from past sessions with gates and an eval set. The closest existing version of Team's propose → evaluate → adopt loop. Swap its PR step for Team's commit-and-base-rate gate | Not yet pulled; verdict "adopt the pattern" in `docs/TOOLS.md` |

Upstream files stay intact in the fork. Team's additions live beside them, so
upstream can be merged in later.

### Borrow ideas from (not code)

- **GEPA** `optimize_anything` for the propose step (reflection model set to a
  free one).
- **TensorZero's** function/variant/feedback idea for choosing models per
  stage (the project itself is archived and needs Postgres: no).
- **mini-swe-agent** as a bash-only baseline worker for free models weak at
  tool calling; **Agentless** for "sample N patches, keep the one that passes";
  **OpenAI Symphony** SPEC.md as a reference for the job state machine.
- **Inspect AI** later, if a small replay runner over the ledger outgrows itself.
- **Arize Phoenix** when traces need a viewer (SQLite, OTLP; check the port
  clash with `com.fullmoney.otlp-receiver`).
- Claude Code's Explore/Plan subagent pattern and Codex's Plan vs Goal
  distinction (`reports/Planning and delegation foundations.md`,
  `research_notes/`).

### Leave out

- **LiteLLM as-is**: its fallback chains route to paid providers. Team did
  build a LiteLLM proxy locked to free models (`litellm/`, 2026-10-26, see
  §5); keep the catalog + generator + spend guard idea, not LiteLLM's defaults.
- CrewAI / AG2 role-chat frameworks, Langfuse (needs Postgres + ClickHouse),
  TextGrad, promptfoo, OpenHands/SWE-agent as runtimes.
- Hermes's own paid defaults: background tasks on `provider: auto` with
  `auxiliary.free_only: false`, the paid MoA preset, curator/background review
  rewriting skills with no approval. All must be pinned or disabled in every
  Team profile (`docs/TOOLS.md`, "Paid-model risks").

---

## 3. Decisions that still hold (Eli, dated)

These are rules for the rebuild, not suggestions.

1. **The foreman is code, not a free-roaming agent.** A typed state machine
   with a fixed action set. A model is called only for narrow decisions
   (classify work into a role; diagnose a failure into one proposed change).
2. **Workers are defined, not improvised.** A role = manifest (`role.toml`:
   task class, tools, budget, how graded, escalation) + `SOUL.md` + skills.
   **No role exists until it has a grading check.**
3. **Self-improvement is propose → evaluate → adopt.** A change is kept only
   if it beats the current pass rate on a fixed replay set, reported next to
   the base rate. Changes are git commits. Nothing self-edits until the replay
   eval exists.
4. **Who may change what.** Free models may propose skill edits and routing
   changes. Code changes go through Claude via `crew` and wait for
   `crew accept`.
5. **Unattended work runs only on free models or on Claude Code through Eli's
   subscription** (2026-10-24). Claude Code is launched only by Team with
   `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_BASE_URL`,
   `CLAUDE_CODE_USE_BEDROCK`, `CLAUDE_CODE_USE_VERTEX` removed. Never a paid
   API key, never the default Hermes profile, never MoA. The free gate is an
   allowlist that **fails closed**.
6. **State lives outside every repo**, at `~/state/team/` (`$TEAM_STATE`).
7. **Team manages every project from outside it** (Decision 1, 2026-10-19).
   Per-target profile, overlays, plans, specs, decisions, worktrees all live
   under `~/state/team/targets/<name>/`. Attaching adds nothing to the target.
8. **First target is Team itself.** Every change to Team is a job the ledger
   records.
9. **Sandbox: crew worktrees only** until a worker runs untrusted code; then
   Docker via colima.
10. **Hermes is the runtime and clock, not the brain.** It schedules the
    foreman and runs workers.
11. **Team works autonomously on Hermes kanban** (2026-10-24). Kanban is the
    queue and runner in Team's own kanban home
    (`HERMES_KANBAN_HOME=$HOME/state/team/kanban`, one board per target); Team
    keeps grading, the ledger, escalation and the model rule. **"Use kanban
    first: this is an already built project; use most of what it has and
    build on top of it."**
12. **Plan, build, watch** (2026-10-24). Boss plans with Eli until parts are
    ready; `team run` builds; an overseer watches. Planning is built before
    the overseer.
13. **Every goal goes through the team, not through the boss** (2026-10-24):
    spec → test (write) → implement ×N → test (verify) → review → land. No
    agent grades its own work.
14. **Models per stage are measured, not guessed** (2026-10-24): candidates per
    role, chosen from the ledger by pass rate, speed and cost; pinnable.
15. **Eli talks to one agent, the boss**, started by plain `team`
    (2026-10-19). Its role, tools and rules live in git.
16. **The full Hermes runtime is the base, Team policy on top** (2026-10-25).
    Native `AIAgent`, with a local adapter using the tool-free Claude CLI for
    subscription inference only.
17. **The session runs each crew wall start to finish without Eli**; bring
    him only real decisions. **Never push to a remote without asking.**
18. Conventions from the global CLAUDE.md: real numbers only (run the command),
    a rate always next to its base rate, verify running state from
    logs/processes not markdown, no empty scaffolding, portable paths from
    `$HOME`, no `cd` inside Bash commands.

Still open: overlap with `~/work/platform/ai` (gateway, router, agents,
skills). Nobody has read it. Needs a session in `platform`.

---

## 4. The system as designed (what we wanted)

Each item names the doc that holds the full contract. These docs are worth
carrying into the new repo largely as they are; the code under them is not.

### 4.1 Boss and planning

- **Boss** (`plugin/agents/boss.md`, `roles/boss/`): the agent Eli talks to.
  Plans, grades, lands; never implements. Proposes new agents when parts need
  a worker the roster lacks (`docs/PLANNER.md`, "New agents").
- **Planner** (`docs/PLANNER.md`): studies the target before asking; splits a
  goal into parts, each with what it does, a done-when a check can run,
  dependencies, assumptions, risks; answers what it can itself and records it
  as an overturnable assumption; asks Eli the rest in batches of 3 to 5 with
  options and a recommendation; releases parts as they become ready. Plans
  live at `$TEAM_STATE/targets/<name>/plan.md`; parts are kanban tasks with
  parent links.
- **`team talk`** (`docs/TALK.md`): Team interviews Eli one question at a time
  with numbered options and a recommendation; Enter takes the recommendation;
  `goal: <text>` starts a new plan; planner turns resume one Claude session.
  Ledger table `plans`. **Never built.** The spec role's `plan-a-goal` skill
  still refers to it.
- **Scoped planning** (`docs/SCOPED-PLANNING.md`, built 2026-10-26): the boss
  investigates only the relevant subsystem through `planning_start / locate /
  scope / read / search / remember / update / helper / dispatch / assess`
  tools with read budgets (30 files, 250 KB per plan, 12 KB per page), saves a
  persistent goal contract (inspection scope, change scope, verification scope,
  acceptance criteria, decisions, stop conditions, owned paths per task), and
  dispatches through a readiness gate. Bounded native Explorer / Critic /
  Verifier helpers (8 iterations, 120 s, 2 concurrent). State in
  `$TEAM_STATE/planning/<uuid>.json` and
  `$TEAM_STATE/targets/<name>/project-map.json`.

### 4.2 The pipeline and the run loop

- **The line** (`docs/PIPELINE.md`): boss → spec → test (write, tests must
  fail) → implement ×N in parallel (each owns exact files) → test (verify
  against the real thing) → review (land / fix / decision) → land. Each stage
  is a goal; gates are commands (`team gate <stage> <part_id>`); one kanban
  graph per part with parent links; branches `team/<part>` and
  `team/<part>--<n>`; spec files under `$TEAM_STATE/targets/<name>/parts/<id>/`.
  Ledger table `parts`; `goals` gains `part_id`, `base`, `needs_commits`.
  Settings `pipeline.max_parallel_impl`, `pipeline.enabled`.
- **`team run`** (`docs/RUN.md`, `docs/KANBAN.md`): each tick, per target:
  dispatch; grade tasks in kanban's `review` column by merging main into the
  branch and running the target's check; pass → `complete` + land (`--no-ff`,
  refuse on dirty tree or non-main); fail → comment + `reopen-review`, attempt
  + 1; after the role's `after_failures`, one Claude attempt in the worktree;
  then `block --kind needs_input` for Eli. Watchdog for dead pids and
  over-budget runs. One line per tick on stdout. `goals` table so the loop can
  restart and carry on. `calls` row per attempt from the Hermes session;
  refuse to record a non-free model.
- **`team goal <target> "<goal>"`**: the one-task path. `team part` was to
  replace it for planned work.
- **Claude in build mode** (`docs/RUN.md`, built 2026-10-25): a Claude Code
  plugin generated per role under `$TEAM_STATE/claude/<role>/` (agent file =
  SOUL.md + build rules, skills copied, hooks); `team hook stop` refuses to
  stop while the tree is dirty or `$TEAM_CHECK` fails (max
  `$TEAM_STOP_RETRIES`); `team hook pretool` refuses edits outside
  `$TEAM_ALLOWED_FILES`; decisions written to `$TEAM_DECISION_FILE` go to the
  inbox; isolation from Eli's own setup via `--setting-sources project,local`,
  a rewritten settings file keeping only his deny rules and confine hook, and
  `claudeMdExcludes` for his global CLAUDE.md. Verified with a real run.
  **Superseded** on 2026-10-25 by the native-Hermes transport (`docs/HARNESS.md`).
- **Routines**: per target, recurring goals on an interval ("run the test
  suite and fix what breaks", every 24h). Moved to Hermes cron creating the
  goal. Never built.

### 4.3 Models and the free gate

- **Free gate** (`team/free.py`): static reading of every `hermes` command;
  refuses anything that could reach a non-free model; `team-*` profiles only
  hold the OpenRouter key; fails closed on anything it cannot read. Hooked as
  a `PreToolUse` on Bash for the boss (`plugin/hooks/hooks.json`).
- **Model choice per role** (`docs/MODELS.md`, `team/models.py`): `role.toml
  [models] candidates / pinned`; Thompson sampling on Beta(passes+1, fails+1)
  from the ledger (this target ×3, last 60 days) minus speed and cost terms
  (`models.claude_cost`, `models.minute_weight`, `models.explore`); retries
  pick a different candidate; every choice recorded. Built; **never wired into
  `team run`**.
- **LiteLLM auto-router** (`litellm/`, 2026-10-26): `free-models.json` is the
  researched catalog (why each model is free, blocked providers: anthropic,
  minimax, dashscope); `build.py` generates `config.yaml` and refuses anything
  not free; tiers `tier-simple / medium / complex / reasoning` with ordered
  deployments; `spend_guard.py` trips on any OpenRouter credit movement and
  refuses everything until a person looks; `session_pin.py` keeps one job on
  one model (LiteLLM's own affinity skipped mixed tiers); `usage_log.py` →
  `$TEAM_STATE/litellm/usage.db` joined to jobs by kanban task id;
  `hermes_profiles.py` points `team-*` profiles at `127.0.0.1:4000` as
  `team-auto`; launchd installer. Workers compress at ~32K.
- **Claude subscription transport** (`team/claude_transport.py`,
  `team/subscription_gateway.py`): a loopback chat-completions adapter that
  runs `claude -p` with tools, slash commands, MCP, settings and persistence
  off and the Hermes prompt supplied explicitly; verifies `claude.ai` login
  first; text only, buffered, no vision, no real streaming. Per-turn bounds:
  12 requests, 200 KB per request, stop after 120K reported input tokens.

### 4.4 Oversight and decisions

- **Decisions** (`team/decisions.py`, `docs/OVERSEER.md` A): table
  `decisions` (question, context, options, recommendation, goal_id, task_id,
  status, answer, source); `team decide list|answer|dismiss`; answers append
  to `$TEAM_STATE/targets/<name>/decisions.md`, which goes into every brief.
  An open decision on a goal pauses it. Built.
- **Overseer** (`docs/OVERSEER.md` B): on a schedule, gather (code) → one
  read-only Claude review with a JSON schema (summary, decisions, actions,
  drift) → apply (code). May add a goal or comment a task; must ask about
  scope, dependencies, money, design choices, deletions. Table `reviews`.
  Settings `oversee.*`. **Never built.**
- **Optimizer** (`docs/OPTIMIZER.md`, built 2026-10-26): `team optimize`, an
  advisory Hermes agent on the subscription that compiles ledger/board/session
  evidence, cites what it retrieved, and saves before/after proposals with
  uncertainty and a validation plan to `$TEAM_STATE/optimizer/proposals/`. No
  terminal, no writes, no dispatch, no auto-adoption.
  **This is the only piece of the self-improvement loop that exists**; the
  evaluate and adopt steps do not.

### 4.5 Surfaces

- **`team ui`** (`docs/UI.md`): stdlib server on `127.0.0.1`, one HTML file,
  live via SSE on a fingerprint. Views: pipeline strip, agent list and pane
  (overview fields editable, instructions, skills add/edit/delete, tools
  checkboxes, hooks, jobs, history with revert; every edit is one git commit
  `<agent>: <what> (<source>)`), **Settings** (orchestration.toml fields,
  per-target `check` and `enabled`), **New agent** + profile sync, **Inbox**
  (decisions), **Runtime** (Hermes profiles, sessions, model calls by
  provider, optimizer proposals), **Flow** (columns are agents, cards are
  goals with a station track, styled after the Hermes kanban board), **Talk**
  (one live boss process behind the page, session resumed from
  `$TEAM_STATE/boss/ui-session.json`).
- **`team deck`** (`docs/DECK.md`): Textual control room for the herdr pane
  to the right of the boss: Pipeline, Inbox, Agents (chat with any `team-<role>`
  profile), Log, Runtime tabs; `g` new goal; `team open` lays it out.
- **`team dash --json`** (`docs/DASH.md`): read-only snapshot for the `grid`
  panel in `~/work/deck`; pass rates always with counts and the base rate.
- **`team board`**: the Hermes kanban dashboard on Team's board.
- **Agents as files** (`team/agents.py`, `team/profiles.py`): everything an
  agent is lives in `roles/<name>/`; `team agent new|sync` generates the
  `team-<role>` Hermes profile cloned from the locked `team-worker`, free model
  only, `max_turns` written from the manifest.

### 4.6 Targets and the ledger

- **Targets** (`team attach`, `team targets`): `$TEAM_STATE/targets/<name>/
  target.json` (path, commit, check, enabled). Attached today: `team`, `brain`.
  Planned and never built: an **onboarding pass** that reads the repo
  (CLAUDE.md, tests, CI, language, conventions; `hermes verify --detect-only`)
  and writes a target profile with per-target skill overlays.
- **Ledger** (`team/ledger.py`, SQLite at `$TEAM_STATE/team.db`): tables
  `jobs` (target, target_commit, role, role_version, attempt, started, ended,
  outcome `pass|fail|error|timeout`, grader, grader_detail, task_ref),
  `calls` (job_id, provider, model, task_class, tokens, latency, ok, error,
  ts), `goals`, `decisions`, `pauses` (free-quota pauses). Outcome comes from
  the grading check, never from a model. No cost column: the gate refuses paid
  models. Designed but absent: `reviews`, `plans`, `parts`, `jobs.model_choice`.

---

## 5. What exists today, and its state

### Ledger, as of 2026-10-26 20:28 UTC

| | count |
|---|---|
| jobs | 78 (45 pass, 23 fail, 6 error, 3 timeout, 1 unfinished) |
| by role | implement 66, boss 9, builder 3 (the first crew walls) |
| target | all `team` |
| calls | 29 |
| goals | 20 |
| decisions | 0 |
| optimizer proposals saved | 2 |
| saved plans | 2 |

Pass rate 45/77 finished. First job 2026-10-20, last 2026-10-26.

### Git

141 commits on `main` from 2026-10-19 to 2026-10-26, then two archive commits
made for this reset (see §8). Before them the working tree held an
**uncommitted 25-file diff (+1743 / −743) and 44 untracked paths**: the whole
native-Hermes runtime, scoped planning, optimizer, flow view, boss chat,
subscription gateway, and 14 test files. All of it is now in commit `1e3291f`.

Tests: 647 `def test_` across 37 files; `docs/NATIVE-VERIFICATION.md` reports
706 passing on 2026-10-26 (not re-run for this document).

### Status per piece

**Built and used for real (ledger or verification evidence):**

- Outcomes ledger, `team attach / targets / ledger / roles / dash --json`.
- Free gate (`team/free.py`) and the `team-worker` locked profile.
- `team goal` + `team run`: kanban dispatch, grade by check, land, retry,
  Claude escalation, watchdog, review-column flow, free-quota pause,
  one-line tick log. 66 implement jobs went through it.
- `team ui` with agents, settings, inbox, new-agent, runtime, flow and talk
  views; `team deck`; `team open`; `team board`.
- Decisions table and `team decide`.
- Agents-as-files with one commit per change; `team agent new|sync`.
- Claude build mode (plugin per role, stop/edit gates, isolation): verified by
  a real run (job 60), then superseded.
- LiteLLM free-only proxy with tiers, spend guard, session pin, usage log.
- Native Hermes boss with scoped planning, helpers, contract gate,
  `planning_dispatch`; scoped owned-file enforcement at grading; task packets
  with `recommended_toolsets` / `required_skills`.
- Subscription adapter: passed native tool use, forced compression and
  continuation (`reports/native-*.json`).
- `team optimize` with saved proposals.
- End-to-end probe: contract → kanban dispatch → native free worker → commit →
  review → grade → local land (`reports/team-native-end-to-end.json`, 52,838
  input tokens, 6 calls).

**Built but never activated or never wired in:**

- `team/models.py` `choose()`: not called by `team run`.
- Roles `spec`, `test`, `review`: manifests, SOUL.md, skills and profiles
  exist; status `planned`; `hands_to` does not drive any stage transition.
  The multi-stage pipeline (`team/pipeline.py`, `team part`, `team gate`,
  `parts` table) was never written.
- `team/harness.py`, `claude_run.legacy_run`, `team/claude_roles.py`: the
  pre-Hermes Claude loop, kept as compatibility code.
- `team/vendor/hermes-kanban/`: the first partial vendoring (5 files with
  hashes) before the full fork; historical only.

**Designed only (doc exists, no code):**

- `team talk` and the `plans` table (`docs/TALK.md`).
- The overseer and `reviews` table (`docs/OVERSEER.md` B).
- Routines / recurring goals via Hermes cron.
- Target onboarding pass and per-target skill overlays.
- The replay set, the evaluate step and the adopt step of self-improvement
  (GEPA / self-evolution pattern).
- Per-target roles layered over the shared roster
  (`$TEAM_STATE/targets/<name>/roles/`).
- The grid panel on the deck side (`docs/DASH.md` is the brief).
- Docker terminal backend for code-running workers.
- `team swarm`-style fan-out for research and audit goals.

---

## 6. Why it felt broken: what the record shows

These are the things to design out of the fresh start.

1. **The stack was inverted twice.** First Team built its own harness around
   `claude -p` and a hand-copied kanban snapshot; then the full Hermes fork was
   imported *underneath* Team's wrappers (2026-10-25). Each layer re-implements
   what the one below has (prompt building, tool loops, session ids), so
   nobody could say which runtime owned a given behaviour. The fresh start
   fixes this by construction: Hermes is the repo, Team is a plugin/profile
   layer in it.
2. **Prose rules were never host rules.** Role SOULs say "touch only your
   files", "write only tests", "read-only", but the manifests grant terminal
   and file toolsets and the generic grader only checks commits, cleanliness,
   merge and the check command. Scoped owned-file enforcement arrived on
   2026-10-26 for boss-created tasks only. Every rule a role is supposed to
   obey needs a code gate before the role is marked active.
3. **Skills described machinery that did not exist.** `plan-a-goal` refers to
   `team talk`; `task-list` claims a gate the run path never had. Agents were
   told to use tools that were never built.
4. **Profile sync protects learned files, so edits to versioned roles did not
   reach existing free workers**, while subscription workers got the source
   files on every launch. Two populations of the same role drifted.
5. **Boss token incident**: one tab-layout session burned 1,311,159 input
   tokens over 18 API calls without dispatching anything. The successful
   planning fixture still cost 171,564 input tokens. Payload cuts were made
   (system prompt −29.7%, tool schemas −21.3%) but total-task cost was never
   shown to drop. Context must be budgeted from the first line of the new
   boss, not patched afterwards.
6. **Models switched on nearly every call** through LiteLLM until
   `session_pin.py`; NVIDIA overloaded; OpenRouter's shared daily cap paused
   the loop. Model routing needs its own evidence table, which `calls` only
   partly is (29 rows for 78 jobs).
7. **Docs drifted from code.** `docs/RUN.md` carries a superseded Claude
   section; `CLAUDE.md` says "base architecture only" while the tree holds
   seven runtime modules; `PIPELINE-READINESS.md` has to warn that it is "an
   audit snapshot, not a live health indicator".
8. **The self-improvement loop, the reason the project exists, is the one
   part with no evaluate or adopt step.** Everything else was built around a
   loop that is still only a proposal writer.
9. **Half the system was uncommitted** at the moment of the reset: 44
   untracked paths and a 25-file diff. Archived in `1e3291f` (§8).

---

## 7. A build order for the fresh start

From `docs/ORIGIN.md` §6 and `docs/KANBAN.md`, adjusted for "fork first".
Each step ends with a thing that runs and a number in the ledger.

1. **Fork** `hermes-agent` (with kanban) into the new repo; pull
   `hermes-agent-self-evolution` beside it. Pin the upstream commits in a
   provenance file. Keep upstream files untouched; Team lives in its own
   directory and in Hermes profiles.
2. **Free gate + `team-worker` profile**, written fresh for Team: a catalog
   of allowed models with why each is free, a generator for the gateway
   routing that refuses anything else, a Hermes hook that blocks the rest;
   Hermes's paid defaults disabled in the profile. Fail closed. Test it
   against the real `hermes` CLI, not a fake.
3. **Ledger** with the agreed schema (`jobs`, `calls`, `goals`, `decisions`)
   plus the designed `plans`, `parts`, `reviews` from day one.
4. **One role with a grading check** (`implement`), `team goal`, `team run` on
   kanban, land by code. Hermes profile = the role; no second harness.
5. **Claude subscription escalation**, scrubbed env, through whatever
   transport is simplest inside Hermes (a provider adapter, not a second
   agent loop).
6. **Boss as a Hermes profile** with scoped planning tools and the contract
   gate; decisions inbox; `team talk` style interviewing.
7. **Replay set + evaluate + adopt** before any role is allowed to self-edit.
   Use the self-evolution pipeline's gates; adoption is a commit that beats
   the base rate.
8. Then, in this order: measured model choice wired into the loop; `spec` /
   `test` / `review` activated one at a time, each only with its code gate;
   overseer; routines; onboarding pass for a second target; UI/deck views on
   top of whatever data exists by then.

Nothing in step 8 starts before step 7 has a number to show.

---

## 8. The archive: where the old work lives

Made 2026-10-26, in this order, so the reset is reversible:

1. Commit `1e3291f` "Archive the native-Hermes runtime work before the fresh
   start": every file that was uncommitted, listed in its message. The Hermes
   fork submodule `vendor/hermes-agent` is part of it.
2. This file, committed on top.
3. Tag **`before-fresh-start`** on that commit. `git checkout
   before-fresh-start` restores the whole old repo, including the live
   worktree branches listed by `git branch` (goal branches `team/...`).
4. The sweep commit after the tag removes everything except this file, so
   `main` starts clean with the history intact underneath.

State outside the repo was not touched by any of this: `~/state/team/`
(ledger, kanban boards, Hermes profiles, planning records, LiteLLM usage,
worktrees) and `~/.hermes/profiles/team-*`. Decide separately whether the new
Team reads that ledger or starts its own.
