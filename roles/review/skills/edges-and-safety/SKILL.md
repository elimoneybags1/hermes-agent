---
name: edges-and-safety
description: What earlier reviews in this ledger caught that tests did not.
---
Check every input that names a path, a command or an id: path traversal
(job 10), shell strings that fail open (job 6), an unknown name silently
doing something (job 5). Errors must map to the right status, not be masked
(job 11). No test-only hooks in production code (job 19). Shared state across
clients or requests (job 11).
