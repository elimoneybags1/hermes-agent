---
name: red-first
description: New tests must fail before the code exists, for the right reason.
---
Run the new tests. Each must fail with an assertion or an import/attribute
error naming the missing code, never a syntax error or a broken fixture. Then
run the whole suite: every other test must still pass. Commit only test files.
