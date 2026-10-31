"""Team's app: the boss chat, review-then-apply edits, and the server's gates."""

import http.client
import json
import shutil
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from team.app import chat, edit, server
from tests.team.test_catalog import CATALOG
from tests.team.test_run import git

REPO = Path(__file__).resolve().parents[2]


class Proc:
    def __init__(self, events, err=""):
        self.stdout = iter(json.dumps(e) + "\n" for e in events)
        self.stderr = type("E", (), {"read": lambda s: err})()

    def wait(self):
        return 0


def popen_with(*runs):
    calls = []

    def popen(argv, **kw):
        calls.append(argv)
        return runs[len(calls) - 1]

    popen.calls = calls
    return popen


def result(sid, text, inp, out, cr, cw):
    return {"type": "result", "session_id": sid, "exit_code": 0, "text": text,
            "tokens": {"input": inp, "output": out, "cache_read": cr, "cache_write": cw}}


def test_chat_resumes_the_session_and_stores_each_messages_own_tokens(team_state, tmp_path):
    popen = popen_with(
        Proc([{"type": "text", "text": "Which "}, {"type": "text", "text": "target?"},
              result("s1", "Which target?", 4738, 4, 0, 4394)]),
        Proc([{"type": "tool_use", "name": "team_status", "input": {"target": "team"}},
              {"type": "tool_result", "name": "team_status", "duration_s": 1.5},
              result("s1", "Proposed.", 9495, 12, 4394, 4753)]),
    )
    c = chat.Chat(tmp_path, popen=popen)
    c.send("Add a test", wait=True)
    c.send("team", wait=True)
    first, second = popen.calls
    assert "--resume" not in first and first[first.index("-t") + 1] == "team,memory,session_search"
    assert second[-2:] == ["--resume", "s1"]
    msgs = c.view()["messages"]
    assert [m["role"] for m in msgs] == ["you", "boss", "you", "boss"]
    assert msgs[1]["text"] == "Which target?"
    assert msgs[1]["tokens"] == {"input": 4738, "output": 4, "cache_read": 0, "cache_write": 4394}
    assert msgs[3]["tokens"] == {"input": 4757, "output": 8, "cache_read": 4394, "cache_write": 359}
    assert msgs[3]["tools"] == [{"name": "team_status", "input": {"target": "team"}, "done": True, "seconds": 1.5}]
    assert json.loads((team_state / "boss" / "chat.json").read_text())["session"] == "s1"


def test_chat_shows_a_failed_run_and_refuses_while_busy(team_state, tmp_path):
    c = chat.Chat(tmp_path, popen=popen_with(Proc([], err="✓ venv\nError: gateway refused\n")))
    c.send("hi", wait=True)
    assert c.view()["messages"][-1]["error"] == "Error: gateway refused"
    c.current = {"role": "boss", "text": "", "tools": []}
    with pytest.raises(chat.Busy):
        c.send("again")
    with pytest.raises(ValueError):
        chat.Chat(tmp_path, popen=None).send("  ")


@pytest.fixture
def root(tmp_path, team_state):
    r = tmp_path / "root"
    shutil.copytree(REPO / "team" / "roles", r / "team" / "roles")
    git(r, "init", "-q", "-b", "main")
    git(r, "config", "user.name", "T")
    git(r, "config", "user.email", "t@t")
    git(r, "add", "-A")
    git(r, "commit", "-q", "-m", "roles")
    return r


def test_review_shows_the_diff_and_refuses_what_team_would_refuse(root):
    rel = "team/roles/boss/role.toml"
    text = (root / rel).read_text()
    ok = edit.review(root, rel, text.replace("max_turns = 20", "max_turns = 25"), CATALOG)
    assert ok["problems"] == [] and "+max_turns = 25" in ok["diff"] and not ok["dirty"]
    bad = edit.review(root, rel, text.replace('harness = "hermes-subscription"', 'harness = "codex"'), CATALOG)
    assert bad["problems"] and "harness" in bad["problems"][0]
    assert edit.review(root, "team/roles/boss/SOUL.md", " ", CATALOG)["problems"]
    with pytest.raises(edit.EditError):
        edit.read(root, "team/run.py")
    with pytest.raises(edit.EditError):
        edit.read(root, "team/roles/boss/../../../etc/passwd")


def test_apply_writes_syncs_and_commits_one_file_and_undo_puts_it_back(root):
    rel = "team/roles/boss/role.toml"
    f = edit.read(root, rel)
    synced = []
    new = f["text"].replace("max_turns = 20", "max_turns = 25")
    res = edit.apply(root, rel, new, f["base"], CATALOG, sync=lambda r, n, c: synced.append(n) or ["config"])
    assert synced == ["boss"] and (root / rel).read_text() == new
    assert git(root, "log", "-1", "--format=%s").stdout.strip() == f"Team app: edit {rel}"
    [h] = edit.history(root)
    assert h["commit"] == res["commit"] and h["path"] == rel
    with pytest.raises(edit.EditError, match="changed on disk"):
        edit.apply(root, rel, new, f["base"], CATALOG, sync=lambda *a: [])
    edit.undo(root, res["commit"], CATALOG, sync=lambda *a: [])
    assert (root / rel).read_text() == f["text"]


def test_apply_refuses_a_file_with_someone_elses_uncommitted_changes(root):
    rel = "team/roles/lead/SOUL.md"
    (root / rel).write_text((root / rel).read_text() + "\nanother session's line\n")
    f = edit.read(root, rel)
    with pytest.raises(edit.EditError, match="uncommitted"):
        edit.apply(root, rel, f["text"] + "mine\n", f["base"], CATALOG, sync=lambda *a: [])


def test_settings_are_typed(root):
    text = edit.settings_text(root, {"lead.max_parts": 4})
    assert "max_parts = 4" in text
    with pytest.raises(edit.EditError):
        edit.settings_text(root, {"lead.max_parts": "four"})
    with pytest.raises(edit.EditError):
        edit.settings_text(root, {"nope.x": 1})


def test_the_server_needs_its_token_and_its_own_host(root, tmp_path):
    app = server.App(root, chat=chat.Chat(tmp_path, popen=None))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), None)
    port = httpd.server_address[1]
    httpd.RequestHandlerClass = server.handler(app, {f"127.0.0.1:{port}"})
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        def get(path, headers):
            c = http.client.HTTPConnection("127.0.0.1", port)
            c.request("GET", path, headers=headers)
            r = c.getresponse()
            return r.status, r.read()

        status, page = get("/", {})
        assert status == 200 and app.token.encode() in page
        assert get("/api/chat", {})[0] == 403
        assert get("/api/chat", {"x-team-token": app.token, "Host": "evil.test"})[0] == 403
        status, body = get("/api/chat", {"x-team-token": app.token})
        assert status == 200 and json.loads(body)["messages"] == []
        assert get("/static/../server.py", {})[0] == 404
    finally:
        httpd.shutdown()


def test_sending_a_plan_back_dismisses_it_and_tells_the_boss_why(root, tmp_path, monkeypatch):
    c = chat.Chat(tmp_path, popen=popen_with(Proc([result("s1", "Re-proposed.", 1, 1, 0, 0)])))
    app = server.App(root, chat=c)
    monkeypatch.setattr(server.boss, "proposals", lambda: [{"id": 3, "status": "waiting", "decision_id": 7}])
    settled = []
    monkeypatch.setattr(server.App, "_settle", lambda self, d, a: settled.append((d, a)) or {"ok": True})
    with pytest.raises(edit.EditError):
        app.post("/api/plans/3/sendback", {"text": " "})
    app.post("/api/plans/3/sendback", {"text": "make it one goal"})
    assert settled == [(7, None)]
    first = c.view()["messages"][0]
    assert first["role"] == "you" and "proposal 3 back" in first["text"] and "make it one goal" in first["text"]
