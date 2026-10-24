"""`team ui`: the agent dashboard, served on 127.0.0.1 with stdlib http.server.

Routes and rules are the ones in docs/UI.md. The page itself is
team/ui/index.html; this module only serves it and the JSON API.
"""

from __future__ import annotations

import json
import select
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from team import agents, dash, ledger, settings, targets
from team.roles import load_roles

DEFAULT_PORT = 7341
INDEX = Path(__file__).resolve().parent / "ui" / "index.html"
POLL_S = 1.0
PING_S = 15.0


class UIServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        root: Path,
        db: Path,
        index: Path = INDEX,
        poll_s: float = POLL_S,
        ping_s: float = PING_S,
    ) -> None:
        super().__init__(address, Handler)
        self.root = root
        self.db = db
        self.index = index
        self.poll_s = poll_s
        self.ping_s = ping_s
        # Writes go through git, and two commits at once fight over the index.
        self.write_lock = threading.Lock()
        self.stopping = threading.Event()
        self.streams = 0
        self.streams_lock = threading.Lock()

    def shutdown(self) -> None:
        self.stopping.set()
        super().shutdown()

    def connect(self):
        """A fresh ledger connection per request (sqlite3 objects stay in one thread)."""
        return ledger.connect(self.db) if self.db.exists() else None


def make_server(
    root: Path,
    db: Path,
    port: int = DEFAULT_PORT,
    index: Path = INDEX,
    poll_s: float = POLL_S,
    ping_s: float = PING_S,
) -> UIServer:
    """Bind on 127.0.0.1:<port> (0 picks a free one); call serve_forever() to run."""
    return UIServer(("127.0.0.1", port), root, db, index, poll_s, ping_s)


class HTTPError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


class Handler(BaseHTTPRequestHandler):
    server: UIServer

    def do_GET(self) -> None:
        self._handle("GET")

    def do_PATCH(self) -> None:
        self._handle("PATCH")

    def do_PUT(self) -> None:
        self._handle("PUT")

    def do_POST(self) -> None:
        self._handle("POST")

    def do_DELETE(self) -> None:
        self._handle("DELETE")

    def _handle(self, method: str) -> None:
        try:
            self._check_host()
            parts = [unquote(p) for p in urlsplit(self.path).path.split("/") if p]
            route = self._route(method, parts)
            if route is None:
                raise HTTPError(404, f"not found: {method} {self.path}")
            if method != "GET":
                self._check_json()
            route()
        except HTTPError as e:
            self._json(e.status, {"error": str(e)})
        except agents.AgentError as e:
            self._json(400, {"error": str(e)})
        except Exception as e:  # noqa: BLE001 - the page shows the text
            self._json(500, {"error": str(e) or type(e).__name__})

    def _check_host(self) -> None:
        port = self.server.server_address[1]
        allowed = {f"127.0.0.1:{port}", f"localhost:{port}"}
        if self.headers.get("Host") not in allowed:
            raise HTTPError(403, "forbidden host")

    def _check_json(self) -> None:
        ctype = self.headers.get("Content-Type", "")
        if ctype.split(";")[0].strip().lower() != "application/json":
            raise HTTPError(415, "Content-Type must be application/json")

    def _route(self, method: str, parts: list[str]):
        if method == "GET" and parts == []:
            return self._index
        if parts[:1] != ["api"]:
            return None
        rest = parts[1:]
        if method == "GET" and rest == ["state"]:
            return self._state
        if method == "GET" and rest == ["events"]:
            return self._events
        # Settings routes (before agents check)
        if method == "GET" and rest == ["settings"]:
            return self._settings
        if method == "PATCH" and rest == ["settings"]:
            return lambda: self._set_setting()
        if len(rest) == 2 and rest[0] == "targets":
            if method == "PATCH":
                return lambda: self._set_target(rest[1])
        # Agents routes
        if len(rest) < 2 or rest[0] != "agents":
            return None
        name, tail = rest[1], rest[2:]
        if method == "GET" and tail == []:
            return lambda: self._detail(name)
        if method == "PATCH" and tail == ["role"]:
            return lambda: self._set_field(name)
        if method == "PUT" and tail == ["instructions"]:
            return lambda: self._write_instructions(name)
        if len(tail) == 2 and tail[0] == "skills":
            if method == "PUT":
                return lambda: self._write_skill(name, tail[1])
            if method == "DELETE":
                return lambda: self._write(agents.delete_skill, name, tail[1])
        if method == "POST" and tail == ["revert"]:
            return lambda: self._revert(name)
        return None

    # --- reads

    def _index(self) -> None:
        if not self.server.index.is_file():
            raise HTTPError(404, f"no page at {self.server.index}")
        body = self.server.index.read_bytes()
        self._send(200, "text/html; charset=utf-8", body)

    def _state(self) -> None:
        roles = load_roles(self.server.root)
        conn = self.server.connect()
        try:
            data = dash.snapshot(conn, roles)
        finally:
            if conn is not None:
                conn.close()
        data["pipeline"] = agents.pipeline(roles)
        self._json(200, data)

    def _detail(self, name: str) -> None:
        conn = self.server.connect()
        try:
            data = agents.detail(self.server.root, conn, name)
        finally:
            if conn is not None:
                conn.close()
        self._json(200, data)

    # --- writes

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw or b"{}")
        except json.JSONDecodeError as e:
            raise HTTPError(400, f"invalid JSON: {e}") from e
        if not isinstance(body, dict):
            raise HTTPError(400, "body must be a JSON object")
        return body

    def _need(self, body: dict, key: str) -> Any:
        if key not in body:
            raise HTTPError(400, f"missing {key!r}")
        return body[key]

    def _need_str(self, body: dict, key: str) -> str:
        value = self._need(body, key)
        if not isinstance(value, str):
            raise HTTPError(400, f"{key!r} must be a string")
        return value

    def _write(self, fn, *args: Any) -> None:
        with self.server.write_lock:
            sha = fn(self.server.root, *args)
        self._json(200, {"sha": sha})

    def _set_field(self, name: str) -> None:
        body = self._body()
        field = self._need_str(body, "field")
        self._write(agents.set_field, name, field, self._need(body, "value"))

    def _write_instructions(self, name: str) -> None:
        text = self._need_str(self._body(), "text")
        self._write(agents.write_instructions, name, text)

    def _write_skill(self, name: str, skill: str) -> None:
        text = self._need_str(self._body(), "text")
        self._write(agents.write_skill, name, skill, text)

    def _revert(self, name: str) -> None:
        sha = self._need_str(self._body(), "sha")
        self._write(agents.revert, name, sha)

    # --- settings

    def _settings(self) -> None:
        target_names = [t["name"] for t in targets.list_targets()]
        self._json(
            200,
            {
                "settings": settings.load(self.server.root),
                "fields": settings.fields(),
                "targets": [settings.target_settings(n) for n in target_names],
                "target_fields": [
                    {"key": k, "type": t.__name__, "help": h}
                    for k, (t, _, h) in settings.TARGET_FIELDS.items()
                ],
            },
        )

    def _set_setting(self) -> None:
        body = self._body()
        key = self._need_str(body, "key")
        with self.server.write_lock:
            sha = settings.set_value(self.server.root, key, self._need(body, "value"))
        self._json(200, {"sha": sha})

    def _set_target(self, name: str) -> None:
        body = self._body()
        key = self._need_str(body, "key")
        result = settings.set_target(name, key, self._need(body, "value"))
        self._json(200, result)

    # --- live events

    def _events(self) -> None:
        self.close_connection = True
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        server = self.server
        with server.streams_lock:
            server.streams += 1
        last = None  # this connection's last fingerprint
        next_ping = time.monotonic() + server.ping_s
        try:
            while not server.stopping.is_set():
                fp = agents.fingerprint(server.root, server.db)
                if fp != last:
                    self._event(f"data: {json.dumps({'fingerprint': fp})}\n\n")
                    last = fp
                if time.monotonic() >= next_ping:
                    self._event(": ping\n\n")
                    next_ping = time.monotonic() + server.ping_s
                if self._client_gone(server.poll_s):
                    return
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return
        finally:
            with server.streams_lock:
                server.streams -= 1

    def _event(self, text: str) -> None:
        self.wfile.write(text.encode())
        self.wfile.flush()

    def _client_gone(self, timeout: float) -> bool:
        """Wait up to timeout; True if the client closed its end meanwhile."""
        readable, _, _ = select.select([self.connection], [], [], timeout)
        if not readable:
            return False
        try:
            return self.connection.recv(1024) == b""
        except OSError:
            return True

    # --- responses

    def _json(self, status: int, data: Any) -> None:
        body = json.dumps(data, default=str).encode()
        self._send(status, "application/json", body)

    def _send(self, status: int, ctype: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def main(port: int = DEFAULT_PORT, open_browser: bool = True) -> int:
    root = Path(__file__).resolve().parent.parent
    server = make_server(root, ledger.db_path(), port)
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    print(f"team ui at {url}")
    if open_browser:
        import webbrowser

        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
