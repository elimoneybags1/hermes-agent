---
name: commit-before-done
description: Five failed jobs in this ledger did the work and never committed.
---
Before you finish: `git status --short` must show nothing of yours. `git add`
only your files, `git commit -m "<task id>: <what>"`, then `git log -1` to see
it. Only then report done.
