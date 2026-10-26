"""A stand-in for team/hermes_native.py's interactive boss, for tests.

Speaks the same protocol: a banner with the session id, `you> ` when it waits
for a line, `boss> ` before a reply, and a bare `> ` after a clarifying
question. `BOSS_RESUME` in the environment is the session to resume; the word
`unknown` there makes it exit at once like the real boss does for a session
it does not have.
"""

import os
import sys

resume = os.environ.get("BOSS_RESUME")
if resume == "unknown-session":
    print("ValueError: Unknown Hermes session for this role", file=sys.stderr)
    sys.exit(1)
sid = resume or "fake-session-0001"
print(f"Hermes-backed Team boss · {sid}", flush=True)
while True:
    print("you> ", end="", flush=True)
    try:
        line = input()
    except EOFError:
        break
    if line.strip() in {"/exit", "/quit"}:
        break
    if line.startswith("ask"):
        print("Which one?", flush=True)
        print("1. alpha", flush=True)
        print("2. beta", flush=True)
        print("> ", end="", flush=True)
        answer = input()
        print(f"boss> you picked {answer}", flush=True)
        continue
    if line.startswith("multi"):
        print("boss> first line\nsecond line", flush=True)
        continue
    if line.startswith("tools"):
        print("  ┊ ⚡ team        0.0s", flush=True)
        print("  ┊ 💬 The team CLI is broken, checking the board instead.", flush=True)
        print("  ┊ ⚡ kanban_list 0.0s", flush=True)
        print("boss> one task is running", flush=True)
        continue
    if line.startswith("slow"):
        import time
        time.sleep(1.5)
        print("boss> that took a while", flush=True)
        continue
    if line.startswith("crash"):
        print("boss> going down", flush=True)
        sys.exit(3)
    print(f"boss> echo: {line}", flush=True)
