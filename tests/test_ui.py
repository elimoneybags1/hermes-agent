"""Tests for team/ui.py: the dashboard server, over real HTTP on port 0."""

from __future__ import annotations

import os
import http.client
import json
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from team import agents, cli, ledger, ui, settings, targets

REPO = Path(__file__).parent.parent


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def temp_repo(tmp_path):
    """A tmp git repo holding a copy of the real roles/ and plugin/."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.name", "Test User")
    _git(repo, "config", "user.email", "test@example.com")
    shutil.copytree(REPO / "roles", repo / "roles")
    shutil.copytree(REPO / "plugin", repo / "plugin")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "initial")
    return repo


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "state" / "team.db"
    conn = ledger.connect(path)
    ledger.init(conn)
    job = ledger.start_job(conn, "team", "abc", "implement", "v1")
    ledger.finish_job(conn, job, "pass", "pytest")
    ledger.start_job(conn, "team", "def", "implement", "v1")
    conn.close()
    return path


@pytest.fixture
def index(tmp_path):
    path = tmp_path / "index.html"
    path.write_text("<!doctype html><title>Team</title>")
    return path


@pytest.fixture
def server(temp_repo, db, index):
    srv = ui.make_server(temp_repo, db, port=0, index=index, poll_s=0.05, ping_s=0.2)
    thread = threading.Thread(target=srv.serve_forever, args=(0.05,), daemon=True)
    thread.start()
    yield srv
    srv.shutdown()
    srv.server_close()
    thread.join(timeout=5)


@pytest.fixture
def temp_team_state(tmp_path, monkeypatch):
    """A temp TEAM_STATE directory with targets."""
    state = tmp_path / "team_state"
    state.mkdir()
    # Create targets directory
    targets_dir = state / "targets"
    targets_dir.mkdir()
    # Set TEAM_STATE for this test
    monkeypatch.setenv("TEAM_STATE", str(state))
    return state


@pytest.fixture
def temp_targets(temp_team_state):
    """Create some test targets."""
    targets_dir = temp_team_state / "targets"
    # Target 1
    t1 = targets_dir / "project1"
    t1.mkdir()
    (t1 / "target.json").write_text(
        json.dumps({"name": "project1", "path": "/tmp/proj1", "check": "pytest", "enabled": True}, indent=2) + "\n"
    )
    # Target 2
    t2 = targets_dir / "project2"
    t2.mkdir()
    (t2 / "target.json").write_text(
        json.dumps({"name": "project2", "path": "/tmp/proj2", "check": "make test", "enabled": False}, indent=2) + "\n"
    )
    return temp_team_state


@pytest.fixture
def server_with_targets(temp_repo, db, index, temp_targets):
    srv = ui.make_server(temp_repo, db, port=0, index=index, poll_s=0.05, ping_s=0.2)
    thread = threading.Thread(target=srv.serve_forever, args=(0.05,), daemon=True)
    thread.start()
    yield srv
    srv.shutdown()
    srv.server_close()
    thread.join(timeout=5)


def _url(srv, path: str) -> str:
    return f"http://127.0.0.1:{srv.server_address[1]}{path}"


def request(srv, method: str, path: str, body=None, headers=None, json_type=True):
    """(status, parsed body or bytes) for one request."""
    data = None if body is None else json.dumps(body).encode()
    hdrs = dict(headers or {})
    if json_type and method != "GET":
        hdrs.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(_url(srv, path), data=data, method=method, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            status, ctype, raw = resp.status, resp.headers["Content-Type"], resp.read()
    except urllib.error.HTTPError as e:
        status, ctype, raw = e.code, e.headers["Content-Type"], e.read()
    if ctype and ctype.startswith("application/json"):
        return status, json.loads(raw)
    return status, raw


def head(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD")


class TestHost:
    @pytest.mark.parametrize(
        "method,path",
        [
            ("GET", "/"),
            ("GET", "/api/state"),
            ("GET", "/api/events"),
            ("GET", "/api/agents/implement"),
            ("PATCH", "/api/agents/implement/role"),
            ("GET", "/nope"),
        ],
    )
    def test_foreign_host_is_403(self, server, temp_repo, method, path):
        before = head(temp_repo)
        body = {"field": "title", "value": "x"} if method != "GET" else None
        status, data = request(server, method, path, body, {"Host": "evil.example"})
        assert status == 403
        assert data == {"error": "forbidden host"}
        assert head(temp_repo) == before

    def test_wrong_port_is_403(self, server):
        status, _ = request(server, "GET", "/", headers={"Host": "127.0.0.1:1"})
        assert status == 403

    def test_localhost_is_allowed(self, server):
        port = server.server_address[1]
        status, _ = request(server, "GET", "/api/state", headers={"Host": f"localhost:{port}"})
        assert status == 200


class TestReads:
    def test_index(self, server):
        status, body = request(server, "GET", "/")
        assert status == 200
        assert body == b"<!doctype html><title>Team</title>"

    def test_index_missing_is_404(self, temp_repo, db, tmp_path):
        srv = ui.make_server(temp_repo, db, port=0, index=tmp_path / "none.html")
        thread = threading.Thread(target=srv.serve_forever, args=(0.05,), daemon=True)
        thread.start()
        try:
            status, data = request(srv, "GET", "/")
        finally:
            srv.shutdown()
            srv.server_close()
        assert status == 404
        assert "error" in data

    def test_state_has_snapshot_and_pipeline(self, server):
        status, data = request(server, "GET", "/api/state")
        assert status == 200
        assert data["pipeline"] == ["boss", "spec", "test", "implement", "review"]
        assert data["base"] == {"jobs": 1, "passed": 1, "pass_rate": 1.0}
        implement = next(r for r in data["roles"] if r["name"] == "implement")
        assert [w["target"] for w in implement["working_on"]] == ["team"]

    def test_state_without_ledger(self, temp_repo, tmp_path, index):
        missing = tmp_path / "nostate" / "team.db"
        srv = ui.make_server(temp_repo, missing, port=0, index=index)
        thread = threading.Thread(target=srv.serve_forever, args=(0.05,), daemon=True)
        thread.start()
        try:
            status, data = request(srv, "GET", "/api/state")
            dstatus, detail = request(srv, "GET", "/api/agents/implement")
        finally:
            srv.shutdown()
            srv.server_close()
        assert status == 200
        assert data["base"]["jobs"] == 0
        assert dstatus == 200
        assert detail["jobs"] == []
        assert not missing.exists()

    def test_detail(self, server):
        status, data = request(server, "GET", "/api/agents/implement")
        assert status == 200
        assert data["name"] == "implement"
        assert data["available_tools"] == agents.TOOLSETS
        assert len(data["jobs"]) == 2
        assert data["history"][0]["subject"] == "initial"

    def test_unknown_agent_is_400(self, server):
        status, data = request(server, "GET", "/api/agents/ghost")
        assert status == 400
        assert data == {"error": "unknown agent: ghost"}


class TestRoutes:
    @pytest.mark.parametrize(
        "method,path",
        [
            ("GET", "/nope"),
            ("GET", "/api"),
            ("GET", "/api/agents"),
            ("GET", "/api/agents/implement/role"),
            ("POST", "/api/state"),
            ("DELETE", "/api/agents/implement"),
            ("PUT", "/api/agents/implement/skills"),
        ],
    )
    def test_unknown_route_is_404(self, server, method, path):
        status, data = request(server, method, path, {} if method != "GET" else None)
        assert status == 404
        assert "error" in data

    def test_query_string_ignored(self, server):
        status, _ = request(server, "GET", "/api/state?x=1")
        assert status == 200


class TestWrites:
    def test_set_field(self, server, temp_repo):
        status, data = request(
            server, "PATCH", "/api/agents/implement/role", {"field": "title", "value": "Builder"}
        )
        assert status == 200
        assert data["sha"] == head(temp_repo)
        assert _git(temp_repo, "log", "-1", "--format=%s") == "implement: set title (dashboard)"
        _, detail = request(server, "GET", "/api/agents/implement")
        assert detail["role"]["title"] == "Builder"

    def test_bad_field_is_400(self, server, temp_repo):
        before = head(temp_repo)
        status, data = request(
            server, "PATCH", "/api/agents/implement/role", {"field": "nope", "value": 1}
        )
        assert status == 400
        assert data == {"error": "unknown field: nope"}
        assert head(temp_repo) == before

    def test_missing_value_is_400(self, server):
        status, data = request(server, "PATCH", "/api/agents/implement/role", {"field": "title"})
        assert status == 400
        assert "value" in data["error"]

    def test_invalid_json_is_400(self, server):
        req = urllib.request.Request(
            _url(server, "/api/agents/implement/role"),
            data=b"{not json",
            method="PATCH",
            headers={"Content-Type": "application/json"},
        )
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req, timeout=10)
        assert e.value.code == 400
        assert "invalid JSON" in json.loads(e.value.read())["error"]

    @pytest.mark.parametrize("ctype", [None, "text/plain", "application/x-www-form-urlencoded"])
    def test_write_without_json_type_is_415(self, server, temp_repo, ctype):
        before = head(temp_repo)
        headers = {"Content-Type": ctype} if ctype else {}
        status, data = request(
            server,
            "PATCH",
            "/api/agents/implement/role",
            {"field": "title", "value": "x"},
            headers,
            json_type=False,
        )
        assert status == 415
        assert "error" in data
        assert head(temp_repo) == before

    def test_delete_without_json_type_is_415(self, server, temp_repo):
        request(server, "PUT", "/api/agents/implement/skills/demo", {"text": "x"})
        status, _ = request(
            server, "DELETE", "/api/agents/implement/skills/demo", json_type=False
        )
        assert status == 415
        assert (temp_repo / "roles/implement/skills/demo/SKILL.md").exists()

    def test_json_type_with_charset(self, server):
        status, _ = request(
            server,
            "PATCH",
            "/api/agents/implement/role",
            {"field": "title", "value": "Builder"},
            {"Content-Type": "application/json; charset=utf-8"},
        )
        assert status == 200

    def test_write_instructions(self, server, temp_repo):
        status, data = request(
            server, "PUT", "/api/agents/implement/instructions", {"text": "be careful\n"}
        )
        assert status == 200
        assert data["sha"] == head(temp_repo)
        assert (temp_repo / "roles/implement/SOUL.md").read_text() == "be careful\n"

    def test_skill_write_then_delete(self, server, temp_repo):
        path = temp_repo / "roles/implement/skills/run-pytest/SKILL.md"
        status, data = request(
            server, "PUT", "/api/agents/implement/skills/run-pytest", {"text": "run it\n"}
        )
        assert status == 200
        assert data["sha"] == head(temp_repo)
        assert path.read_text() == "run it\n"

        status, data = request(server, "DELETE", "/api/agents/implement/skills/run-pytest")
        assert status == 200
        assert data["sha"] == head(temp_repo)
        assert not path.exists()

    def test_bad_skill_name_is_400(self, server):
        status, data = request(
            server, "PUT", "/api/agents/implement/skills/Bad_Name", {"text": "x"}
        )
        assert status == 400
        assert "skill name" in data["error"]

    def test_revert(self, server, temp_repo):
        _, first = request(
            server, "PATCH", "/api/agents/implement/role", {"field": "title", "value": "Builder"}
        )
        status, data = request(
            server, "POST", "/api/agents/implement/revert", {"sha": first["sha"]}
        )
        assert status == 200
        assert data["sha"] == head(temp_repo)
        original = (agents.load_roles(REPO))
        title = next(r["title"] for r in original if r["name"] == "implement")
        _, detail = request(server, "GET", "/api/agents/implement")
        assert detail["role"]["title"] == title

    def test_revert_other_agents_commit_is_400(self, server):
        _, first = request(
            server, "PATCH", "/api/agents/spec/role", {"field": "title", "value": "Speccer"}
        )
        status, data = request(
            server, "POST", "/api/agents/implement/revert", {"sha": first["sha"]}
        )
        assert status == 400
        assert "does not belong to implement" in data["error"]

    def test_other_exception_is_500_with_text(self, server, monkeypatch):
        def boom(*args, **kwargs):
            raise RuntimeError("disk on fire")

        monkeypatch.setattr(agents, "set_field", boom)
        status, data = request(
            server, "PATCH", "/api/agents/implement/role", {"field": "title", "value": "x"}
        )
        assert status == 500
        assert data == {"error": "disk on fire"}


class EventStream:
    """A raw SSE client: http.client so the test controls when it hangs up."""

    def __init__(self, srv):
        port = srv.server_address[1]
        self.conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        self.conn.request("GET", "/api/events")
        self.resp = self.conn.getresponse()

    def next_line(self, prefix: str) -> str:
        while True:
            line = self.resp.fp.readline().decode()
            if not line:
                raise EOFError("stream closed")
            if line.startswith(prefix):
                return line.rstrip("\n")

    def next_fingerprint(self) -> str:
        return json.loads(self.next_line("data: ")[len("data: "):])["fingerprint"]

    def close(self):
        self.resp.close()
        self.conn.close()


def _wait_for(cond, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.02)
    return False


class TestEvents:
    def test_headers_and_first_fingerprint(self, server, temp_repo, db):
        stream = EventStream(server)
        try:
            assert stream.resp.status == 200
            assert stream.resp.getheader("Content-Type") == "text/event-stream"
            assert stream.next_fingerprint() == agents.fingerprint(temp_repo, db)
        finally:
            stream.close()

    def test_change_sends_new_fingerprint(self, server):
        stream = EventStream(server)
        try:
            first = stream.next_fingerprint()
            status, _ = request(
                server, "PATCH", "/api/agents/implement/role", {"field": "title", "value": "B"}
            )
            assert status == 200
            assert stream.next_fingerprint() != first
        finally:
            stream.close()

    def test_unchanged_fingerprint_is_not_resent(self, server):
        stream = EventStream(server)
        try:
            stream.next_fingerprint()
            # Several poll intervals pass with nothing changing: only pings arrive.
            for _ in range(2):
                line = stream.next_line(":")
                assert line == ": ping"
        finally:
            stream.close()

    def test_each_connection_keeps_its_own_fingerprint(self, server):
        a = EventStream(server)
        try:
            fp = a.next_fingerprint()
            b = EventStream(server)
            try:
                # b is new, so it gets the current fingerprint even though a has it.
                assert b.next_fingerprint() == fp
            finally:
                b.close()
        finally:
            a.close()

    def test_stream_stops_when_client_leaves(self, server):
        stream = EventStream(server)
        stream.next_fingerprint()
        assert _wait_for(lambda: server.streams == 1)
        stream.close()
        assert _wait_for(lambda: server.streams == 0)


class TestCli:
    def test_ui_subcommand(self, monkeypatch):
        seen = {}

        def fake_main(port, open_browser):
            seen.update(port=port, open_browser=open_browser)
            return 0

        monkeypatch.setattr(ui, "main", fake_main)
        assert cli.main(["ui"]) == 0
        assert seen == {"port": 7341, "open_browser": True}
        assert cli.main(["ui", "--port", "9000", "--no-open"]) == 0
        assert seen == {"port": 9000, "open_browser": False}


class TestSettings:
    def test_get_settings(self, server_with_targets, temp_repo):
        status, data = request(server_with_targets, "GET", "/api/settings")
        assert status == 200
        assert "settings" in data
        assert "fields" in data
        assert "targets" in data
        assert "target_fields" in data
        # Check settings has all FIELDS with defaults
        for key in settings.FIELDS:
            assert key in data["settings"]
        # Check fields structure
        for f in data["fields"]:
            assert "key" in f
            assert "type" in f
            assert "default" in f
            assert "help" in f
        # Check targets list
        assert len(data["targets"]) == 2
        target_names = {t["name"] for t in data["targets"]}
        assert target_names == {"project1", "project2"}
        # Check target_fields structure
        for f in data["target_fields"]:
            assert "key" in f
            assert "type" in f
            assert "help" in f
        assert {f["key"] for f in data["target_fields"]} == {"check", "enabled"}

    def test_get_settings_no_targets(self, temp_repo, db, index, tmp_path, monkeypatch):
        """Test settings when no targets are attached."""
        empty_state = tmp_path / "empty_state"
        empty_state.mkdir()
        (empty_state / "targets").mkdir()
        # Set TEAM_STATE to use our empty state dir
        monkeypatch.setenv("TEAM_STATE", str(empty_state))
        srv = ui.make_server(temp_repo, db, port=0, index=index, poll_s=0.05, ping_s=0.2)
        thread = threading.Thread(target=srv.serve_forever, args=(0.05,), daemon=True)
        thread.start()
        try:
            status, data = request(srv, "GET", "/api/settings")
        finally:
            srv.shutdown()
            srv.server_close()
            thread.join(timeout=5)
        assert status == 200
        assert data["targets"] == []

    def test_patch_setting_valid(self, server_with_targets, temp_repo):
        # Change loop.interval_s
        status, data = request(server_with_targets, "PATCH", "/api/settings", {"key": "loop.interval_s", "value": 120})
        assert status == 200
        assert "sha" in data
        # Verify the change
        status2, data2 = request(server_with_targets, "GET", "/api/settings")
        assert status2 == 200
        assert data2["settings"]["loop.interval_s"] == 120

    def test_patch_setting_invalid_key(self, server_with_targets, temp_repo):
        before = _git(temp_repo, "rev-parse", "HEAD")
        status, data = request(server_with_targets, "PATCH", "/api/settings", {"key": "unknown.setting", "value": 1})
        assert status == 400
        assert "unknown setting" in data["error"]
        assert _git(temp_repo, "rev-parse", "HEAD") == before

    def test_patch_setting_bad_value_type(self, server_with_targets, temp_repo):
        before = _git(temp_repo, "rev-parse", "HEAD")
        status, data = request(server_with_targets, "PATCH", "/api/settings", {"key": "loop.interval_s", "value": "not a number"})
        assert status == 400
        assert "whole number" in data["error"] or "integer" in data["error"]
        assert _git(temp_repo, "rev-parse", "HEAD") == before

    def test_patch_setting_bool_value(self, server_with_targets, temp_repo):
        status, data = request(server_with_targets, "PATCH", "/api/settings", {"key": "escalation.claude", "value": False})
        assert status == 200
        status2, data2 = request(server_with_targets, "GET", "/api/settings")
        assert data2["settings"]["escalation.claude"] is False

    def test_patch_setting_string_value(self, server_with_targets, temp_repo):
        status, data = request(server_with_targets, "PATCH", "/api/settings", {"key": "grading.default_check", "value": "make test"})
        assert status == 200
        status2, data2 = request(server_with_targets, "GET", "/api/settings")
        assert data2["settings"]["grading.default_check"] == "make test"

    def test_patch_target_valid(self, server_with_targets, temp_repo):
        # Change project1's check
        status, data = request(server_with_targets, "PATCH", "/api/targets/project1", {"key": "check", "value": "pytest -x"})
        assert status == 200
        assert data["check"] == "pytest -x"
        assert data["name"] == "project1"
        # Verify persisted
        status2, data2 = request(server_with_targets, "GET", "/api/settings")
        project1 = next(t for t in data2["targets"] if t["name"] == "project1")
        assert project1["check"] == "pytest -x"

    def test_patch_target_enabled_toggle(self, server_with_targets, temp_repo):
        status, data = request(server_with_targets, "PATCH", "/api/targets/project2", {"key": "enabled", "value": True})
        assert status == 200
        assert data["enabled"] is True

    def test_patch_target_unknown(self, server_with_targets):
        status, data = request(server_with_targets, "PATCH", "/api/targets/unknown", {"key": "check", "value": "x"})
        assert status == 400
        assert "unknown target" in data["error"]

    def test_patch_target_bad_key(self, server_with_targets):
        status, data = request(server_with_targets, "PATCH", "/api/targets/project1", {"key": "unknown", "value": "x"})
        assert status == 400
        assert "unknown target setting" in data["error"]

    def test_patch_target_bad_value_type(self, server_with_targets):
        status, data = request(server_with_targets, "PATCH", "/api/targets/project1", {"key": "enabled", "value": "not bool"})
        assert status == 400
        assert "true or false" in data["error"]

    def test_patch_target_empty_check_clears(self, server_with_targets):
        # Empty check should clear to None (use default)
        status, data = request(server_with_targets, "PATCH", "/api/targets/project1", {"key": "check", "value": ""})
        assert status == 200
        assert data["check"] is None


class TestDecisions:
    def test_get_decisions_empty(self, server):
        status, data = request(server, "GET", "/api/decisions")
        assert status == 200
        assert data == {"decisions": [], "reviews": {}}

    def test_get_decisions_with_data(self, server, db):
        from team import decisions
        conn = ledger.connect(db)
        try:
            d1 = decisions.create(conn, "team", "Use Rust?", "Performance", ["yes", "no"], "yes", goal_id=1)
            d2 = decisions.create(conn, "brain", "Use Go?", "Concurrency", ["yes", "no"], "no")
        finally:
            conn.close()

        status, data = request(server, "GET", "/api/decisions")
        assert status == 200
        assert "decisions" in data
        assert "reviews" in data
        assert data["reviews"] == {}
        assert len(data["decisions"]) == 2
        # Decisions come newest first
        assert data["decisions"][0]["id"] == d2
        assert data["decisions"][1]["id"] == d1
        # Check options are parsed
        for d in data["decisions"]:
            assert isinstance(d["options"], list)
            assert d["options"] == ["yes", "no"]

    def test_post_decision_answer(self, server, db, tmp_path, monkeypatch):
        from team import decisions
        # Create a temp TEAM_STATE for decisions.md
        state = tmp_path / "team_state"
        state.mkdir()
        (state / "targets").mkdir()
        monkeypatch.setenv("TEAM_STATE", str(state))

        conn = ledger.connect(db)
        try:
            d_id = decisions.create(conn, "team", "Use Rust?", "Performance", ["yes", "no"], "yes")
        finally:
            conn.close()

        status, data = request(server, "POST", f"/api/decisions/{d_id}", {"answer": "Yes, we will use Rust"})
        assert status == 200
        assert data == {"ok": True}

        # Verify decision was answered
        conn = ledger.connect(db)
        try:
            row = conn.execute("SELECT * FROM decisions WHERE id = ?", (d_id,)).fetchone()
            assert row["status"] == "answered"
            assert row["answer"] == "Yes, we will use Rust"
            assert row["answered_at"] is not None
        finally:
            conn.close()

        # Verify decisions.md was created
        log_path = state / "targets" / "team" / "decisions.md"
        assert log_path.exists()
        content = log_path.read_text()
        assert "Use Rust?" in content
        assert "Yes, we will use Rust" in content

    def test_post_decision_dismiss(self, server, db):
        from team import decisions
        conn = ledger.connect(db)
        try:
            d_id = decisions.create(conn, "team", "Use Rust?", "Performance", ["yes", "no"], "yes")
        finally:
            conn.close()

        status, data = request(server, "POST", f"/api/decisions/{d_id}", {"dismiss": True})
        assert status == 200
        assert data == {"ok": True}

        # Verify decision was dismissed
        conn = ledger.connect(db)
        try:
            row = conn.execute("SELECT * FROM decisions WHERE id = ?", (d_id,)).fetchone()
            assert row["status"] == "dismissed"
            assert row["answered_at"] is not None
        finally:
            conn.close()

    def test_post_decision_invalid_id(self, server):
        status, data = request(server, "POST", "/api/decisions/not-a-number", {"answer": "x"})
        assert status == 400
        assert data == {"error": "invalid decision id"}

    def test_post_decision_unknown_id_404(self, server):
        """Unknown decision id returns 404."""
        status, data = request(server, "POST", "/api/decisions/9999", {"answer": "x"})
        assert status == 404
        assert "error" in data

    def test_post_decision_not_open_409(self, server, db):
        """Decision that is not open returns 409."""
        from team import decisions
        conn = ledger.connect(db)
        try:
            d_id = decisions.create(conn, "team", "Use Rust?", "Performance", ["yes", "no"], "yes")
            # Answer it first
            decisions.answer(conn, d_id, "Yes, we will use Rust")
        finally:
            conn.close()

        status, data = request(server, "POST", f"/api/decisions/{d_id}", {"answer": "No"})
        assert status == 409
        assert "not open" in data["error"]

    def test_post_decision_empty_answer_400(self, server, db):
        """Empty or whitespace answer returns 400."""
        from team import decisions
        conn = ledger.connect(db)
        try:
            d_id = decisions.create(conn, "team", "Use Rust?", "Performance", ["yes", "no"], "yes")
        finally:
            conn.close()

        # Empty string
        status, data = request(server, "POST", f"/api/decisions/{d_id}", {"answer": ""})
        assert status == 400
        assert data == {"error": "answer must not be empty"}

        # Whitespace only
        status, data = request(server, "POST", f"/api/decisions/{d_id}", {"answer": "   "})
        assert status == 400
        assert data == {"error": "answer must not be empty"}

    def test_post_decision_missing_body(self, server, db):
        from team import decisions
        conn = ledger.connect(db)
        try:
            d_id = decisions.create(conn, "team", "Use Rust?", "Performance", ["yes", "no"], "yes")
        finally:
            conn.close()

        status, data = request(server, "POST", f"/api/decisions/{d_id}", {})
        assert status == 400
        assert "body must contain" in data["error"]

    def test_post_decision_bad_answer_type(self, server, db):
        from team import decisions
        conn = ledger.connect(db)
        try:
            d_id = decisions.create(conn, "team", "Use Rust?", "Performance", ["yes", "no"], "yes")
        finally:
            conn.close()

        status, data = request(server, "POST", f"/api/decisions/{d_id}", {"answer": 123})
        assert status == 400
        assert data == {"error": "answer must be a string"}

    def test_host_check_on_decisions(self, server):
        # GET
        status, data = request(server, "GET", "/api/decisions", headers={"Host": "evil.example"})
        assert status == 403
        # POST
        status, data = request(server, "POST", "/api/decisions/1", {"answer": "x"}, headers={"Host": "evil.example"})
        assert status == 403

    def test_content_type_check_on_decisions(self, server, db):
        from team import decisions
        conn = ledger.connect(db)
        try:
            d_id = decisions.create(conn, "team", "Use Rust?", "Performance", ["yes", "no"], "yes")
        finally:
            conn.close()

        for ctype in [None, "text/plain", "application/x-www-form-urlencoded"]:
            headers = {"Content-Type": ctype} if ctype else {}
            status, data = request(
                server, "POST", f"/api/decisions/{d_id}", {"answer": "x"}, headers, json_type=False
            )
            assert status == 415
            assert "error" in data
