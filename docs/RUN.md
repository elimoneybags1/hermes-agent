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
