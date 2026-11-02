---
name: write-failing-tests
description: Tests written before the code; they fail for the right reason, then the builder makes them pass.
---
1. Read the done_when lines. Each one names a fact a test can assert. Write
   the assertion first, then the setup it needs.
2. Import the function or module the part will add exactly as the brief's
   interfaces name it. A missing import is a correct failure now.
3. Copy the shape of a neighbouring test file (fixtures, helpers, how it
   builds a repo or a ledger). Do not invent a new style.
4. Run the acceptance command. Read the failure: it must be *your* test
   failing because the part does not exist yet, not a syntax error in your
   file. Fix syntax errors; leave the real failure.
5. `git add` only your test files, commit, then `kanban_request_review`.
