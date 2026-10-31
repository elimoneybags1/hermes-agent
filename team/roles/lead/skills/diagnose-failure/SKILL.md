---
name: diagnose-failure
description: How to read a failed attempt and steer the retry.
---
1. Read the check output first: the failing test name and assertion usually
   say what is wrong.
2. Compare the diff with the order's steps: did the worker skip a step, edit
   the wrong place, or follow a step that was wrong?
3. If the order was wrong, give corrected steps and the context the worker
   lacked. If the worker slipped, the note alone is enough.
4. "uncommitted changes" or "no commits" means the worker did not commit:
   say so plainly; that is a retry, not an escalation.
5. "changes outside owned files" means the order's owned paths were too
   narrow or the worker strayed: say which.
