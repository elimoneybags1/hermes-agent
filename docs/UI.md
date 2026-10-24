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
