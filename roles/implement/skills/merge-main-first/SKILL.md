---
name: merge-main-first
description: What to do when Team reports a merge conflict with main.
---
Run `git merge main`. For each conflicted file keep both sides' intent (main
usually added something unrelated next to yours), remove the markers, run the
whole check, `git add` the files, `git commit --no-edit`. Then continue.
