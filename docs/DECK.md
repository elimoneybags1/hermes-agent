# The Team deck: boss + control room in herdr

Eli, 2026-10-24: "when it's complete I want to interact in that specific CLI,
and open it, along with a nice UI (not just terminal lines) within this deck."
Chosen layout: **boss on the left, control room on the right.**

```
┌──────────── boss ────────────┬────────── team deck ──────────┐
│ claude --agent boss          │ Pipeline  Inbox(2)  Agents  Log│
│ (you talk here)              │ ...                            │
└──────────────────────────────┴────────────────────────────────┘
```

herdr panes are terminals, so the control room is a full-screen terminal app
(Textual): panels, tables, keys and mouse, colour, live refresh. The web
dashboard (`team ui`) stays for long edits (instructions, skills, settings).

## `team deck` (`team/deck.py`, Textual)

Reads the same sources as `team ui`, directly in Python (no server): the
ledger (`goals`, `jobs`, `calls`, `decisions`), `team/roles.py`, and Team's
kanban board through `team/kanban.py`. Refreshes every 2 s; never blocks the UI
on a hermes call (run it in a worker thread).

Header, always visible: `team run` alive or stopped (a process whose argv is
`team run`), goals open / landed today, pass rate as `14/28 · base 18/30`
(never a bare percentage), open decisions count.

Tabs (F1–F5, or click):

1. **Pipeline**: the role strip (boss → spec → test → implement → review) with
   how many goals are at each; below, a table of open goals: id, target, role,
   attempt, kanban status, age, first line of the goal. Enter on a goal opens
   its detail: the whole goal text, every attempt (job id, model, outcome,
   grader detail tail), and the kanban task's comments.
2. **Inbox**: open decisions, newest first, each with its question (bold),
   context, options numbered with the recommendation marked. Keys: `1`–`9`
   answer with that option, `a` type a free answer, `d` dismiss (asks y/n in a
   footer line, never a modal). Uses `team/decisions.py`.
3. **Agents**: one row per role: name, title, status, model, pass `n/m`.
   Enter shows its instructions and skills (read-only) and its last jobs.
   `c` opens a chat with that agent: a new herdr pane in the current tab
   running `hermes -p team-<role> chat` (only if that profile exists; boss →
   says the boss is the left pane). `e` opens `team ui` in the browser at that
   agent.
4. **Log**: what Team did, newest first, from the ledger: jobs started and
   finished (outcome, grader, detail tail), goals landed, decisions answered;
   one line each with time and target.
5. **Runtime** (added 2026-10-26, `docs/UI.md` "Runtime"): the Hermes runtime
   from `team/runtime.py`. A summary line (profiles, sessions and how many are
   active, proposals open / total, model calls in the last 24 h by provider),
   a table of the newest sessions per profile (role, started, title, status,
   model, messages, tool calls, API calls, tokens in/out, session id) and a
   table of optimizer proposals (status, created, subject, where). Read-only.

`g` anywhere: new goal (a form: target from attached targets, goal text, role)
→ `team.run.start_goal`. `q` quits.

## `team open`

Opens the deck in the current herdr session from the boss's terminal or any
shell: a pane to the right of the current one running `team deck`, and, if no
`team run` process exists, a `run` tab running `team run`. Uses the herdr CLI
(`herdr pane split`, `herdr tab create`, `pane send-text`, `send-keys enter`);
does nothing that needs a TTY. `.work.json`'s Team layout gets a second pane,
`deck`, so `work team` opens boss and deck side by side.

## Tests

Textual's `App.run_test()` pilot: the app starts with a real ledger in
`TEAM_STATE` (tmp_path) holding goals, jobs and decisions; tabs switch with
1–4; the Inbox answers decision 1 with key `1` and the ledger shows it
answered; the Pipeline table lists the open goals. Kanban and herdr calls go
through injectable runners; tests never call them for real.
