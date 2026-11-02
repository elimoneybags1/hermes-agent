---
name: find-the-check
description: How to find or make the one command that decides "done" for a repo, and prove it can fail.
---
1. Look, in this order, and stop at the first that answers: CLAUDE.md or
   AGENTS.md; package.json `scripts` (`check`, `test`, `lint`, `build`);
   Makefile or justfile targets; pyproject `[tool.pytest]` or a tests/
   directory; `.github/workflows/*.yml` (what CI runs is what counts).
2. Run it in the worktree exactly as a shell would (`/bin/sh -c`). If a
   tool is missing (`pytest: command not found`, `node_modules` absent),
   that is the `setup` command's job: write the install step as `setup` and
   prefix the check so it works in a fresh worktree (`npm ci && npm test`
   is slow; prefer `setup: npm ci`, `check: npm test`).
3. `test_runner` is the same command for one file, with `{path}` where the
   file goes: `npm test -- {path}`, `scripts/run_tests.sh {path}`,
   `python -m pytest {path}`. If the repo has no per-file way, say so by
   leaving it empty.
4. `break_cmd` must corrupt something the check covers and be undoable by
   `git checkout -- .`: `sed -i '' 's/return /return None; /' src/x.ts` style
   edits to a tracked file, never a deletion of untracked files or a change
   outside the worktree.
5. Say in `notes` what you could not settle: a test that needs a service, a
   flaky suite, a command that takes minutes.
