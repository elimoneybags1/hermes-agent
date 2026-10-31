"""Keeps a dispatched worker's calls light: Team's ``pre_tool_call`` hook.

Every tool call a worker makes is one more API request that re-sends the whole
conversation, so a wasted call costs the full context, not just its result.
Measured over one day of implementer sessions, 27 of 64 ``read_file`` calls
re-read a file already in the context, and ``kanban_show`` returned the
worker's own task card (18,700 chars on average) eight times.

This hook is active only inside a kanban-dispatched worker (``HERMES_KANBAN_TASK``
is set; the boss and the lead never see it) and refuses two kinds of call. The
refusal text becomes the tool's result, so the model reads why and moves on.

- Kanban tools other than the hand-off ones: the brief in the first message is
  the whole card.
- ``read_file`` with the same arguments as an earlier read in this session,
  when the file has not changed since (size and mtime): it is already in the
  context.
"""

from __future__ import annotations

import os
from typing import Any

HANDOFF = frozenset({"kanban_request_review", "kanban_block"})
WRITERS = frozenset({"write_file", "patch"})

CARD_MSG = (
    "Refused: the first message of this conversation is your whole task card, with the "
    "code you need copied in. Nothing more is on the board. Follow its steps; when the "
    "check passes and your work is committed, call kanban_request_review."
)
COMPLETE_MSG = (
    "Refused: never call kanban_complete, Team grades the work. When the check passes "
    "and your work is committed, call kanban_request_review."
)
REREAD_MSG = (
    "Refused: you already read {what} in this conversation and nothing has changed it "
    "since. Its contents are above; use them instead of reading again."
)

# session id -> {read key: (size, mtime_ns)} for files read so far in that session.
_seen: dict[str, dict[tuple, tuple[int, int] | None]] = {}


def active(env: dict | None = None) -> bool:
    return bool((os.environ if env is None else env).get("HERMES_KANBAN_TASK"))


def _stat(path: str) -> tuple[int, int] | None:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_size, st.st_mtime_ns)


def _key(args: dict) -> tuple | None:
    path = str(args.get("path") or "")
    if not path:
        return None
    return (os.path.normpath(path), args.get("offset"), args.get("limit"))


def decide(tool_name: str, args: dict | None, session_id: str = "") -> dict | None:
    """The hook's verdict for one call: a block directive, or None to let it run."""
    args = args if isinstance(args, dict) else {}
    if tool_name.startswith("kanban_") and tool_name not in HANDOFF:
        return {"action": "block", "message": COMPLETE_MSG if tool_name == "kanban_complete" else CARD_MSG}
    seen = _seen.setdefault(session_id or "", {})
    if tool_name in WRITERS:
        path = os.path.normpath(str(args.get("path") or ""))
        for key in [k for k in seen if k[0] == path]:
            del seen[key]
        return None
    if tool_name == "read_file":
        key = _key(args)
        if key is None:
            return None
        now = _stat(key[0])
        if key in seen and now is not None and seen[key] == now:
            where = key[0] if key[1] is None else f"{key[0]} from line {key[1]}"
            return {"action": "block", "message": REREAD_MSG.format(what=where)}
        seen[key] = now
    return None


def pre_tool_call(tool_name: str = "", args: Any = None, session_id: str = "", **_kw) -> dict | None:
    if not active():
        return None
    return decide(str(tool_name), args, str(session_id or ""))


def forget(session_id: str | None = None) -> None:
    """Drop what was seen (tests); one session, or everything."""
    if session_id is None:
        _seen.clear()
    else:
        _seen.pop(session_id, None)
