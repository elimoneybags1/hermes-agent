# The overseer: always watching, bringing Eli only decisions

Eli, 2026-10-24: "an agent that is always looking over the work being done, and
finds the things and decisions that will happen and need deciding through the
whole build of the project. The only work I need to do is steering sometimes."

`team run` keeps work moving. The overseer is the part that thinks about it:
on a schedule, for each target, it reads what happened, compares it with the
target's goals, settles what it can inside that scope, and turns the rest into
questions for Eli. Eli's side is one inbox.

## What it does each review

1. **Gather** (code, no model): the target's goals and their state, ledger jobs
   since the last review (outcome, grader detail), blocked kanban tasks and their
   reasons, commits on the target's main since the last review (subjects and
   stat), open decisions, the decisions log, and the last review's summary.
2. **Review** (Claude Code on the subscription, read-only): one `claude -p` run in
   the target repo with only `Read,Grep,Glob` tools and `--json-schema`, given the
   gathered context. It returns:
   ```json
   {
     "summary": "where this target stands, for Eli, 5 lines max",
     "decisions": [{"question": "...", "context": "why it matters, what was seen",
                    "options": ["...", "..."], "recommendation": "...",
                    "goal_id": 3 | null}],
     "actions": [{"kind": "add_goal", "goal": "...", "why": "..."},
                 {"kind": "comment", "task_id": "t_...", "text": "..."}],
     "drift": ["work that is outside the goals, one line each"]
   }
   ```
3. **Apply** (code): validate and carry out `actions`, record `decisions`, store
   the review. A decision with a `goal_id` pauses that goal: `team run` creates no
   new tasks for it until the decision is answered. Everything else keeps going.

**What the overseer may do on its own** (inside the scope Eli set): add a goal
that serves an existing goal, comment on a kanban task to steer a worker. **What
it must ask about**: scope changes, new dependencies or services, anything that
costs money, choices between designs that Eli would notice, deleting work, and
a goal that keeps failing after escalation (`team run` already blocks it). The
overseer never edits code and never lands anything.

## Data (ledger, `$TEAM_STATE/team.db`)

- `decisions`: id, target, created (UTC ISO seconds), question, context,
  options (JSON list), recommendation, goal_id (nullable), status
  (`open|answered|dismissed`), answer, answered_at, source (`overseer|run|boss`).
- `reviews`: id, target, ts, summary, drift (JSON list), actions (JSON list, as
  applied, with any refusal), session_id, num_turns, duration_ms, ok.

Answers also append to `$TEAM_STATE/targets/<name>/decisions.md`
(`## <date> — <question>` then the answer), which goes into every worker brief
and every review for that target, so a decision is made once.

## Settings (`team/settings.py`, so they show on the Settings page)

`oversee.enabled` (bool, true), `oversee.interval_s` (int, 900: at most one
review per target per interval, and only when the ledger or board changed since
the last one), `oversee.max_turns` (int, 30), `oversee.timeout_s` (int, 900).

## Eli's side

- `team decide` lists open decisions; `team decide <id> "<answer>"` answers one;
  `team decide <id> --dismiss`.
- `team ui`: an **Inbox** button in the header with the open count; each decision
  shows question, context, options as buttons, the recommendation marked, and a
  free-text answer. Each target's latest review summary and drift at the top.
- The boss reads the same inbox and can answer for Eli when Eli tells it to.

## Build (each step goes in through `team goal team "..."`)

**A. Decisions** — `team/decisions.py`: `create(conn, target, question, context,
options, recommendation, goal_id=None, source="overseer") -> int`,
`list_open(conn, target=None) -> list[dict]`, `answer(conn, id, text)`,
`dismiss(conn, id)`, `log_path(target)`, and appending to `decisions.md` on
answer. Tables added by `ledger.init`. CLI `team decide`. When a goal is paused
or resumed is `team/run.py`'s business: give it `paused_goals(conn, target) ->
set[int]` (goal ids with an open decision) and make the tick skip them.

**B. Review** — `team/oversee.py`: `gather(root, conn, target) -> dict`,
`brief(context) -> str`, `SCHEMA`, `review(root, conn, target, runner=...)`
(runs `claude_run.run` with `tools=("Read","Grep","Glob")` and `json_schema`,
which `claude_run.run` gains: `--tools ...` and `--json-schema <json>`, still
never with an API key; read the structured output), `apply(conn, target,
result) -> dict`, `due(conn, root, target) -> bool`. `team run` calls it at the
end of each tick for each due target; `team oversee <target>` runs one now.
Settings fields above.

**C. Inbox** — `team/ui.py`: `GET /api/decisions` (open ones plus each target's
latest review), `POST /api/decisions/<id>` `{"answer": "..."}` or
`{"dismiss": true}`; the decisions table and the reviews table go into
`agents.fingerprint` via the ledger file already. `team/ui/index.html`: the
Inbox view, same rules as the Settings view.
