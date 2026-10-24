---
name: task-list
description: The JSON task list a spec must produce; Team's gate rejects anything else.
---
Write exactly this shape (no comments, no extra keys):

```json
{
  "tests": [
    {"file": "tests/test_widget.py", "covers": ["impl-1"],
     "cases": ["widget refuses an empty name", "widget round-trips through the ledger"]}
  ],
  "tasks": [
    {"id": "impl-1", "title": "Widget store", "does": "one paragraph",
     "files": ["team/widget.py"], "tests": ["tests/test_widget.py"]}
  ],
  "verify": ["uv run team widget list", "open team ui, Widgets tab shows the row"],
  "decisions": []
}
```

Rules the gate checks: every task has at least one file and one test file; no
file in two tasks; no test file listed under "files"; every path is inside the
repo; ids are `impl-<n>`. Test files are written by the test agent, not by
coders.
