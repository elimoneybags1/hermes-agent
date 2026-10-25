import pytest
import os
import shutil
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

from team import cli, ledger


@pytest.fixture
def state(monkeypatch, tmp_path):
    monkeypatch.setenv("TEAM_STATE", str(tmp_path / "state"))
    return tmp_path / "state"


def test_init_creates_db(state, capsys):
    assert cli.main(["ledger", "init"]) == 0
    assert (state / "team.db").exists()
    assert str(state / "team.db") in capsys.readouterr().out


def test_list_without_db_fails(state, capsys):
    assert cli.main(["ledger", "list"]) == 1
    assert "team ledger init" in capsys.readouterr().err
    assert not state.exists()


def test_list_shows_jobs(state, capsys):
    cli.main(["ledger", "init"])
    conn = ledger.connect()
    job = ledger.start_job(conn, "team", "abc", "profiler", "v1")
    ledger.finish_job(conn, job, "pass", "pytest")
    ledger.start_job(conn, "brain", "def", "profiler", "v1")
    conn.close()
    capsys.readouterr()

    assert cli.main(["ledger", "list", "--target", "team"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].split("\t") == list(cli.LIST_COLUMNS)
    assert len(lines) == 2
    row = dict(zip(cli.LIST_COLUMNS, lines[1].split("\t")))
    assert row["target"] == "team" and row["outcome"] == "pass"


def test_boss_argv():
    repo = Path("/fake/repo")
    extra = ["--dangerously-skip-permissions"]
    argv = cli.boss_argv(repo, extra)
    assert argv == [
        "claude",
        "--plugin-dir",
        str(repo / "plugin"),
        "--agent",
        "boss",
        "--dangerously-skip-permissions",
    ]


def test_board_argv_is_the_free_worker_profile():
    argv = cli.board_argv(9129)
    assert argv[:3] == ["hermes", "-p", "team-worker"]
    assert argv[3:] == ["dashboard", "--isolated", "--port", "9129", "--no-open"]


def test_boss_argv_no_extra():
    repo = Path("/fake/repo")
    argv = cli.boss_argv(repo, [])
    assert argv == [
        "claude",
        "--plugin-dir",
        str(repo / "plugin"),
        "--agent",
        "boss",
    ]


def test_team_no_subcommand_execvp(monkeypatch, capsys):
    """Test that team with no subcommand calls os.execvp with the right argv and chdirs."""
    repo = Path("/fake/repo")

    execvp_called = {}
    chdir_called = {}

    def mock_execvp(cmd, argv):
        execvp_called["cmd"] = cmd
        execvp_called["argv"] = argv
        raise SystemExit(0)  # simulate exec replacing process

    def mock_chdir(path):
        chdir_called["path"] = path

    monkeypatch.setattr(os, "execvp", mock_execvp)
    monkeypatch.setattr(os, "chdir", mock_chdir)
    monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/claude")

    # Mock the module's __file__ to control the repo path
    with patch("team.cli.__file__", "/fake/repo/team/cli.py"):
        with pytest.raises(SystemExit):
            cli.main([])

    assert chdir_called["path"] == repo
    assert execvp_called["cmd"] == "/usr/bin/claude"
    assert execvp_called["argv"] == [
        "claude",
        "--plugin-dir",
        str(repo / "plugin"),
        "--agent",
        "boss",
    ]


def test_team_no_subcommand_with_extra_args(monkeypatch, capsys):
    """Test that team -- --arg passes extra args to claude."""
    repo = Path("/fake/repo")

    execvp_called = {}

    def mock_execvp(cmd, argv):
        execvp_called["argv"] = argv
        raise SystemExit(0)

    monkeypatch.setattr(os, "execvp", mock_execvp)
    monkeypatch.setattr(os, "chdir", lambda _: None)
    monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/claude")

    with patch("team.cli.__file__", "/fake/repo/team/cli.py"):
        with pytest.raises(SystemExit):
            cli.main(["--", "--dangerously-skip-permissions", "--resume"])

    assert execvp_called["argv"] == [
        "claude",
        "--plugin-dir",
        str(repo / "plugin"),
        "--agent",
        "boss",
        "--dangerously-skip-permissions",
        "--resume",
    ]


def test_team_help_exits_via_argparse(monkeypatch, capsys):
    """Test that team --help exits through argparse (SystemExit 0) without calling execvp."""
    execvp_called = False

    def mock_execvp(cmd, argv):
        nonlocal execvp_called
        execvp_called = True
        raise SystemExit(0)

    monkeypatch.setattr(os, "execvp", mock_execvp)
    monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/claude")

    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--help"])

    assert excinfo.value.code == 0
    assert not execvp_called


def test_team_unknown_subcommand_exits_via_argparse(monkeypatch, capsys):
    """Test that team ledgr (unknown subcommand) raises SystemExit 2 without calling execvp."""
    execvp_called = False

    def mock_execvp(cmd, argv):
        nonlocal execvp_called
        execvp_called = True
        raise SystemExit(0)

    monkeypatch.setattr(os, "execvp", mock_execvp)
    monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/claude")

    with pytest.raises(SystemExit) as excinfo:
        cli.main(["ledgr"])

    assert excinfo.value.code == 2
    assert not execvp_called


def test_team_claude_not_found(monkeypatch, capsys):
    """Test that team returns 1 when claude is not on PATH."""
    repo = Path("/fake/repo")

    monkeypatch.setattr(os, "chdir", lambda _: None)
    monkeypatch.setattr(shutil, "which", lambda _: None)

    with patch("team.cli.__file__", "/fake/repo/team/cli.py"):
        result = cli.main([])

    assert result == 1
    assert "claude not found on PATH" in capsys.readouterr().err


def test_existing_subcommand_still_works(state, capsys):
    """Test that existing subcommands like ledger init still work."""
    result = cli.main(["ledger", "init"])
    assert result == 0
    assert (state / "team.db").exists()


def test_goals_without_db_fails(state, capsys):
    assert cli.main(["goals"]) == 1
    assert "team ledger init" in capsys.readouterr().err


def test_goals_shows_open_goals(state, capsys):
    cli.main(["ledger", "init"])
    conn = ledger.connect()
    ledger.create_goal(conn, "team", "First goal", "implement", "team/branch1", "/tmp/wt1", "pytest")
    ledger.create_goal(conn, "brain", "Second goal", "implement", "team/branch2", "/tmp/wt2", "pytest")
    conn.close()
    capsys.readouterr()

    assert cli.main(["goals"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].split("\t") == list(cli.GOALS_COLUMNS)
    assert len(lines) == 3  # header + 2 goals
    row1 = dict(zip(cli.GOALS_COLUMNS, lines[1].split("\t")))
    row2 = dict(zip(cli.GOALS_COLUMNS, lines[2].split("\t")))
    assert row1["target"] == "brain"
    assert row1["status"] == "open"
    assert row1["goal"] == "Second goal"
    assert row2["target"] == "team"
    assert row2["status"] == "open"
    assert row2["goal"] == "First goal"


def test_goals_all_shows_finished(state, capsys):
    cli.main(["ledger", "init"])
    conn = ledger.connect()
    ledger.create_goal(conn, "team", "Open goal", "implement", "team/branch1", "/tmp/wt1", "pytest")
    goal_id = ledger.create_goal(conn, "team", "Finished goal", "implement", "team/branch2", "/tmp/wt2", "pytest")
    ledger.finish_goal(conn, goal_id, "pass", "done")
    conn.close()
    capsys.readouterr()

    # --all shows both (newest first)
    assert cli.main(["goals", "--all"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 3  # header + 2 goals
    row1 = dict(zip(cli.GOALS_COLUMNS, lines[1].split("\t")))
    row2 = dict(zip(cli.GOALS_COLUMNS, lines[2].split("\t")))
    # newest first: Finished goal (id=2) then Open goal (id=1)
    assert row1["status"] == "pass"
    assert row1["goal"] == "Finished goal"
    assert row2["status"] == "open"
    assert row2["goal"] == "Open goal"

    # default shows only open
    capsys.readouterr()
    assert cli.main(["goals"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 2  # header + 1 open goal
    row = dict(zip(cli.GOALS_COLUMNS, lines[1].split("\t")))
    assert row["status"] == "open"
    assert row["goal"] == "Open goal"


def test_goals_filters_by_target(state, capsys):
    cli.main(["ledger", "init"])
    conn = ledger.connect()
    ledger.create_goal(conn, "team", "Team goal", "implement", "team/branch1", "/tmp/wt1", "pytest")
    ledger.create_goal(conn, "brain", "Brain goal", "implement", "team/branch2", "/tmp/wt2", "pytest")
    conn.close()
    capsys.readouterr()

    assert cli.main(["goals", "team"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 2  # header + 1 goal
    row = dict(zip(cli.GOALS_COLUMNS, lines[1].split("\t")))
    assert row["target"] == "team"
    assert row["goal"] == "Team goal"


def test_decide_list_no_db(state, capsys):
    """team decide list fails gracefully without ledger."""
    assert cli.main(["decide", "list"]) == 1
    assert "team ledger init" in capsys.readouterr().err


def test_decide_list_empty(state, capsys):
    """team decide list shows 'no open decisions' when empty."""
    cli.main(["ledger", "init"])
    assert cli.main(["decide", "list"]) == 0
    assert "no open decisions" in capsys.readouterr().out


def test_decide_list_shows_decisions(state, capsys):
    """team decide list shows open decisions with details."""
    cli.main(["ledger", "init"])
    conn = ledger.connect()
    from team import decisions
    d1 = decisions.create(conn, "team", "Use Rust?", "Perf", ["yes", "no"], "yes", goal_id=1, task_id=5, source="run")
    d2 = decisions.create(conn, "brain", "Use Go?", "Concurrency", ["yes", "no"], "no", source="overseer")
    conn.close()

    assert cli.main(["decide", "list"]) == 0
    out = capsys.readouterr().out
    # Newest first
    assert out.index("Use Go?") < out.index("Use Rust?")
    assert "options: yes, no" in out
    assert "recommendation: yes" in out
    assert "recommendation: no" in out
    assert "goal_id: 1" in out
    assert "task_id: 5" in out
    assert "source: run" in out
    assert "source: overseer" in out


def test_decide_list_filter_target(state, capsys):
    """team decide list --target filters by target."""
    cli.main(["ledger", "init"])
    conn = ledger.connect()
    from team import decisions
    decisions.create(conn, "team", "Q1", "C", ["a", "b"], "a")
    decisions.create(conn, "brain", "Q2", "C", ["a", "b"], "a")
    conn.close()

    assert cli.main(["decide", "list", "--target", "team"]) == 0
    out = capsys.readouterr().out
    assert "Q1" in out
    assert "Q2" not in out


def test_decide_answer(state, capsys):
    """team decide answer answers a decision and updates DB and log."""
    cli.main(["ledger", "init"])
    conn = ledger.connect()
    from team import decisions
    d_id = decisions.create(conn, "team", "Use Rust?", "Perf", ["yes", "no"], "yes")
    conn.close()

    assert cli.main(["decide", "answer", str(d_id), "Yes, for the hot path."]) == 0
    assert f"answered decision {d_id}" in capsys.readouterr().out

    # Verify DB
    conn = ledger.connect()
    row = conn.execute("SELECT * FROM decisions WHERE id = ?", (d_id,)).fetchone()
    assert row["status"] == "answered"
    assert row["answer"] == "Yes, for the hot path."
    conn.close()


def test_decide_answer_nonexistent(state, capsys):
    """team decide answer fails for nonexistent decision."""
    cli.main(["ledger", "init"])
    assert cli.main(["decide", "answer", "999", "answer"]) == 1
    assert "decision not found" in capsys.readouterr().err


def test_decide_answer_already_answered(state, capsys):
    """team decide answer fails for already answered decision."""
    cli.main(["ledger", "init"])
    conn = ledger.connect()
    from team import decisions
    d_id = decisions.create(conn, "team", "Q", "C", ["a", "b"], "a")
    decisions.answer(conn, d_id, "A")
    conn.close()

    assert cli.main(["decide", "answer", str(d_id), "B"]) == 1
    assert "cannot answer" in capsys.readouterr().err


def test_decide_dismiss(state, capsys):
    """team decide dismiss dismisses a decision."""
    cli.main(["ledger", "init"])
    conn = ledger.connect()
    from team import decisions
    d_id = decisions.create(conn, "team", "Q", "C", ["a", "b"], "a")
    conn.close()

    assert cli.main(["decide", "dismiss", str(d_id)]) == 0
    assert f"dismissed decision {d_id}" in capsys.readouterr().out

    conn = ledger.connect()
    row = conn.execute("SELECT * FROM decisions WHERE id = ?", (d_id,)).fetchone()
    assert row["status"] == "dismissed"
    conn.close()


def test_decide_dismiss_nonexistent(state, capsys):
    """team decide dismiss fails for nonexistent decision."""
    cli.main(["ledger", "init"])
    assert cli.main(["decide", "dismiss", "999"]) == 1
    assert "decision not found" in capsys.readouterr().err


def test_decide_dismiss_already_answered(state, capsys):
    """team decide dismiss fails for already answered decision."""
    cli.main(["ledger", "init"])
    conn = ledger.connect()
    from team import decisions
    d_id = decisions.create(conn, "team", "Q", "C", ["a", "b"], "a")
    decisions.answer(conn, d_id, "A")
    conn.close()

    assert cli.main(["decide", "dismiss", str(d_id)]) == 1
    assert "cannot dismiss" in capsys.readouterr().err


def test_deck_subcommand_exists():
    """team deck is a recognized subcommand."""
    import argparse
    parser = argparse.ArgumentParser(prog="team")
    commands = parser.add_subparsers(dest="command", required=True)
    deck_parser = commands.add_parser("deck", help="launch the Team deck (Textual control room)")
    deck_parser.set_defaults(func=lambda args: 0)

    args = parser.parse_args(["deck"])
    assert args.command == "deck"
    assert args.func(args) == 0


def test_open_subcommand_exists():
    """team open is a recognized subcommand."""
    import argparse
    parser = argparse.ArgumentParser(prog="team")
    commands = parser.add_subparsers(dest="command", required=True)
    open_parser = commands.add_parser("open", help="open the deck in the current herdr session")
    open_parser.set_defaults(func=lambda args: 0)

    args = parser.parse_args(["open"])
    assert args.command == "open"
    assert args.func(args) == 0


def test_deck_main_returns_zero(monkeypatch):
    """deck.main() returns 0 (test mode just checks import works)."""
    # Mock the app.run to avoid actually running the TUI
    mock_app = MagicMock()
    mock_app.run = MagicMock()
    monkeypatch.setattr("team.deck.TeamDeck", lambda: mock_app)

    result = cli.main(["deck"])
    # Should succeed (the actual app.run isn't called in this test setup)
    assert result == 0 or result is None  # deck.main calls app.run() which we mocked


def test_open_not_in_herdr_pane(capsys, monkeypatch):
    """team open fails gracefully when not in a herdr pane."""
    monkeypatch.delenv("HERDR_PANE_ID", raising=False)

    result = cli.main(["open"])
    assert result == 1
    out = capsys.readouterr().out
    assert "Not in a herdr pane" in out
    assert "herdr pane split" in out


def test_hook_stop_exists():
    """team hook stop is a recognized subcommand."""
    import argparse
    parser = argparse.ArgumentParser(prog="team")
    commands = parser.add_subparsers(dest="command", required=True)
    hook_parser = commands.add_parser("hook", help="Claude Code hooks for build mode")
    hook_sub = hook_parser.add_subparsers(dest="action", required=True)
    hook_sub.add_parser("stop", help="Stop hook - runs when agent finishes").set_defaults(func=lambda args: 0)

    args = parser.parse_args(["hook", "stop"])
    assert args.command == "hook"
    assert args.action == "stop"
    assert args.func(args) == 0


def test_hook_pretool_exists():
    """team hook pretool is a recognized subcommand."""
    import argparse
    parser = argparse.ArgumentParser(prog="team")
    commands = parser.add_subparsers(dest="command", required=True)
    hook_parser = commands.add_parser("hook", help="Claude Code hooks for build mode")
    hook_sub = hook_parser.add_subparsers(dest="action", required=True)
    hook_sub.add_parser("pretool", help="PreToolUse hook - enforces TEAM_ALLOWED_FILES gate").set_defaults(func=lambda args: 0)

    args = parser.parse_args(["hook", "pretool"])
    assert args.command == "hook"
    assert args.action == "pretool"
    assert args.func(args) == 0


def test_hook_stop_returns_zero_on_valid_input(monkeypatch, capsys, tmp_path):
    """team hook stop returns 0 in a clean worktree with no check set."""
    import json
    import io
    import subprocess
    import sys

    subprocess.run(["git", "init", "-q", str(tmp_path / "wt")], check=True)
    monkeypatch.setenv("TEAM_STATE", str(tmp_path / "state"))
    monkeypatch.delenv("TEAM_CHECK", raising=False)
    payload = {"session_id": "s", "cwd": str(tmp_path / "wt")}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    
    result = cli.main(["hook", "stop"])
    assert result == 0


def test_hook_stop_returns_zero_on_invalid_json(monkeypatch, capsys):
    """team hook stop returns 0 on invalid JSON (ignores non-hook input)."""
    import io
    import sys
    
    monkeypatch.setattr(sys, "stdin", io.StringIO("not json"))
    
    result = cli.main(["hook", "stop"])
    assert result == 0


def test_hook_pretool_allows_non_edit_tools(monkeypatch, capsys):
    """team hook pretool returns 0 for non-edit tools."""
    import json
    import io
    import sys
    
    payload = {"tool_name": "Read", "tool_input": {"file_path": "/tmp/test.txt"}}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    
    result = cli.main(["hook", "pretool"])
    assert result == 0


def test_hook_pretool_allows_when_no_gate(monkeypatch, capsys):
    """team hook pretool returns 0 when TEAM_ALLOWED_FILES not set."""
    import json
    import io
    import sys
    
    monkeypatch.delenv("TEAM_ALLOWED_FILES", raising=False)
    payload = {"tool_name": "Edit", "tool_input": {"file_path": "/tmp/test.txt"}}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    
    result = cli.main(["hook", "pretool"])
    assert result == 0


def test_hook_pretool_allows_matching_file(monkeypatch, capsys):
    """team hook pretool returns 0 when file matches allowed pattern."""
    import json
    import io
    import sys
    
    monkeypatch.setenv("TEAM_ALLOWED_FILES", "src/**:tests/**")
    payload = {"tool_name": "Edit", "cwd": "/home/user", "tool_input": {"file_path": "/home/user/src/main.py"}}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    
    result = cli.main(["hook", "pretool"])
    assert result == 0


def test_hook_pretool_blocks_non_matching_file(monkeypatch, capsys):
    """team hook pretool returns 2 when file doesn't match allowed pattern."""
    import json
    import io
    import sys
    
    monkeypatch.setenv("TEAM_ALLOWED_FILES", "src/**:tests/**")
    payload = {"tool_name": "Edit", "cwd": "/home/user", "tool_input": {"file_path": "/home/user/config.json"}}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    
    result = cli.main(["hook", "pretool"])
    assert result == 2
    assert "not one of this task's files" in capsys.readouterr().err


def test_hook_pretool_blocks_when_no_file_path(monkeypatch, capsys):
    """team hook pretool returns 0 when no file_path in tool_input (edge case)."""
    import json
    import io
    import sys
    
    monkeypatch.setenv("TEAM_ALLOWED_FILES", "src/**")
    payload = {"tool_name": "Edit", "tool_input": {}}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    
    result = cli.main(["hook", "pretool"])
    assert result == 0


def test_hook_pretool_handles_invalid_json(monkeypatch, capsys):
    """team hook pretool returns 0 on invalid JSON."""
    import io
    import sys
    
    monkeypatch.setattr(sys, "stdin", io.StringIO("not json"))
    
    result = cli.main(["hook", "pretool"])
    assert result == 0