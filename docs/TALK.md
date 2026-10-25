# `team talk`: Team interviews Eli

Eli, 2026-10-24: "I want to work in a different CLI, with a different way of
conversation: question answering and goal setting." Chosen style: **Team leads.**
It puts one question at a time in front of Eli, with numbered options and its
recommendation; Eli answers with a key or a sentence. Between questions he can
type a new goal; Team asks follow-ups until the goal is ready, then hands it to
the team. No free-form chat unless he asks for it.

This is how planning happens (`docs/PLANNER.md`): the planner runs behind
`team talk` instead of inside a boss conversation. The boss stays for open
conversation and for changing the team itself.

```
team talk — fundamentals                      [3 open · 1 goal being planned]

Q  Trailing stops: trail by % or by ATR?
   context: entries already use ATR(14)
   1) %      2) ATR (recommended)   3) both, per strategy
> 2
✓ recorded.

Q  Apply to open positions too?
   1) yes   2) new entries only (recommended)
> new entries only, and log every adjustment
✓ goal 'trailing stops' is ready → 3 parts sent to the team

> goal: add a weekly P&L email
  ↳ planning… 2 questions
```

## The loop

`team talk [target]` (default: the target of the cwd, else ask):

1. Show the header: target, open decisions, goals being planned, goals building.
2. Show the next question: open decisions for the target, oldest first;
   questions from a plan being worked come first, so one goal is finished
   before the next starts.
3. Read one line:
   - `1`–`9`: that option; Enter alone: the recommendation.
   - Any other text: the answer, in Eli's words.
   - `goal: <text>`: a new goal; planning starts (step 5).
   - `/skip` (leave it open, next), `/dismiss`, `/goals`, `/status`,
     `/target <name>`, `/ask <question>` (one answer from the planner about
     the project, nothing recorded), `/quit`.
4. Record the answer (`team.decisions.answer`, so it lands in the target's
   decisions log); if it belonged to a plan, the plan's next planner turn runs.
5. **Planner turn**: one `claude_run.run` in **build mode** (`docs/RUN.md`), in the
   target repo, read-only tools (`Read, Grep, Glob`), role `spec`'s plugin with
   the planning skill, `--json-schema` below. Input: the goal, the target's
   CLAUDE.md, `plan.md`, the decisions log, and every answer so far.
   Later turns `--resume` the same Claude session, so what it already read is
   a cache read, not re-sent work. Output:
   ```json
   {"summary": "what this goal will deliver, 3 lines",
    "questions": [{"question", "context", "options": [], "recommendation"}],
    "assumptions": ["decided without asking, Eli can overturn"],
    "parts": [{"title", "does", "done_when"}],
    "ready": true|false}
   ```
   Questions become decisions (`plan_id` set); at most 3 per turn. `ready`
   with no open questions → the plan's parts are handed over: one
   `team goal` each today, `team part` once the pipeline exists; `plan.md`
   gets the summary, assumptions and parts. Show "✓ goal '<title>' is ready →
   N parts sent to the team".
6. While a planner turn runs (seconds to a minute), the prompt stays usable:
   show "↳ planning…" and keep answering other open questions.

## Data

- Ledger table `plans`: id, target, goal, status
  (`planning|ready|sent|dropped`), session_id (the Claude session to resume),
  summary, created, updated.
- `decisions` gains `plan_id` (nullable).
- `$TEAM_STATE/targets/<name>/plan.md`: appended per plan when it is sent.
- Every planner turn is a ledger job (role `spec`, grader `plan-schema`: the
  output parsed against the schema) with its Claude usage in `calls`.

## Look

Plain terminal, not full-screen: it reads like a conversation and scrolls.
`rich` (already installed with Textual) for colour: the question bold, context
dim, the recommendation marked, ✓ in green. Works in any herdr pane;
`team open` gets a `talk` option.

## Build (after Claude build mode, goal 16, which it uses)

`team/talk.py` (loop, rendering, input), `team/planner.py` (planner turn,
schema, plans table, hand-off), ledger changes, `team talk` in `team/cli.py`,
the planning skill `roles/spec/skills/plan-a-goal/SKILL.md` (written by the
boss). Tests drive the loop with scripted input lines and a fake
`claude_run`, with a real ledger in `TEAM_STATE`.
