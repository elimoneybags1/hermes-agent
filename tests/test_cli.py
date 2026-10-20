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