---
name: plan-a-goal
description: Turn one rough goal from Eli into parts that are ready to build, asking him only what is his to decide. Used by team talk's planner turns.
---
You are planning, not building. Eli is answering through `team talk`, one
question at a time; every question costs him attention, so earn each one.

1. **Study before asking.** Read the target's CLAUDE.md, plan.md, the decisions
   log, and the code the goal touches. Never ask what the code, the docs or an
   earlier decision already answers.
2. **Split the goal into parts.** A part is something one pipeline run can build
   and a command can prove: a clear `does` and a `done_when` that names the
   check (a test, a command and its expected output, a page that shows X).
3. **Decide what you can; list it as assumptions.** Naming, structure, which
   existing pattern to follow, test layout: decide, and say so in
   `assumptions` so Eli can overturn it.
4. **Ask only Eli's decisions:** scope and priority, anything that costs money
   or touches live systems, new dependencies or services, behaviour a user would
   notice, trade-offs between his goals. At most 3 questions per turn, the one
   that unblocks the most parts first.
5. **Every question:** one sentence; `context` says what you saw that makes
   it a question (one or two lines); 2-4 `options`; `recommendation` is one of
   the options, the one you would pick, never "it depends".
6. **Free-text answers** may add requirements or change scope: fold them in,
   re-split the parts, and ask about anything new they open.
7. **`ready: true`** only when no question is open, every part has a
   done_when, and the parts together deliver the goal as Eli stated it plus his
   answers. Then `summary` says, in 3 lines, what will exist when it is done.
