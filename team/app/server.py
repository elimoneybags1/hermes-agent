"""Team's app over HTTP, on 127.0.0.1 only.

    team app [--port N]      default 8792; open http://127.0.0.1:8792/

Every /api request needs the token the page was served with (new each start:
reload the page after a restart) and this server's own Host header, so no
other web page can drive it and DNS rebinding cannot reach it.
"""

from __future__ import annotations

import json
import secrets
import sys
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from team import board, boss, catalog, ledger
from team.app import chat as chat_mod
from team.app import edit

STATIC = Path(__file__).resolve().parent / "static"
TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
         ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml"}


def _conn():
    conn = ledger.connect()
    ledger.init(conn)
    return conn


def _cat():
    try:
        return catalog.load()
    except catalog.GateError:
        return None


class App:
    """What the handler calls; one per server, so tests can build one without a socket."""

    def __init__(self, root: Path, chat: chat_mod.Chat | None = None):
        self.root = Path(root)
        self.chat = chat or chat_mod.Chat(self.root)
        self.token = secrets.token_urlsafe(24)

    def get(self, path: str, query: dict) -> dict:
        if path == "/api/board":
            with _conn() as conn:
                return board.board(conn, self.root)
        if path == "/api/inbox":
            with _conn() as conn:
                decisions = [d for d in ledger.list_decisions(conn) if not d["source"].startswith("boss-plan:")]
            return {"plans": [boss.card(p) for p in boss.proposals()[:30]], "decisions": decisions}
        if path == "/api/chat":
            return self.chat.view()
        if path == "/api/team":
            return edit.roster(self.root, _cat())
        if path == "/api/team/file":
            return edit.read(self.root, query.get("path", [""])[0])
        if path == "/api/gateway":
            return edit.gateway(self.root).overview()
        if path == "/api/history":
            return {"changes": edit.history(self.root)}
        raise edit.EditError("not found", 404)

    def post(self, path: str, d: dict) -> dict:
        parts = path.strip("/").split("/")
        if path == "/api/chat":
            self.chat.send(str(d.get("text") or ""))
            return {"ok": True}
        if path == "/api/chat/new":
            self.chat.new()
            return {"ok": True}
        if len(parts) == 4 and parts[1] == "plans" and parts[3] in ("approve", "dismiss"):
            return self._plan(int(parts[2]), "approve" if parts[3] == "approve" else None)
        if len(parts) == 4 and parts[1] == "decisions" and parts[3] in ("answer", "dismiss"):
            return self._settle(int(parts[2]), str(d.get("answer")) if parts[3] == "answer" else None)
        if path == "/api/team/settings":
            return {"path": edit.SETTINGS, "text": edit.settings_text(self.root, d.get("values") or {})}
        if path == "/api/team/review":
            return edit.review(self.root, str(d.get("path")), str(d.get("text")), _cat())
        if path == "/api/team/apply":
            return edit.apply(self.root, str(d.get("path")), str(d.get("text")), str(d.get("base")), _cat())
        if path == "/api/gateway/review":
            return edit.gateway_review(self.root, d.get("catalog"))
        if path == "/api/gateway/apply":
            return edit.gateway_apply(self.root, d.get("catalog"), str(d.get("base")))
        if path in ("/api/gateway/key", "/api/gateway/restart", "/api/gateway/probe"):
            gw = edit.gateway(self.root)
            if path.endswith("key"):
                return edit._gw_call(gw.save_key, str(d.get("file", "")), str(d.get("name", "")), str(d.get("value", "")))
            if path.endswith("restart"):
                return edit._gw_call(gw.restart, str(d.get("service", "")), json.loads(gw.catalog_text()))
            return edit._gw_call(gw.probe, str(d.get("provider", "")))
        if path == "/api/undo":
            return edit.undo(self.root, str(d.get("commit")), _cat())
        raise edit.EditError("not found", 404)

    def _plan(self, plan_id: int, answer: str | None) -> dict:
        p = next((x for x in boss.proposals() if x["id"] == plan_id), None)
        if p is None:
            raise edit.EditError(f"no plan {plan_id}", 404)
        if p["status"] != "waiting":
            raise edit.EditError(f"plan {plan_id} is {p['status']}", 409)
        return self._settle(p["decision_id"], answer)

    def _settle(self, decision_id: int, answer: str | None) -> dict:
        with _conn() as conn:
            try:
                boss.settle(conn, decision_id, answer)
            except boss.BossError as e:
                raise edit.EditError(str(e), 409) from None
        return {"ok": True}


def handler(app: App, hosts: set[str]):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # quiet; errors still go to stderr below
            pass

        def _send(self, status: int, body: bytes, kind: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, obj) -> None:
            self._send(status, json.dumps(obj).encode(), "application/json")

        def _allowed(self) -> bool:
            if self.headers.get("Host") not in hosts:
                self._json(403, {"error": "wrong Host"})
                return False
            if self.path.startswith("/api/") and not secrets.compare_digest(
                    self.headers.get("x-team-token", ""), app.token):
                self._json(403, {"error": "reload the page: the app restarted"})
                return False
            return True

        def _run(self, fn):
            try:
                self._json(200, fn())
            except edit.EditError as e:
                self._json(e.status, {"error": str(e)})
            except chat_mod.Busy as e:
                self._json(409, {"error": str(e)})
            except (ValueError, KeyError, LookupError) as e:
                self._json(400, {"error": f"{type(e).__name__}: {e}"})
            except Exception as e:  # the page shows it; the trace goes to the log
                traceback.print_exc(file=sys.stderr)
                self._json(500, {"error": f"{type(e).__name__}: {e}"})

        def do_GET(self):
            if not self._allowed():
                return
            url = urlparse(self.path)
            if url.path.startswith("/api/"):
                return self._run(lambda: app.get(url.path, parse_qs(url.query)))
            name = "index.html" if url.path in ("/", "/index.html") else url.path.removeprefix("/static/")
            f = (STATIC / name).resolve()
            if f.parent != STATIC or not f.is_file():
                return self._json(404, {"error": "not found"})
            body = f.read_bytes()
            if name == "index.html":
                body = body.replace(b"__TEAM_TOKEN__", app.token.encode())
            self._send(200, body, TYPES.get(f.suffix, "application/octet-stream"))

        def do_POST(self):
            if not self._allowed():
                return
            n = int(self.headers.get("Content-Length") or 0)
            try:
                d = json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                return self._json(400, {"error": "body is not JSON"})
            self._run(lambda: app.post(urlparse(self.path).path, d))

    return Handler


def serve(root: Path, port: int) -> None:
    app = App(root)
    hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    httpd = ThreadingHTTPServer(("127.0.0.1", port), handler(app, hosts))
    print(f"Team app on http://127.0.0.1:{port}/", flush=True)
    httpd.serve_forever()
