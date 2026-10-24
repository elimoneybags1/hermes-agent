---
name: real-not-fake
description: Test against the real thing; how fakes hid real bugs before.
---
Seen in this repo (ledger job 18): tests passed against a fake `hermes` while
the code used flags that do not exist (`--format json`, `--skills`) and parsed
plain-text output as JSON. Before faking any command:
1. Run its real `--help` and copy the exact flags.
2. Run it once for real and save its real output as the fake's canned output.
3. Prefer the real thing whenever it is local and free: git in tmp_path,
   sqlite in tmp_path, a server on port 0.
