---
name: review-the-diff
description: How to turn a diff and a done_when list into a verdict code will accept.
---
1. List the done_when lines. For each, find the hunk that makes it true and
   the test that proves it. Write both into `checked` as
   "<line>: met by <file>:<line>, tested by <test>". A line with no hunk or no
   test is a finding, not a checked entry.
2. Read each hunk for behaviour: wrong condition, missing case, swallowed
   error, a name that does not match what the order said, a value the tests
   never exercise.
3. For every finding, note the `+` line number in the new file from the
   hunk header (`@@ -a,b +c,d @@`: the first `+` line is `c`). Code checks
   the number; a wrong one refuses your whole answer.
4. Decide: all checked and no findings → `land`; anything else → `fix`.
5. Answer with the JSON object only.
