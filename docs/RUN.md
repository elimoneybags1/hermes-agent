# `team run`: the autonomous loop

Design and reasons: `docs/KANBAN.md`. This file is the contract for the code.
Built in two rounds: three modules with no dependency on each other, then
`team/run.py`, which uses them.

Common rules: Python 3.11, stdlib only, `from __future__ import annotations`,
type hints, short docstrings, style of `team/ledger.py`. Every subprocess goes
through an injectable `runner` (default `subprocess.run`) so tests never call
hermes, claude or the network. Paths from `$HOME` / `$TEAM_STATE`
(`team.ledger.state_dir()`), never a hardcoded user directory.

## Round 1

### `team/kanban.py`: Team's view of Hermes kanban

Every call runs `hermes -p team-worker kanban --board <board> ...` with
`HERMES_KANBAN_HOME=<state_dir>/kanban` added to the environment (everything
else inherited). `KanbanError(RuntimeError)` on a non-zero exit, carrying the
command's stderr tail.

- `kanban_home() -> Path`: `state_dir() / "kanban"`.
- `create_task(board, title, body, *, assignee="team-worker", workspace=None, branch=None, max_runtime=None, skills=(), runner=...) -> dict`:
  `create ... --json`; `workspace` is passed through (`worktree:<path>` etc.);
  returns the parsed task (has `id`).
- `show(board, task_id, runner=...) -> dict`: `show <id> --json`, parsed.
- `list_tasks(board, status=None, runner=...) -> list[dict]`: `list --json [--status S]`.
- `dispatch(board, runner=...) -> dict`: `dispatch --json`, parsed.
- `comment(board, task_id, text, runner=...) -> None`: `comment <id> <text> --author team`.
- `block(board, task_id, reason, kind="needs_input", runner=...) -> None`.
- `session_for_task(task_id, runner=...) -> dict | None`: find the Hermes
  session whose title starts with `work kanban task <first 9 chars of id>` in
  `hermes -p team-worker sessions list` output, export it with
  `hermes -p team-worker sessions export <tmpfile> --session-id <id> --format jsonl`,
  and return `{session_id, model, billing_provider, input_tokens,
  output_tokens, estimated_cost_usd, api_call_count}`. None if not found.

Tests: a fake runner that records argv/env and returns canned
`CompletedProcess` objects (use real JSON shapes: a task has `id`, `status`,
`result`; `show --json` returns `{"task": {...}, "runs": [...]}` or the task
itself: accept both). Check argv, the env var, parsing, KanbanError.

### `team/grade.py`: worktrees, grading by code, landing

- `worktree_path(target, slug) -> Path`: `state_dir()/"worktrees"/target/slug`.
- `create_worktree(repo, target, slug, base="main", runner=...) -> tuple[Path, str]`:
  `git -C repo worktree add -b team/<slug> <path> <base>`; returns (path, branch).
  GradeError if the branch or path exists.
- `run_check(worktree, command, timeout_s=900, runner=...) -> tuple[bool, str]`:
  run `command` (a string, via `["/bin/sh", "-c", command]`) with cwd=worktree;
  returns (exit code == 0, last 60 lines of stdout+stderr). A timeout returns
  (False, "timeout after <n>s").
- `has_commits(repo, branch, base="main", runner=...) -> bool`: `git rev-list --count base..branch` > 0.
- `land(repo, branch, message, runner=...) -> str`: refuse (GradeError) if
  `git -C repo status --porcelain --untracked-files=no` is not empty or the
  checked-out branch is not `main`; then `git -C repo merge --no-ff -m message branch`;
  on conflict run `git merge --abort` and raise GradeError; return the merge sha.
- `remove_worktree(repo, path, branch, runner=...) -> None`:
  `git worktree remove --force <path>` then `git branch -D <branch>`.

Tests: real git in tmp_path (init a repo with a main branch and one commit,
set user.name/email locally). Cover create, check pass/fail/timeout, has_commits,
land success, land refused on dirty tree, land conflict aborts cleanly, remove.

### `team/claude_run.py`: Claude Code on the subscription

- `SCRUB = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
  "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX")`.
- `clean_env(env=None) -> dict`: copy of env (default os.environ) without SCRUB.
- `run(worktree, brief, *, max_turns=60, timeout_s=1800, runner=...) -> dict`:
  `claude -p <brief> --output-format json --max-turns <n> --dangerously-skip-permissions`
  with cwd=worktree and env=clean_env(). Parse stdout JSON and return
  `{ok, result, session_id, num_turns, duration_ms, usage}` (missing keys None;
  `ok` is False when `is_error` is true, exit is non-zero, or the output is not
  JSON). A timeout returns ok False with result "timeout after <n>s".
  Never pass any API key; never read one.

Tests: fake runner; assert the scrubbed env (set fake values for every SCRUB
name first), argv, parsing of a success and an error payload, timeout.

## Round 2 (after round 1 lands)

### `team/run.py` and `team goal` / `team run`

- Ledger: `jobs` gains `task_ref TEXT` (kanban task id or `claude:<session>`),
  added by `ledger.init` if missing.
- `team goal <target> "<goal>" [--role implement] [--check "<cmd>"]`: create a
  worktree, a kanban task on board `<target>` with workspace
  `worktree:<path>` and `--branch team/<slug>`, the role's assignee
  (`team-<role>` profile when it exists, else `team-worker`), `--max-runtime`
  from the role budget; a ledger job (attempt 1, task_ref). The check
  is `settings.check_for(root, target)`; `--check` sets the target's check
  first (`settings.set_target`).
- All loop numbers come from `settings.load(root)` (`team/settings.py`),
  read fresh each tick so a change in `team ui` applies on the next tick:
  interval, check timeout, whether to escalate to Claude and its limits,
  whether to block after Claude fails. Targets with `enabled: false` are
  skipped. Each target's board is named after the target; create it on first
  use (`hermes -p team-worker kanban boards create <name>`, via team.kanban).
- `team run [--once] [--interval N]`, each tick for every attached target:
  1. `kanban.dispatch(board)`.
  2. For each task Team created that kanban reports `done`: if the branch has
     no commits, fail with "no commits". Else `run_check`. Pass: `land`,
     `remove_worktree`, ledger pass. Fail: ledger fail with the output tail.
  3. After a fail, attempt + 1: while attempts < the role's
     `escalation.after_failures` + 1, a new kanban task on the same branch,
     body = goal + failure output. Kanban removes a clean worktree when its
     task completes, so grade and retry from the branch: recreate the
     worktree with `git worktree add <path> <branch>` when it is missing.
     Then, if `escalation.claude`, one attempt with `claude_run.run` in the
     worktree, graded the same way. If that fails and
     `ask.block_after_claude_fails`, block the last kanban task with
     "needs_input" and stop: Eli decides.
  4. Blocked or crashed tasks: ledger error with kanban's reason.
  5. Team's own record of what it is tracking (goal, target, role, branch,
     worktree, attempt, current kanban task, job id) lives in a new ledger
     table `goals`, so `team run` can restart at any time and carry on.
  6. Each finished free attempt gets a `calls` row from `session_for_task`;
     refuse to record it as free (ledger error, block) if the model is not
     free or the cost is above zero.

Learned from running kanban for real (2026-10-24), also part of the contract:

- Kanban neither enforces `--max-runtime` nor notices a dead worker. Each
  tick, for a tracked task kanban calls `running`, the latest run's
  `worker_pid` and `started_at` (`show --json`, `runs[-1]`) are checked: a pid
  that is gone, or a run older than the role's `budget.run_budget_s` + 300 s,
  gets the pid terminated (if alive), the task blocked with the reason, and a
  ledger error; the retry/escalation path then carries on.
- Before grading, main is merged into the branch in the worktree, so the
  check runs against current main. A conflict is a failed attempt listing the
  conflicted files; the merge is aborted.
- `dispatch` passes `--max <loop.max_new_per_tick>`; spawning stays kanban's.

## Round 3: one kanban task per goal, Team as the reviewer

Why: `docs/KANBAN.md`, "Use kanban first". Kanban v0.21 facts this relies on
(checked in `hermes_cli/kanban_db.py`): a worker calls `kanban_request_review`
to move its task to `review`; with `review_dispatch: false` nothing else
touches it there; `hermes kanban reopen-review <id>` sends it back to
`ready` under the implementer that requested review, and the worker's next run
sees the task's comments in its context; `complete <id>` finishes it.

- The brief tells the worker to finish with `kanban_request_review` (summary:
  what it did and the check's last line), never `kanban_complete`.
- Each tick, for each goal: task in `review` → grade (as now). Pass:
  `kanban.complete`, land. Fail: `kanban.comment` with the failure tail, then
  `kanban.reopen_review`; attempt + 1, a new ledger job with the same
  `task_ref`. No new kanban task per attempt.
- A task the worker marked `done` anyway is graded the same way; on a fail it
  cannot be reopened, so fall back to a new task on the same branch (today's
  path).
- Escalation counts attempts as now: after the role's `after_failures`, the
  Claude attempt runs in the worktree; its result is commented on the task;
  pass → `complete` and land; fail → `block --kind needs_input`.
- `team/kanban.py` gains `reopen_review(board, task_id)` and
  `complete(board, task_id, result)`.
- `kanban.session_for_task(task_id, profile=...)`: sessions live in the profile
  that ran the task (the task's `assignee`), not always `team-worker`.
- Tasks get the role's skills (`--skill`, from `roles/<role>/skills/` names
  when that role's profile has them) and the role's free model (`--model`,
  checked with `is_free_model`, refuse otherwise).
- **One line per tick on stdout**, so the `run` tab shows what Team does:
  `HH:MM:SS <target>: dispatched N; goal 3 review→grading; goal 3 pass, landed abc1234`
  or `HH:MM:SS idle (2 goals running)`. Errors stay on stderr.

## Claude in build mode (Eli, 2026-10-24)

Claude Code runs under two sets of rules. **Interactive** (the boss, Eli's
sessions): ask before assuming, explain, glue steps together. **Build** (every
`claude_run` call Team makes): no one is there to answer, so it never asks; it
works only inside its task, proves its work by the check, returns exactly the
shape the gate reads, and turns anything that is Eli's to decide into a
decision file instead of a question. Build mode runs Claude through Team's
orchestration with the same role files as the Hermes workers.

`team/claude_roles.py`:
- `plugin_dir(root, role) -> Path`: generate, under
  `$TEAM_STATE/claude/<role>/`, a Claude Code plugin from `roles/<role>/`:
  `.claude-plugin/plugin.json`; `agents/<role>.md` (frontmatter `name`,
  `description` from `does`; body: the role's SOUL.md, then the **build rules**
  below); `skills/<skill>/SKILL.md` copied from the role (same Agent Skills
  format Hermes uses); `hooks/hooks.json` with the two gates below. Regenerate
  when any source file is newer. Never written inside a repo.
- Build rules appended to every build agent: "No person is reading this
  session. Never ask a question and never wait for an answer. If a choice
  belongs to Eli (scope, a dependency, cost, a design he would notice), write
  it to $TEAM_DECISION_FILE as JSON {question, context, options,
  recommendation} and stop. Touch only the files your task names. Finish only
  when the check passes and your work is committed."

`team hook stop` and `team hook pretool` (`team/cli.py`, read Claude Code's
hook JSON on stdin; exit 2 with a reason on stderr blocks and tells Claude why):
- **Stop gate**: refuse to stop while `git status --porcelain
  --untracked-files=no` in the worktree is not empty, or while
  `$TEAM_CHECK` (run in the worktree, timeout from settings) fails; the
  reason is the dirty file list or the check's last 40 lines. At most
  `$TEAM_STOP_RETRIES` (default 3) refusals per session (count in a file under
  the plugin dir keyed by session_id), then allow, so it can never loop forever.
- **Edit gate** (PreToolUse on Edit, Write, MultiEdit, NotebookEdit): when
  `$TEAM_ALLOWED_FILES` is set (colon-separated paths relative to the
  worktree), refuse a file_path outside it.

`claude_run.run(...)` gains `role`, `tools`, `model`, `json_schema`,
`allowed_files`, `check`, `decision_file`: argv adds `--plugin-dir
<plugin_dir> --agent <role>`, `--tools <role.toml tools mapped to Claude
tools>` (file→Read,Edit,Write,Glob,Grep; terminal→Bash; read-only roles get
Read,Grep,Glob), `--model` when given, `--json-schema` when given, and the env
vars above. API-key variables stay scrubbed. Result adds `structured` (the
parsed schema output) and the usage block as returned (input, output,
cache_creation_input_tokens, cache_read_input_tokens).

Interactive settings: check (and record here) whether the user's
`~/.claude/CLAUDE.md` loads in `claude -p`; if it does, the build rules above
override its "ask before assuming" for build sessions. The user's settings.json
deny rules (they protect secrets) must stay in force: do not pass
`--setting-sources` that drops `user`.
