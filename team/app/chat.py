"""The boss chat behind Team's app.

Each message runs one Hermes one-shot on the boss profile (``--format
stream-json``) and resumes the conversation by its session id, so the boss
keeps its context between messages exactly as in ``team boss``. The boss's
``clarify`` tool needs a terminal, so here it asks its questions in its reply.

The transcript lives in ``$TEAM_STATE/boss/chat.json``. Hermes reports tokens
as running totals for the session; each boss message stores its own share
(the difference from the previous message), with cache read and write.
"""

from __future__ import annotations

import json
import subprocess
import threading
import time
from pathlib import Path

from team import ledger

TOOLSETS = "team,memory,session_search"
TOKEN_KEYS = ("input", "output", "cache_read", "cache_write")


class Busy(Exception):
    """The boss is still answering the previous message."""


def state() -> Path:
    return ledger.state_dir() / "boss"


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def argv(root: Path, text: str, session: str | None) -> list[str]:
    cmd = [
        str(root / "team" / "bin" / "team-hermes"), "-p", "boss", "chat",
        "-q", text, "--oneshot", "--format", "stream-json", "-t", TOOLSETS,
    ]
    return cmd + (["--resume", session] if session else [])


def share(now: dict, before: dict | None) -> dict:
    """This message's tokens from two running totals; a counter that went down restarted."""
    out = {}
    for k in TOKEN_KEYS:
        a, b = int(now.get(k) or 0), int((before or {}).get(k) or 0)
        out[k] = a - b if a >= b else a
    return out


class Chat:
    def __init__(self, root: Path, popen=subprocess.Popen, path: Path | None = None):
        self.root = Path(root)
        self.popen = popen
        self.path = path or state() / "chat.json"
        self.lock = threading.Lock()
        self.current: dict | None = None
        self.data = self._load()

    def _load(self) -> dict:
        if self.path.exists():
            return json.loads(self.path.read_text())
        return {"session": None, "totals": None, "messages": []}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=1))
        tmp.replace(self.path)

    def view(self) -> dict:
        with self.lock:
            return {
                "session": self.data["session"],
                "messages": list(self.data["messages"]),
                "current": dict(self.current) if self.current else None,
            }

    def send(self, text: str, wait: bool = False) -> None:
        text = text.strip()
        if not text:
            raise ValueError("say something")
        with self.lock:
            if self.current is not None:
                raise Busy("the boss is still answering")
            self.data["messages"].append({"role": "you", "text": text, "at": _now()})
            self._save()
            self.current = {"role": "boss", "text": "", "tools": [], "at": _now()}
            session = self.data["session"]
        t = threading.Thread(target=self._run, args=(text, session), daemon=True)
        t.start()
        if wait:
            t.join()

    def new(self) -> None:
        """Start a fresh conversation; the old transcript is kept beside this one."""
        with self.lock:
            if self.current is not None:
                raise Busy("the boss is still answering")
            if self.data["messages"]:
                old = self.path.with_name(f"chat-{int(time.time())}.json")
                old.write_text(json.dumps(self.data, indent=1))
            self.data = {"session": None, "totals": None, "messages": []}
            self._save()

    def _event(self, e: dict) -> dict | None:
        kind = e.get("type")
        with self.lock:
            cur = self.current
            if kind == "text":
                cur["text"] += e.get("text", "")
            elif kind == "tool_use":
                cur["tools"].append({"name": e.get("name"), "input": e.get("input"), "done": False})
            elif kind == "tool_result":
                for t in reversed(cur["tools"]):
                    if t["name"] == e.get("name") and not t["done"]:
                        t["done"] = True
                        t["seconds"] = e.get("duration_s") or e.get("duration")
                        break
        return e if kind == "result" else None

    def _run(self, text: str, session: str | None) -> None:
        result, err = None, ""
        try:
            proc = self.popen(
                argv(self.root, text, session), cwd=str(state()),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            for line in proc.stdout:
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                result = self._event(e) or result
            err = proc.stderr.read()
            proc.wait()
        except OSError as e:
            err = str(e)
        with self.lock:
            msg = self.current
            self.current = None
            if result and result.get("exit_code") == 0:
                msg["text"] = result.get("text") or msg["text"]
                totals = result.get("tokens") or {}
                same = result.get("session_id") == self.data["session"]
                msg["tokens"] = share(totals, self.data["totals"] if same else None)
                self.data["session"] = result.get("session_id") or self.data["session"]
                self.data["totals"] = totals
            else:
                tail = [x for x in err.splitlines() if x.strip() and not x.startswith(("✓", "→", "["))]
                why = (result or {}).get("error") or " ".join(tail[-3:]) or "the boss run failed"
                msg["error"] = why[:600]
            msg["done"] = _now()
            self.data["messages"].append(msg)
            self._save()
