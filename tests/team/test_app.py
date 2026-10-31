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
        if len(calls) > len(runs):
            return Proc([], err="Error: no answer scripted for this run\n")
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
    c = chat.Chat(tmp_path, popen=popen, aide=False)
    assert c.send("Add a test", wait=True) == "boss"
    c.send("team", wait=True)
    first, second = popen.calls
    assert first[1:3] == ["-p", "boss"]
    assert "--resume" not in first and first[first.index("-t") + 1] == "team,memory,session_search"
    assert second[-2:] == ["--resume", "s1"]
    msgs = c.view()["messages"]
    assert [m["role"] for m in msgs] == ["you", "boss", "you", "boss"]
    assert msgs[0]["to"] == "boss"
    assert msgs[1]["text"] == "Which target?"
    assert msgs[1]["tokens"] == {"input": 4738, "output": 4, "cache_read": 0, "cache_write": 4394}
    assert msgs[3]["tokens"] == {"input": 4757, "output": 8, "cache_read": 4394, "cache_write": 359}
    assert msgs[3]["tools"] == [{"name": "team_status", "input": {"target": "team"}, "done": True, "seconds": 1.5}]
    assert json.loads((team_state / "boss" / "chat.json").read_text())["lanes"]["boss"]["session"] == "s1"


def test_chat_shows_a_failed_run_and_queues_while_busy(team_state, tmp_path):
    c = chat.Chat(tmp_path, popen=popen_with(Proc([], err="✓ venv\nError: gateway refused\n")), aide=False)
    c.send("hi", wait=True)
    assert c.view()["messages"][-1]["error"] == "Error: gateway refused"
    with c.lock:  # pretend the boss is mid-answer
        c.current["boss"] = {"role": "boss", "text": "", "tools": []}
        c.running["boss"] = True
    c.send("again")
    c.note("Eli answered: calls")
    assert c.view()["queued"] == {"aide": 0, "boss": 2} and c.view()["current"]["role"] == "boss"
    with pytest.raises(chat.Busy):
        c.new()
    with c.lock:
        merged = c._boss_message(c.queue["boss"])
    assert merged == "again\n\nEli answered: calls"  # one boss turn for both
    with pytest.raises(ValueError):
        chat.Chat(tmp_path, popen=None).send("  ")
    with pytest.raises(ValueError):
        chat.Chat(tmp_path, popen=None).send("x", to="lead")


def test_the_aide_answers_questions_and_hands_the_rest_to_the_boss(team_state, tmp_path):
    popen = popen_with(
        Proc([result("a1", "HANDOFF: asks to build something", 100, 5, 0, 0)]),
        Proc([result("b1", "Project 1 is in your inbox.", 4000, 20, 0, 0)]),
        Proc([result("a1", "3 goals landed since Monday; 0 in rescue.", 120, 9, 0, 0)]),
        Proc([result("b1", "Added goal 4.", 4100, 10, 0, 0)]),
    )
    c = chat.Chat(tmp_path, popen=popen, aide=True)
    assert c.send("Build a token cap", wait=True) == "aide"
    aide_run, boss_run = popen.calls[:2]
    assert aide_run[1:3] == ["-p", "aide"] and aide_run[aide_run.index("-t") + 1] == "team-read"
    assert boss_run[1:3] == ["-p", "boss"] and boss_run[boss_run.index("-q") + 1] == "Build a token cap"
    msgs = c.view()["messages"]
    assert [m["role"] for m in msgs] == ["you", "aide", "boss"]
    assert msgs[0]["to"] == "boss" and msgs[1]["handoff"] is True and msgs[2]["text"] == "Project 1 is in your inbox."

    c.send("How many landed this week?", wait=True)
    msgs = c.view()["messages"]
    assert msgs[3]["to"] == "aide" and msgs[4]["role"] == "aide" and msgs[4]["handoff"] is False
    assert popen.calls[2][-2:] == ["--resume", "a1"]  # the aide keeps its own session

    c.send("Add a goal for it", to="boss", wait=True)
    text = popen.calls[3][popen.calls[3].index("-q") + 1]
    assert text.startswith("(Since your last turn, the aide answered Eli") and "3 goals landed since Monday" in text
    assert text.endswith("Add a goal for it") and "How many landed" in text
    assert popen.calls[3][-2:] == ["--resume", "b1"]
    assert c.view()["messages"][4]["relayed"] is True
    assert c.view()["session"] == "b1"


def test_a_failed_aide_run_still_reaches_the_boss(team_state, tmp_path):
    popen = popen_with(Proc([], err="Error: gateway refused\n"), Proc([result("b1", "Here.", 1, 1, 0, 0)]))
    c = chat.Chat(tmp_path, popen=popen, aide=True)
    c.send("what now?", wait=True)
    msgs = c.view()["messages"]
    assert [m["role"] for m in msgs] == ["you", "aide", "boss"] and msgs[1]["error"] and msgs[0]["to"] == "boss"


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


def test_sending_a_goal_back_tells_the_boss_why(root, tmp_path, monkeypatch):
    c = chat.Chat(tmp_path, popen=popen_with(Proc([result("s1", "Re-proposed.", 1, 1, 0, 0)])), aide=True)
    app = server.App(root, chat=c)
    monkeypatch.setattr(server.boss, "proposals", lambda: [{"id": 3, "status": "open", "target": "team"}])
    sent = []

    def send_back(conn, target, pid, n, text):
        if not text.strip():
            raise server.boss.BossError("say what should change")
        sent.append((target, pid, n, text))
        return {"goal": "Cap tokens", "note": text, "status": "sent_back"}

    monkeypatch.setattr(server.boss, "send_back", send_back)
    with pytest.raises(edit.EditError):
        app.post("/api/plans/3/goals/1/sendback", {"text": " "})
    app.post("/api/plans/3/goals/1/sendback", {"text": "make it one goal"})
    assert sent == [("team", 3, 1, "make it one goal")]
    c.drain()
    first = c.view()["messages"][0]
    assert first["role"] == "you" and first["kind"] == "note" and first["to"] == "boss"
    assert "goal 1 of project 3 back" in first["text"] and "make it one goal" in first["text"]
    assert "team_propose(project=3)" in first["text"]
    with pytest.raises(edit.EditError, match="is open"):  # approve-all on a project with nothing waiting
        monkeypatch.setattr(server.boss, "settle_goals", lambda conn, p, a: (_ for _ in ()).throw(server.boss.BossError("project 3 is open: no goal waiting")))
        app.post("/api/plans/3/approve", {})


def test_answering_the_boss_question_carries_the_answer_back(root, tmp_path, team_state):
    from team import ledger

    c = chat.Chat(tmp_path, popen=popen_with(Proc([result("s1", "Going on.", 1, 1, 0, 0)]), Proc([result("s1", "Ok.", 1, 1, 0, 0)])), aide=True)
    app = server.App(root, chat=c)
    conn = ledger.connect()
    ledger.init(conn)
    q = ledger.create_decision(conn, "team", "Which limit first?", "ctx", ["calls", "tokens"], "calls", source="boss-ask:0")
    other = ledger.create_decision(conn, "team", "Retry goal 9?", "ctx", ["yes", "no"], "yes")
    box = app.get("/api/inbox", {})
    assert [d["id"] for d in box["decisions"]] == [q, other] and box["decisions"][0]["plan_id"] is None
    with pytest.raises(edit.EditError, match="pick an option"):
        app.post(f"/api/decisions/{q}/answer", {"answer": " "})
    app.post(f"/api/decisions/{q}/answer", {"answer": "in my own words: tokens, but cap calls too"})
    app.post(f"/api/decisions/{other}/answer", {"answer": "no"})  # not the boss's: nothing is said to it
    c.drain()
    msgs = c.view()["messages"]
    assert len(msgs) == 2 and msgs[0]["kind"] == "note"
    assert msgs[0]["text"] == "You asked: Which limit first? Eli answered: in my own words: tokens, but cap calls too"
    assert ledger.get_decision(conn, q)["answer"].startswith("in my own words")
    q2 = ledger.create_decision(conn, "team", "Ship?", "ctx", ["yes", "no"], "yes", source="boss-ask:0")
    app.post(f"/api/decisions/{q2}/dismiss", {})
    c.drain()
    assert "dismissed your question" in c.view()["messages"][2]["text"]
