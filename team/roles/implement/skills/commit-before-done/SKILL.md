---
name: commit-before-done
description: Uncommitted work is thrown away; five jobs in the old ledger did the work and never committed.
---
Before you finish: `git status --short` must show nothing of yours. `git add`
only your files, `git commit -m "<task id>: <what>"`, then `git log -1` to see
it. Only then call `kanban_request_review`.
