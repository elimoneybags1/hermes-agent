"""The conversation behind Team's app: the aide answers, the boss plans.

Two lanes, each a Hermes profile resumed by its session id, so each keeps its
context between messages exactly as in ``team boss``:

- **aide** (``team/roles/aide``, a free model with read-only Team tools):
  every message Eli types goes here first, unless he aimed it at the boss or
  ``aide.enabled`` is off. A reply that starts with ``HANDOFF`` sends the
  message on to the boss unchanged, and so does a failed aide run.
- **boss** (Claude on the subscription): what the aide handed off, what Eli
  aimed at it, and Team's own notes to it (a question he answered, a goal he
  sent back). Boss messages queue while it is answering and are merged into
  one message when dispatched, with the aide's answers since the boss's last
  turn as context, so one boss turn costs one boss turn.

The transcript lives in ``$TEAM_STATE/boss/chat.json``. Hermes reports tokens
as running totals per session; each message stores its own share (the
difference from the previous message), with cache read and write.
"""

from __future__ import annotations

import json
import re
import subprocess
import threading
import time
from pathlib import Path

from team import ledger, profiles, settings

LANES = ("aide", "boss")
TOOLSETS = {"boss": "team,memory,session_search", "aide": "team-read"}
TOKEN_KEYS = ("input", "output", "cache_read", "cache_write")
HANDOFF = re.compile(r"^\s*\**HANDOFF\b", re.IGNORECASE)
CONTEXT_CHARS = 400


class Busy(Exception):
    """The conversation cannot be reset while someone is still answering."""


def state() -> Path:
    return ledger.state_dir() / "boss"


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def argv(root: Path, text: str, session: str | None, lane: str = "boss") -> list[str]:
    cmd = [
        str(root / "team" / "bin" / "team-hermes"), "-p", lane, "chat",
        "-q", text, "--oneshot", "--format", "stream-json", "-t", TOOLSETS[lane],
    ]
    return cmd + (["--resume", session] if session else [])


def share(now: dict, before: dict | None) -> dict:
    """This message's tokens from two running totals; a counter that went down restarted."""
    out = {}
    for k in TOKEN_KEYS:
        a, b = int(now.get(k) or 0), int((before or {}).get(k) or 0)
        out[k] = a - b if a >= b else a
    return out


def _lane() -> dict:
    return {"session": None, "totals": None}


class Chat:
    def __init__(self, root: Path, popen=subprocess.Popen, path: Path | None = None, aide: bool | None = None):
        self.root = Path(root)
        self.popen = popen
        self.path = path or state() / "chat.json"
        self.aide = aide  # None: from settings and whether the aide's profile exists
        self.lock = threading.Lock()
        self.current: dict[str, dict | None] = {lane: None for lane in LANES}
        self.queue: dict[str, list[str]] = {lane: [] for lane in LANES}
        self.threads: dict[str, threading.Thread | None] = {lane: None for lane in LANES}
        self.running: dict[str, bool] = {lane: False for lane in LANES}
        self.data = self._load()

    # --- state ---------------------------------------------------------------

    def _load(self) -> dict:
        if not self.path.exists():
            return {"lanes": {lane: _lane() for lane in LANES}, "messages": []}
        d = json.loads(self.path.read_text())
        if "lanes" not in d:  # saved when the boss was the only lane
            d = {
                "lanes": {"boss": {"session": d.get("session"), "totals": d.get("totals")}, "aide": _lane()},
                "messages": d.get("messages", []),
            }
        return d

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=1))
        tmp.replace(self.path)

    def aide_on(self) -> bool:
        if self.aide is not None:
            return self.aide
        try:
            return bool(settings.load(self.root)["aide.enabled"]) and profiles.profile_dir("aide").exists()
        except Exception:
            return False

    def view(self) -> dict:
        with self.lock:
            return {
                "session": self.data["lanes"]["boss"]["session"],
                "messages": list(self.data["messages"]),
                "current": dict(self.current["boss"]) if self.current["boss"] else None,
                "aide_current": dict(self.current["aide"]) if self.current["aide"] else None,
                "queued": {lane: len(self.queue[lane]) for lane in LANES},
                "aide": self.aide_on(),
            }

    def busy(self) -> bool:
        with self.lock:
            return any(self.current.values()) or any(self.queue.values())

    # --- sending -------------------------------------------------------------

    def send(self, text: str, to: str = "auto", wait: bool = False) -> str:
        """Eli's message. ``to`` is ``auto`` (the aide first, when on), ``boss`` or ``aide``.
        Returns the lane it went to."""
        text = text.strip()
        if not text:
            raise ValueError("say something")
        if to not in ("auto", *LANES):
            raise ValueError(f"to must be auto, boss or aide, not {to!r}")
        lane = to if to in LANES else ("aide" if self.aide_on() else "boss")
        self._enqueue(lane, text, {"role": "you", "text": text, "at": _now(), "to": lane})
        if wait:
            self.drain()
        return lane

    def note(self, text: str, wait: bool = False) -> None:
        """Team's own message to the boss: an answered question, a goal sent back."""
        self._enqueue("boss", text, {"role": "you", "text": text, "at": _now(), "to": "boss", "kind": "note"})
        if wait:
            self.drain()

    def _enqueue(self, lane: str, text: str, message: dict | None) -> None:
        with self.lock:
            if message is not None:
                self.data["messages"].append(message)
                self._save()
            self.queue[lane].append(text)
            self._kick(lane)

    def _kick(self, lane: str) -> None:
        """Start the lane's worker if none is running. Called with the lock held."""
        if not self.running[lane]:
            self.running[lane] = True
            t = threading.Thread(target=self._worker, args=(lane,), daemon=True)
            self.threads[lane] = t
            t.start()

    def drain(self) -> None:
        """Wait until every lane is idle and nothing is queued (tests, the CLI)."""
        while True:
            with self.lock:
                threads = [t for t in self.threads.values() if t is not None and t.is_alive()]
                if not threads and not any(self.running.values()):
                    return
            for t in threads:
                t.join()

    def new(self) -> None:
        """Start a fresh conversation; the old transcript is kept beside this one."""
        with self.lock:
            if any(self.current.values()) or any(self.queue.values()):
                raise Busy("someone is still answering")
            if self.data["messages"]:
                old = self.path.with_name(f"chat-{int(time.time())}.json")
                old.write_text(json.dumps(self.data, indent=1))
            self.data = {"lanes": {lane: _lane() for lane in LANES}, "messages": []}
            self._save()

    # --- the lanes -----------------------------------------------------------

    def _worker(self, lane: str) -> None:
        while True:
            with self.lock:
                if not self.queue[lane]:
                    self.running[lane] = False  # in the same critical section as the check
                    return
                if lane == "boss":
                    text, self.queue["boss"] = self._boss_message(self.queue["boss"]), []
                else:
                    text = self.queue["aide"].pop(0)
                self.current[lane] = {"role": lane, "text": "", "tools": [], "at": _now()}
                session = self.data["lanes"][lane]["session"]
            msg = self._run(lane, text, session)
            if lane == "aide" and (msg.get("handoff") or msg.get("error")):
                with self.lock:
                    for m in reversed(self.data["messages"]):
                        if m["role"] == "you" and m["text"] == text and m.get("to") == "aide":
                            m["to"] = "boss"
                            break
                    self._save()
                self._enqueue("boss", text, None)

    def _boss_message(self, queued: list[str]) -> str:
        """Everything queued for the boss as one message, with the aide's answers since its
        last turn as context. Called with the lock held."""
        context = []
        for m in reversed(self.data["messages"]):
            if m["role"] == "boss":
                break
            if m["role"] == "aide" and not m.get("handoff") and not m.get("error") and not m.get("relayed"):
                m["relayed"] = True
                context.append(m)
        body = "\n\n".join(queued)
        if not context:
            return body
        lines = ["(Since your last turn, the aide answered Eli from the ledger; for context only.)"]
        for m in reversed(context):
            q = next((x["text"] for x in reversed(self.data["messages"][: self.data["messages"].index(m)])
                      if x["role"] == "you"), "")
            lines.append(f"- Eli: {q[:CONTEXT_CHARS]}\n  aide: {' '.join(m['text'].split())[:CONTEXT_CHARS]}")
        return "\n".join(lines) + "\n\n" + body

    def _event(self, lane: str, e: dict) -> dict | None:
        kind = e.get("type")
        with self.lock:
            cur = self.current[lane]
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

    def _run(self, lane: str, text: str, session: str | None) -> dict:
        result, err = None, ""
        started = ledger._now()  # the ledger's clock, for the model lookup
        try:
            proc = self.popen(
                argv(self.root, text, session, lane), cwd=str(state()),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            for line in proc.stdout:
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                result = self._event(lane, e) or result
            err = proc.stderr.read()
            proc.wait()
        except OSError as e:
            err = str(e)
        with self.lock:
            msg = self.current[lane]
            self.current[lane] = None
            state_ = self.data["lanes"][lane]
            if result and result.get("exit_code") == 0:
                msg["text"] = result.get("text") or msg["text"]
                totals = result.get("tokens") or {}
                same = result.get("session_id") == state_["session"]
                msg["tokens"] = share(totals, state_["totals"] if same else None)
                state_["session"] = result.get("session_id") or state_["session"]
                state_["totals"] = totals
                if lane == "aide":
                    msg["handoff"] = bool(HANDOFF.match(msg["text"]))
                    msg["model"] = _model("aide", started)
            else:
                tail = [x for x in err.splitlines() if x.strip() and not x.startswith(("✓", "→", "["))]
                why = (result or {}).get("error") or " ".join(tail[-3:]) or f"the {lane} run failed"
                msg["error"] = why[:600]
            msg["done"] = _now()
            self.data["messages"].append(msg)
            self._save()
            return dict(msg)


def _model(profile: str, since: str) -> str | None:
    """The model that answered for ``profile`` since ``since``, from the ledger's calls."""
    try:
        conn = ledger.connect()
        try:
            row = conn.execute(
                "SELECT COALESCE(response_model, model) FROM calls WHERE profile = ? AND ts >= ?"
                " ORDER BY id DESC LIMIT 1",
                (profile, since),
            ).fetchone()
        finally:
            conn.close()
    except Exception:
        return None
    return row[0] if row else None
