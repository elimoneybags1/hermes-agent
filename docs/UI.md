# Agent dashboard: `team ui`

The main place Eli looks at and changes Team's agents (Eli, 2026-10-21).
A local web page served by `team ui` on this machine. It shows every agent,
its role, where it sits in the pipeline, and what it is doing; and it lets Eli
open any agent and change its model, tools, skills, instructions and budget.
The boss changes the same files while Eli talks to it, and the page shows
those changes live.

`docs/DASH.md` (the read-only `team dash --json` snapshot for grid) stays as
it is. This page builds on the same snapshot.

## Where an agent lives

Everything that defines an agent is a file in this repo, so every change is a
git commit that can be seen and rolled back.

| Part | File | Edited from the page |
|---|---|---|
| Role, model, budget, tools, pipeline | `roles/<name>/role.toml` | yes, field by field |
| Instructions (who it is, how it works) | `roles/<name>/SOUL.md`; for boss, `plugin/agents/boss.md` | yes, whole text |
| Skills | `roles/<name>/skills/<skill>/SKILL.md` | yes: add, edit, delete |
| Hooks | `plugin/hooks/hooks.json` (boss); `roles/<name>/hooks.json` if present | no, shown read-only |
| Jobs, pass rates | ledger `$TEAM_STATE/team.db` | no |

`role.toml` gains two keys:

- `tools = ["terminal", "file", ...]`: Hermes toolsets the worker runs with
  (`hermes chat -t`). Allowed values are `TOOLSETS` below. Absent for boss.
- `hands_to = "<role>"`: the next role in the pipeline.

`TOOLSETS = ["terminal", "file", "code_execution", "todo", "skills", "web",
"browser", "vision", "session_search", "clarify"]`

## `team/agents.py` (pure Python, no server)

Every function takes `root: Path` (the Team repo). Refusals raise
`AgentError(ValueError)` with a one-line message. Writes commit with
`git -C root commit -m <msg> -- <paths>` (only the paths changed; nothing
else staged is swept in) and return the new commit's full sha. Commit message:
`<agent>: <what changed> (<source>)`, `source` is `dashboard` by default.

- `agent_names(root) -> list[str]`: directories under `roles/` with a role.toml, sorted.
- `pipeline(roles: list[dict]) -> list[str]`: follow `hands_to` from `"boss"`
  until a name repeats or is missing; names not reached are not included.
  For the current roster: `["boss", "triage", "spec", "implement", "review"]`.
- `detail(root, conn, name) -> dict` (conn may be None: no ledger yet):
  ```
  {
    "name": "implement",
    "role": {... role.toml as parsed ...},
    "instructions": {"path": "roles/implement/SOUL.md", "text": "..." | null},
    "skills": [{"name": "run-pytest", "path": "roles/implement/skills/run-pytest/SKILL.md", "text": "..."}],
    "tools": ["terminal", "file"],          role.toml tools, [] if absent
    "available_tools": TOOLSETS,
    "hooks": [{"source": "plugin/hooks/hooks.json", "event": "PreToolUse",
               "matcher": "Bash", "command": "..."}],
    "history": [{"sha": "...", "short": "c8ae6de", "date": "UTC ISO seconds",
                 "author": "...", "subject": "..."}],   last 30 commits touching this agent's files
    "jobs": [... ledger rows for this role, newest first, max 20 ...],
    "working_on": [...], "stats": {...}, "base": {...}   same shapes as dash.py
  }
  ```
  Paths are relative to root. Unknown name raises AgentError.
- `set_field(root, name, field, value, source="dashboard") -> str`. Fields:
  `title`, `does`, `graded_by` (non-empty str); `status` (`active|planned`);
  `runs_on.model` (str; when `runs_on.harness == "hermes"` it must pass
  `team.free.is_free_model`, fail closed); `budget.max_turns`,
  `budget.run_budget_s`, `escalation.after_failures` (int > 0);
  `tools` (list, each in TOOLSETS); `hands_to` (an existing agent name, not
  itself). Any other field: AgentError. Writes role.toml with `tomli_w`.
- `write_instructions(root, name, text, source="dashboard") -> str`
- `write_skill(root, name, skill, text, source="dashboard") -> str`: skill
  name matches `^[a-z0-9][a-z0-9-]{0,63}$`; creates the folder on first write.
- `delete_skill(root, name, skill, source="dashboard") -> str`
- `revert(root, name, sha, source="dashboard") -> str`: `git revert --no-edit`,
  refused unless every path the commit touched belongs to that agent.
- `fingerprint(root, db: Path) -> str`: changes whenever anything above
  changes: mtimes and sizes under `roles/`, `plugin/`, `.git/HEAD`,
  `.git/refs/heads/`, and the ledger file plus its `-wal`.

## `team ui` (`team/ui.py`, stdlib `http.server`)

`team ui [--port 7341] [--no-open]` serves on `127.0.0.1` only and opens the
browser unless `--no-open`. Requests whose `Host` is not `127.0.0.1:<port>`
or `localhost:<port>` get 403. Write requests must have
`Content-Type: application/json` (a browser can't send that cross-site
without a preflight, which the server never answers).

| Method, path | Body | Returns |
|---|---|---|
| `GET /` | | `team/ui/index.html` |
| `GET /api/state` | | `dash.snapshot(...)` plus `"pipeline": [...]` |
| `GET /api/agents/<name>` | | `agents.detail(...)` |
| `PATCH /api/agents/<name>/role` | `{"field": "...", "value": ...}` | `{"sha": "..."}` |
| `PUT /api/agents/<name>/instructions` | `{"text": "..."}` | `{"sha": "..."}` |
| `PUT /api/agents/<name>/skills/<skill>` | `{"text": "..."}` | `{"sha": "..."}` |
| `DELETE /api/agents/<name>/skills/<skill>` | | `{"sha": "..."}` |
| `POST /api/agents/<name>/revert` | `{"sha": "..."}` | `{"sha": "..."}` |
| `GET /api/events` | | `text/event-stream`: `data: {"fingerprint": "..."}` each time `agents.fingerprint` changes (checked every second), plus a `: ping` comment every 15 s |

Errors: AgentError → 400 `{"error": "..."}`; unknown path → 404 JSON.

## The page (`team/ui/index.html`)

One self-contained file: inline CSS and vanilla JS, no build step, no CDN.
Light and dark from `prefers-color-scheme`. Usable at 1100 px wide and above.

- **Header**: "Team", a live dot (green while the event stream is open, grey
  and "reconnecting" when it drops), base rate as `6/9 passed`.
- **Pipeline strip**: the `pipeline` roles left to right with arrows, last one
  back to boss. Each stage shows its jobs in progress (`job 10 · team · 4m`).
  Planned roles are dimmed.
- **Agent list** (left column): name, title, status dot, model (short), pass
  as `3/6 · base 6/9`, never a bare percentage. Click opens the agent.
- **Agent pane** (right), tabs:
  - *Overview*: does, graded by, status, model, harness, budget, escalation,
    hands to; each editable in place (click, edit, Enter saves, Esc cancels).
  - *Instructions*: the text in a monospace editor; Save commits.
  - *Skills*: list; open one to edit; add (name + text); delete with an
    inline confirm (no `confirm()` dialogs).
  - *Tools*: a checkbox per `available_tools`; a change saves the `tools` field.
  - *Hooks*: read-only table: source, event, matcher, command.
  - *Jobs*: this role's jobs: id, target, attempt, outcome, duration, grader_detail.
  - *History*: commits for this agent, newest first, each with a Revert button.
- **Live**: on each event, reload `/api/state` and the open agent. Anything
  that changed since the last load (a new history commit, a new skill, a
  changed field) is highlighted for a few seconds, with a toast:
  `boss changed implement: added skill run-pytest`, taken from the commit subject.
- After a save, show the short sha; on 400, show the error inline next to the
  field. Never lose text being edited when a live reload arrives: an editor
  with unsaved changes keeps its text and shows "changed elsewhere" instead.

## Settings (Eli, 2026-10-24)

How the orchestration behaves, editable from the page. The data and its rules
are in `team/settings.py` (read it): `orchestration.toml` in the repo for the
loop's rules (each change is a commit), `target.json` in the state dir for
each target (`check`, `enabled`; no commit). Retries before escalation stay
per role (`escalation.after_failures`, on the agent's Overview).

API (same Host and Content-Type rules as the rest):

| Method, path | Body | Returns |
|---|---|---|
| `GET /api/settings` | | `{"settings": settings.load(root), "fields": settings.fields(), "targets": [settings.target_settings(n) for each attached target], "target_fields": [{"key","type","help"} for TARGET_FIELDS]}` |
| `PATCH /api/settings` | `{"key": "...", "value": ...}` | `{"sha": "..."}` (settings.set_value, under the write lock) |
| `PATCH /api/targets/<name>` | `{"key": "...", "value": ...}` | the target's settings (settings.set_target) |

`settings.fingerprint` is not needed: `orchestration.toml` lives in the repo
root, so add it to `agents.fingerprint`'s inputs, and the targets directory
(`$TEAM_STATE/targets/*/target.json`) too, so the page updates live.

Page: a **Settings** button in the header opens a settings view in place of
the agent pane (the agent list and pipeline stay).
- *Loop rules*: one row per field, grouped by section (loop, grading,
  escalation, ask), label from the key, the help text under it, edited in
  place like Overview fields: a checkbox for bool, a number input for int,
  text for str. Show the default when the value equals it.
- *Projects*: one card per target: name, path, `check` (editable text; empty
  means the default check, shown as placeholder), `enabled` (toggle).
- *Retries*: a read-only line per role with `escalation.after_failures` and a
  link that opens that agent's Overview.
- Saves, errors and live highlights behave like the agent pane.

## Creating agents and their profiles (Eli, 2026-10-24)

Design: `docs/PLANNER.md`, "New agents". Code in `team/agents.py` and
`team/profiles.py`; CLI `team agent ...`.

- `agents.create(root, name, *, title, does, graded_by, model, tools,
  hands_to=None, instructions, source="dashboard") -> str`: refuses an existing
  name, a name not matching `^[a-z][a-z0-9-]{0,31}$`, a non-free model, tools
  outside TOOLSETS, an unknown `hands_to`. Writes `roles/<name>/role.toml`
  (status `planned`, harness `hermes`, budget 25 turns / 600 s,
  `escalation.after_failures = 2`) and `roles/<name>/SOUL.md` in one commit:
  `<name>: created (<source>)`.
- `profiles.profile_name(role) -> "team-<role>"`; `profiles.sync(root, role,
  runner=...)`: create the Hermes profile if missing with
  `hermes profile create team-<role> --clone-from team-worker --no-alias
  --description <does>`, then write into `~/.hermes/profiles/team-<role>/`:
  `SOUL.md` from the role's instructions, the role's skills as
  `skills/<skill>/SKILL.md` (replace the whole `skills/` dir of that profile),
  and `model.default` in its config.yaml from `runs_on.model` (edit that one
  key; keep every other key cloned from team-worker, which holds the free-only
  settings). Refuse a non-free model. Never touch any profile not named
  `team-*`. Returns what changed.
- `profiles.sync_all(root)`: every hermes role with status `active` or `planned`.
- CLI: `team agent new <name> --title ... --does ... --graded-by ... --model ...
  [--tools a,b] [--hands-to x]` (instructions from `--instructions-file` or a
  one-line default built from `does`); `team agent sync [<name>]`.
- `team/free.py`'s `free-check` must accept `hermes -p team-<role> ...` like
  `team-worker` (a `team-*` profile is cloned from the locked one).
- UI: a **New agent** button over the agent list: a form with those fields;
  `POST /api/agents {name, title, does, graded_by, model, tools, hands_to,
  instructions}` → `{"sha"}`, then `profiles.sync` for it. Each agent's
  Overview shows its profile (`team-<name>`, exists or not) and a **Sync
  profile** button: `POST /api/agents/<name>/sync`.

## Runtime (Eli, 2026-10-26)

"Surface the new Hermes runtime": what the boss, the workers and the
optimizer are doing, from the state they leave on disk. Read-only. Data in
`team/runtime.py` (`python -m team.runtime --json` prints the same snapshot);
the page shows it under a **Runtime** button in the header, next to Settings
and Inbox, and the deck under its Runtime tab (`docs/DECK.md`).

Sources, all under `$TEAM_STATE`:

| What | Where | Shown as |
|---|---|---|
| `team run` alive | the process table: an argv token whose basename is `team` followed by `run` | header chip (always visible) and the Loop row |
| Model calls | ledger `calls`, last 24 h, grouped by provider and model | calls, ok/calls, tokens in and out, API requests, last call |
| Hermes profiles | `hermes/profiles/<role>/`: `config.yaml` (provider, model), native `state.db` (`sessions`, read-only), `memories/`, `skills/team/`, `logs/agent.log`, `logs/errors.log` | one card per role: model, session counts, memories, team skills, the 10 newest sessions (title, active or end reason, messages, tool calls, API calls, tokens, duration; click a row for the id, cwd, branch, last activity and the resume command), last log line with its age; a recent errors.log line is red |
| Optimizer proposals | `optimizer/proposals/*.json` via `optimizer.proposals()` (status `proposed` or `superseded`), evidence payload left out | one card per proposal, newest first: status, subject, scope, location, supersedes links, evidence count; Details opens before, after, reason, validation, uncertainty, evidence ids, file |

API: `GET /api/runtime` → `runtime.snapshot(conn)`:
`{"generated_at", "state_dir", "run": {"alive", "pids"}, "profiles": [...],
"calls": {"window_h", "providers": [...]}, "proposals": [...]}`. `GET /api/state`
also carries `"run"`. The live stream's fingerprint is `ui.fingerprint(root, db)`:
`agents.fingerprint` plus `runtime.fingerprint()` (profile `state.db`, its WAL,
the two logs, `memories/`, and the proposal file names), so a session that
starts or ends, a log line, or a saved proposal refreshes the view; a new
proposal also raises a toast.

Not shown, on purpose: Claude stop counters (`claude/stops/`, one number per
session) and the free workers' `~/.hermes/profiles/team-*` state, which is not
under `$TEAM_STATE`.

## Flow and Talk (Eli, 2026-10-26)

"I want to see the boss I talk to, plan projects with it, and watch the goals
it makes go to the agents: each agent with its pending goals and the one it is
working on, the goal moving from agent to agent, independent goals running at
the same time, and the model next to each, live." Decided the same day: one
live boss process behind the page, and the flow built on what exists now
(a goal goes boss → its role → landed; the stages of `docs/PIPELINE.md`
become more stations when that pipeline is built).

### Flow view (`team/flow.py`, `GET /api/flow`)

The landing view (header button **Flow**). Read-only; joins the ledger
(`goals`, `jobs`, `calls`), each target's kanban board
(`$TEAM_STATE/kanban/kanban/boards/<target>/kanban.db`, read-only: task
status, model override, worker pid, parent links) and the boss's saved plans
(`$TEAM_STATE/planning/<uuid>.json`).

Look and feel (Eli, 2026-10-26, from a screenshot of the Hermes kanban
board): the board in `vendor/hermes-agent/plugins/kanban/dashboard` is the
original. Its column, lane, card and status-dot rules are copied into the
page (monospace, compact cards with a dot, id row, two-line title and a
meta row; a left border and a travelling arc on a card an agent is actually
working; dashed lanes inside a column), then bent to our pipeline: columns
are agents, not statuses.

- One column per station: **boss** (lilac cards: plans being made, and how
  many goals it has sent), each role in pipeline order, **landed**. A role's
  column has lanes **In progress**, **Blocked** (when any), **To do** in
  dispatch order, and its last three **Done**; its sub-header carries the
  model it is running on right now (the kanban task's model; for a proxy
  tier such as `team-auto`, the real model the LiteLLM proxy last served
  that task with, from `$TEAM_STATE/litellm/usage.db`) or else its
  configured one. Idle planned roles are slim.
- Dots, as on the kanban board: **green** working, **amber** waiting for
  something else to finish first (kanban holds a task until its parents are
  done), grey queued and next up, red blocked, cyan under review (Team is
  grading), blue done or landed, lilac still being planned.
- A card: dot, `#id` (and its plan task), attempt, target, the title, a
  stage word (`running 4m`, `waits for 1 task`, `next up`, `grading`,
  `landed 2h ago`), the model (green when live), and a three-segment
  **track** underneath: the stations in order, lit as the goal passes them.
  Clicking a card opens a **drawer** on the right with the full description,
  where it is (task, worker pid, what it waits for, branch), the model and
  where that is known from, every attempt with its outcome and model, the
  outcome text and the plan it came from.
- Stages: `waiting` (a parent task is not done), `queued` (no task yet, or a
  task not started), `running`, `review`, `blocked`, `landed`, `failed`. A
  plan task that is not sent yet is `planned`, or `waiting` when something
  it depends on has not landed.
- **Plans**: each saved plan with its tasks grouped into waves by
  `depends_on`: wave 1 can start together, later waves wait for earlier
  ones. Each task shows its stage, role, model and goal number once sent.
- The agent list on the left shows under each agent the goal it is working
  on with its live model, and how many are queued or blocked.
- Live: the stream's fingerprint includes the boards and plans
  (`flow.fingerprint`), so a task starting, finishing or changing model
  refreshes the view; a new goal or a stage change raises a toast.

`snapshot(conn, roles, pipeline)` returns `{"generated_at", "stations",
"agents": [{name, title, status, model, live_model, running, queued, blocked,
done}], "goals": [card...], "plans": [{id, target, objective, revision,
tasks: [{id, title, role, depends_on, goal_id, stage, model}], waves,
dispatched}], "counts": {made, queued, running, review, blocked, landed,
failed}}`.

### Talk tab (`team/boss_chat.py`, `/api/boss`)

The boss agent's first tab. The dashboard server keeps **one live boss**: the
same `team/hermes_native.py` process plain `team` starts (profile,
subscription check, local inference gateway, clean environment), with stdin
and stdout piped. Protocol: the banner `Hermes-backed Team boss · <session>`,
`you> ` when it waits for a line, `boss> ` before a reply, a bare `> ` after a
clarifying question (shown with a hint to answer by number or sentence).

- The Hermes session id is saved in `$TEAM_STATE/boss/ui-session.json`, so a
  restart resumes the same session; a session the profile no longer knows
  falls back to a new one with a system line saying so. The transcript is
  appended to `$TEAM_STATE/boss/ui-transcript.jsonl` and survives reloads.
- Goals created while the boss answers (its `planning_dispatch`) appear as
  system lines: `sent goal 19 to implement on team: <title>`; the Flow view
  picks them up through the live stream.
- API: `GET /api/boss` → `{alive, busy, awaiting: "you"|"ask"|null,
  session_id, started_at, error, transcript: [{ts, who: you|boss|system,
  text}], started}`; `POST /api/boss/start|send|stop|new` (JSON; `send`
  takes `{"text"}`; 409 while the boss is still answering or not running,
  400 for an empty line). The server stops the boss when it closes.
- Page: status (not started, ready, thinking, asking you, stopped), session,
  Start/Resume, Stop, New session (forgets the session and the transcript),
  a transcript of bubbles, a box where Enter sends. The box is always open:
  the first message starts the boss (resuming the saved session) and is then
  delivered, so nobody has to press Start (`send` on a stopped boss starts
  it; 503 with the reason if it cannot). While the boss is busy the page
  polls every 1.5 s on top of the live events.

Not built here, on purpose: the multi-stage pipeline (spec, test, review as
stations), and the deck's view of the same flow.
