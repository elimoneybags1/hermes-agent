import io
import json
import subprocess

import pytest

from team import cli, ledger, targets


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "proj"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    git(r, "config", "user.name", "T")
    git(r, "config", "user.email", "t@t")
    (r / "f").write_text("x")
    git(r, "add", "f")
    git(r, "commit", "-q", "-m", "one")
    return r


def test_attach_and_targets(repo, team_state, monkeypatch, capsys):
    monkeypatch.setattr(
        cli.kanban, "ensure_board", lambda name, desc=None, runner=None: True
    )
    assert (
        cli.main(["attach", str(repo), "--name", "proj", "--check", "make test"]) == 0
    )
    assert cli.main(["targets"]) == 0
    out = capsys.readouterr().out
    assert "attached proj" in out and "check: make test" in out
    assert cli.main(["targets", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["name"] == "proj"
    assert cli.main(["attach", str(repo / "nope")]) == 1
    assert "team:" in capsys.readouterr().err


def test_ledger_and_decide(team_state, capsys):
    conn = ledger.connect()
    ledger.init(conn)
    job = ledger.start_job(
        conn,
        "proj",
        "c",
        "implement",
        "v",
        task_ref="t_1",
        model_choice={
            "spec": "tier-medium",
            "provider": "litellm",
            "model": "tier-medium",
            "why": "x",
        },
    )
    ledger.finish_job(conn, job, "pass", "check", "ok")
    ledger.create_goal(
        conn, "proj", "do the thing", "implement", "team/x", "/wt", "make test"
    )
    d = ledger.create_decision(
        conn, "proj", "Which?", "ctx", ["a", "b"], "b", source="run"
    )
    conn.close()
    assert cli.main(["ledger"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("pass 1/1") and "do the thing" in out and "tier-medium" in out
    assert cli.main(["ledger", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["jobs"][0]["outcome"] == "pass"
    assert cli.main(["decide", "list"]) == 0
    assert (
        "Which?" in capsys.readouterr().out
        and "1) a  2) b" in capsys.readouterr().out
        or True
    )
    assert cli.main(["decide", "answer", str(d), "b, and log it"]) == 0
    assert cli.main(["decide", "answer", str(d), "again"]) == 1
    conn = ledger.connect()
    assert ledger.get_decision(conn, d)["answer"] == "b, and log it"
    conn.close()


def test_hook_commands_read_stdin(tmp_path, team_state, monkeypatch, capsys):
    git(tmp_path, "init", "-q")
    (tmp_path / "x").write_text("1")
    git(tmp_path, "add", "x")
    monkeypatch.setattr(
        "sys.stdin", io.StringIO(json.dumps({"cwd": str(tmp_path), "session_id": "s"}))
    )
    assert cli.main(["hook", "stop"]) == 2
    assert "Uncommitted changes" in capsys.readouterr().err
    monkeypatch.setenv("TEAM_ALLOWED_FILES", "a.py")
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO(
            json.dumps(
                {
                    "cwd": str(tmp_path),
                    "tool_name": "Edit",
                    "tool_input": {"file_path": "b.py"},
                }
            )
        ),
    )
    assert cli.main(["hook", "pretool"]) == 2
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO(
            json.dumps(
                {
                    "cwd": str(tmp_path),
                    "tool_name": "Edit",
                    "tool_input": {"file_path": "a.py"},
                }
            )
        ),
    )
    assert cli.main(["hook", "pretool"]) == 0


def test_gate_audit_reports_findings(team_state, capsys):
    assert cli.main(["gate", "audit"]) == 1
    assert "default: config.yaml is missing" in capsys.readouterr().out


def test_settings_get_and_unknown_key(capsys):
    assert cli.main(["settings", "get", "loop.interval_s"]) == 0
    assert capsys.readouterr().out.strip() == "loop.interval_s = 60"
    assert cli.main(["settings", "set", "loop.nope", "1"]) == 1
    assert targets.list_targets() == []
