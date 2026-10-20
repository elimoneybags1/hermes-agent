import pytest

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
