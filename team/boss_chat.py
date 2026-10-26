"""One live boss behind `team ui`: the same Hermes-backed boss that plain `team`
starts, kept running by the dashboard server so the page can talk to it.

The process is `team/hermes_native.py` for role `boss`, launched exactly as
`hermes_runtime.launch` does (profile, subscription check, local inference
gateway, clean environment) but with its stdin and stdout piped. Its protocol
is the interactive one: it prints `Hermes-backed Team boss · <session id>`,
then `you> ` whenever it waits for a line; a reply is `boss> <text>` up to
the next prompt; a clarifying question ends in a bare `> ` prompt. The
session id is saved under `$TEAM_STATE/boss/` so a restart resumes the same
Hermes session, and the transcript is appended there as JSON lines so the page
keeps it across reloads.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import threading
import time
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path

from team import ledger

ROOT = Path(__file__).resolve().parent.parent
PROMPT_YOU = "you> "
PROMPT_ASK = "> "
REPLY_PREFIX = "boss> "
BANNER = "Hermes-backed Team boss · "
TRANSCRIPT_LIMIT = 200
STARTUP_S = 90.0


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def state_home() -> Path:
    return ledger.state_dir() / "boss"


_TOOL_LINE = re.compile(r"^[\s┊│├└─╰╭╮╯]*⚡\s*(\S+)")
_PROGRESS_CHARS = "┊│├└─╰╭╮╯⚡💬🔧⏳"


def split_reply(lines: list[str]) -> tuple[str, list[str]]:
    """Hermes prints its tool activity (`┊ ⚡ team 0.2s`, `💬 ...`) before the
    `boss> ` reply. Return the reply text and the tool names it used, in order."""
    reply_at = None
    for i, line in enumerate(lines):
        if line.startswith(REPLY_PREFIX):
            reply_at = i
    tools, head = [], []
    before = lines if reply_at is None else lines[:reply_at]
    for line in before:
        m = _TOOL_LINE.match(line)
        if m:
            tools.append(m.group(1))
            continue
        stripped = line.strip()
        if stripped and stripped[0] in _PROGRESS_CHARS:
            continue  # thinking notes and tree decoration
        head.append(line)
    if reply_at is None:
        text = "\n".join(head).strip()
    else:
        text = "\n".join(lines[reply_at:]).strip()[len(REPLY_PREFIX):].strip()
        extra = "\n".join(head).strip()
        if extra:
            text = extra + "\n\n" + text if text else extra
    return text, tools


class BossChat:
    """Owns one boss process. Thread-safe for one sender at a time.

    `command` replaces the native launch (tests pass a fake boss script);
    with it, no profile, subscription check or gateway is set up.
    """

    def __init__(self, root: Path = ROOT, cwd: Path | None = None, *, command: list[str] | None = None,
                 model: str = "opus", max_turns: int = 40, timeout_s: int = 1800) -> None:
        self.root = Path(root)
        self.cwd = Path(cwd or root)
        self.command = command
        self.model, self.max_turns, self.timeout_s = model, max_turns, timeout_s
        self.lock = threading.Lock()
        self.proc: subprocess.Popen | None = None
        self.stack: ExitStack | None = None
        self.reader: threading.Thread | None = None
        self.buffer = ""
        self.stderr_tail = ""
        self.busy = False
        self.awaiting = None  # "you" | "ask" | None while the boss is thinking
        self.session_id: str | None = None
        self.started_at: str | None = None
        self.last_error: str | None = None
        self.goals_before: int | None = None
        self.transcript: list[dict] = []
        self._load()

    # --- persistence

    def _load(self) -> None:
        home = state_home()
        try:
            data = json.loads((home / "ui-session.json").read_text())
            self.session_id = data.get("session_id") or None
        except (OSError, ValueError):
            self.session_id = None
        lines = []
        try:
            with (home / "ui-transcript.jsonl").open() as f:
                for line in f:
                    try:
                        lines.append(json.loads(line))
                    except ValueError:
                        continue
        except OSError:
            pass
        self.transcript = lines[-TRANSCRIPT_LIMIT:]

    def _save_session(self) -> None:
        home = state_home()
        home.mkdir(parents=True, exist_ok=True)
        (home / "ui-session.json").write_text(json.dumps({"session_id": self.session_id, "saved": _now_iso()}))

    def _append(self, who: str, text: str) -> dict:
        return self._append_entry({"ts": _now_iso(), "who": who, "text": text})

    def _append_entry(self, entry: dict) -> dict:
        self.transcript.append(entry)
        del self.transcript[:-TRANSCRIPT_LIMIT]
        home = state_home()
        try:
            home.mkdir(parents=True, exist_ok=True)
            with (home / "ui-transcript.jsonl").open("a") as f:
                f.write(json.dumps(entry) + "\n")
        except OSError:
            pass
        return entry

    def clear_transcript(self) -> None:
        self.transcript = []
        try:
            (state_home() / "ui-transcript.jsonl").unlink()
        except OSError:
            pass

    # --- lifecycle

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self, resume: bool = True) -> dict:
        """Start the boss (resuming the saved session when `resume`); no-op if alive."""
        with self.lock:
            if self.alive():
                return self.status()
            self.last_error = None
            if not resume:
                self.session_id = None
                self._save_session()
            try:
                self._spawn(self.session_id if resume else None)
            except Exception as e:  # noqa: BLE001 - shown on the page
                self.last_error = str(e)
                self._teardown()
                return self.status()
        if not self._wait_prompt(STARTUP_S):
            # A saved session the profile no longer knows makes the boss exit at once.
            if self.session_id and not self.alive():
                err = self.stderr_tail.strip().splitlines()[-1:] or ["boss exited during startup"]
                self._append("system", f"could not resume session {self.session_id}: {err[-1]}; starting a new one")
                with self.lock:
                    self._teardown()
                    self.session_id = None
                    self._save_session()
                    try:
                        self._spawn(None)
                    except Exception as e:  # noqa: BLE001
                        self.last_error = str(e)
                        self._teardown()
                        return self.status()
                if not self._wait_prompt(STARTUP_S):
                    self.last_error = self.stderr_tail.strip()[-500:] or "boss did not become ready"
            else:
                self.last_error = self.stderr_tail.strip()[-500:] or "boss did not become ready"
        return self.status()

    def _spawn(self, resume: str | None) -> None:
        self.buffer, self.stderr_tail, self.awaiting, self.busy = "", "", None, True
        self.stack = ExitStack()
        if self.command:
            cmd, env, cwd = list(self.command), dict(os.environ), self.cwd
            if resume:
                env["BOSS_RESUME"] = resume
        else:
            from team import claude_transport, hermes_runtime
            from team.subscription_gateway import serve

            home = hermes_runtime.prepare_profile("boss", self.root)
            claude_transport.subscription_ready(self.cwd)
            gateway = self.stack.enter_context(serve(self.cwd, timeout_s=self.timeout_s, trace_path=home / "inference.jsonl"))
            payload = dict(role="boss", root=str(self.root), cwd=str(self.cwd), prompt=None, resume=resume,
                           model=self.model, max_turns=self.max_turns, timeout_s=self.timeout_s,
                           initial_prompt=None, decision_file=None, result_file=None)
            with tempfile.NamedTemporaryFile(mode="w", suffix=".json", dir=home, delete=False) as f:
                json.dump(payload, f)
                payload_path = Path(f.name)
            self.stack.callback(payload_path.unlink, missing_ok=True)
            cmd = [str(hermes_runtime.python_executable()), str(ROOT / "team/hermes_native.py"), str(payload_path)]
            env, cwd = hermes_runtime.runtime_env(home, gateway), self.cwd
        self.proc = subprocess.Popen(cmd, cwd=cwd, env=env, text=True, bufsize=1,
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.started_at = _now_iso()
        self.reader = threading.Thread(target=self._read_stdout, daemon=True)
        self.reader.start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

    def _teardown(self) -> None:
        proc, self.proc = self.proc, None
        if proc is not None and proc.poll() is None:
            try:
                proc.stdin.write("/exit\n")
                proc.stdin.flush()
            except (OSError, ValueError):
                pass
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
        if self.stack is not None:
            self.stack.close()
            self.stack = None
        self.busy, self.awaiting = False, None

    def stop(self) -> dict:
        with self.lock:
            self._teardown()
        return self.status()

    def new_session(self) -> dict:
        """Forget the saved Hermes session and the transcript; start fresh."""
        with self.lock:
            self._teardown()
            self.session_id = None
            self._save_session()
            self.clear_transcript()
        return self.start(resume=False)

    # --- the protocol

    def _read_stderr(self) -> None:
        proc = self.proc
        if proc is None or proc.stderr is None:
            return
        for line in proc.stderr:
            self.stderr_tail = (self.stderr_tail + line)[-4000:]

    def _read_stdout(self) -> None:
        proc = self.proc
        if proc is None or proc.stdout is None:
            return
        while True:
            chunk = proc.stdout.read(1)
            if not chunk:
                break
            self.buffer += chunk
            self._scan()
        # The process ended: anything left is its last words.
        pending = getattr(self, "pending", [])
        self.pending = []
        rest = "\n".join([*pending, self.buffer]).strip()
        self.buffer = ""
        if rest.startswith(REPLY_PREFIX):
            rest = rest[len(REPLY_PREFIX):].strip()
        if rest:
            self._append("boss", rest[-2000:])
        if self.busy or rest:
            err = self.stderr_tail.strip().splitlines()
            self._append("system", "boss exited" + (f": {err[-1][:300]}" if err else ""))
        self.busy, self.awaiting = False, None

    def _scan(self) -> None:
        """Consume complete lines and prompts from the buffer."""
        while True:
            nl = self.buffer.find("\n")
            if nl < 0:
                break
            line, self.buffer = self.buffer[:nl], self.buffer[nl + 1:]
            if line.startswith(BANNER):
                sid = line[len(BANNER):].strip()
                if sid and sid != self.session_id:
                    self.session_id = sid
                    self._save_session()
                continue
            self._pending_line(line)
        if self.buffer.endswith(PROMPT_YOU):
            self.buffer = self.buffer[: -len(PROMPT_YOU)]
            self._turn_done("you")
        elif self.buffer == PROMPT_ASK or self.buffer.endswith("\n" + PROMPT_ASK):
            self.buffer = self.buffer[: -len(PROMPT_ASK)]
            self._turn_done("ask")

    def _pending_line(self, line: str) -> None:
        self.pending = getattr(self, "pending", [])
        self.pending.append(line)

    def _turn_done(self, kind: str) -> None:
        lines = getattr(self, "pending", [])
        self.pending = []
        text, tools = split_reply(lines)
        if text or tools:
            entry = {"ts": _now_iso(), "who": "boss",
                     "text": text if kind == "you" else text + "\n\n(the boss is asking; answer with a number or a sentence)"}
            if tools:
                entry["tools"] = tools
            self._append_entry(entry)
        self._note_new_goals()
        self.busy, self.awaiting = False, kind

    def _note_new_goals(self) -> None:
        """Goals the boss sent to the team during this turn, as system lines."""
        path = ledger.db_path()
        if not path.exists():
            return
        try:
            conn = ledger.connect(path)
        except Exception:  # noqa: BLE001
            return
        try:
            rows = conn.execute("SELECT id, target, role, goal FROM goals ORDER BY id").fetchall()
        except Exception:  # noqa: BLE001
            return
        finally:
            conn.close()
        if self.goals_before is None:
            self.goals_before = len(rows)
            return
        for r in rows[self.goals_before:]:
            title = r["goal"].splitlines()[0][:100] if r["goal"] else ""
            self._append("system", f"sent goal {r['id']} to {r['role']} on {r['target']}: {title}")
        self.goals_before = len(rows)

    def _wait_prompt(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.awaiting is not None:
                return True
            if not self.alive():
                return False
            time.sleep(0.05)
        return False

    def send(self, text: str) -> dict:
        """Give the boss one line. Refused while it is still answering."""
        text = text.strip()
        if not text:
            raise ValueError("say something")
        if "\n" in text:
            text = " ".join(text.split())
        with self.lock:
            if not self.alive():
                raise RuntimeError("the boss is not running; start it first")
            if self.busy:
                raise RuntimeError("the boss is still answering")
            if self.goals_before is None:
                self._note_new_goals()
            self.busy, self.awaiting = True, None
            self._append("you", text)
            try:
                self.proc.stdin.write(text + "\n")
                self.proc.stdin.flush()
            except (OSError, ValueError) as e:
                self.busy = False
                raise RuntimeError(f"could not reach the boss: {e}") from e
        return self.status()

    def status(self) -> dict:
        return {
            "alive": self.alive(),
            "busy": self.busy,
            "awaiting": self.awaiting,
            "session_id": self.session_id,
            "started_at": self.started_at,
            "error": self.last_error,
            "transcript": list(self.transcript),
        }

    def fingerprint(self) -> str:
        n = len(self.transcript)
        last = self.transcript[-1]["ts"] if self.transcript else ""
        return f"boss:{int(self.alive())}:{int(self.busy)}:{self.awaiting}:{n}:{last}:{self.last_error or ''}"
