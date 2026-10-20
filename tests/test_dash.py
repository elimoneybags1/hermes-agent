"""Tests for team/dash.py."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

import pytest

from team import dash, ledger


@pytest.fixture
def temp_ledger(tmp_path):
    """Create a temporary ledger with test data."""
    db_path = tmp_path / "team.db"
    conn = ledger.connect(db_path)
    ledger.init(conn)
    yield conn
    conn.close()


@pytest.fixture
def temp_roles_dir(tmp_path):
    """Create a temporary roles directory structure."""
    roles_dir = tmp_path / "roles"
    roles_dir.mkdir()

    # boss role
    boss_dir = roles_dir / "boss"
    boss_dir.mkdir()
    (boss_dir / "role.toml").write_text(
        'name = "boss"\n'
        'title = "Foreman"\n'
        'does = "Talks to Eli"\n'
        'graded_by = "Eli"\n'
        'status = "planned"\n'
        '\n'
        '[runs_on]\n'
        'harness = "claude-code"\n'
        'model = "claude"\n'
    )

    # implement role
    impl_dir = roles_dir / "implement"
    impl_dir.mkdir()
    (impl_dir / "role.toml").write_text(
        'name = "implement"\n'
        'title = "Implement"\n'
        'does = "Writes the code"\n'
        'graded_by = "pytest"\n'
        'status = "active"\n'
        '\n'
        '[runs_on]\n'
        'harness = "hermes"\n'
        'model = "nvidia/nemotron-3-ultra-550b-a55b:free"\n'
        '\n'
        '[escalation]\n'
        'harness = "crew"\n'
        'model = "claude"\n'
        'after_failures = 2\n'
        '\n'
        '[budget]\n'
        'max_turns = 40\n'
        'run_budget_s = 900\n'
    )

    # review role
    review_dir = roles_dir / "review"
    review_dir.mkdir()
    (review_dir / "role.toml").write_text(
        'name = "review"\n'
        'title = "Review"\n'
        'does = "Checks a finished diff"\n'
        'graded_by = "The boss agrees"\n'
        'status = "planned"\n'
        '\n'
        '[runs_on]\n'
        'harness = "hermes"\n'
        'model = "nvidia/nemotron-3-ultra-550b-a55b:free"\n'
        '\n'
        '[budget]\n'
        'max_turns = 15\n'
        'run_budget_s = 300\n'
    )

    return tmp_path


def _seed_jobs(conn):
    """Add test jobs to the ledger."""
    # implement: 1 pass, 1 fail, 1 open
    j1 = ledger.start_job(conn, "team", "c1", "implement", "v1")
    ledger.finish_job(conn, j1, "pass", "pytest", "12 passed")
    j2 = ledger.start_job(conn, "team", "c2", "implement", "v1")
    ledger.finish_job(conn, j2, "fail", "pytest", "3 failed")
    j3 = ledger.start_job(conn, "team", "c3", "implement", "v1")  # open

    # review: 2 pass, 0 open
    j4 = ledger.start_job(conn, "team", "c4", "review", "v1")
    ledger.finish_job(conn, j4, "pass", "boss", "approved")
    j5 = ledger.start_job(conn, "team", "c5", "review", "v1")
    ledger.finish_job(conn, j5, "pass", "boss", "approved")

    # boss: 0 finished, 1 open
    j6 = ledger.start_job(conn, "team", "c6", "boss", "v1")  # open

    # unknown role: 1 pass (should count in base, not in any role)
    j7 = ledger.start_job(conn, "team", "c7", "unknown_role", "v1")
    ledger.finish_job(conn, j7, "pass", "manual", "ok")

    return [j1, j2, j3, j4, j5, j6, j7]


def test_snapshot_with_ledger(temp_ledger, temp_roles_dir, monkeypatch):
    """snapshot() returns correct structure with ledger data."""
    import team.dash as dash_mod
    monkeypatch.setattr(dash_mod, "__file__", str(temp_roles_dir / "team" / "dash.py"))
    (temp_roles_dir / "team").mkdir()

    _seed_jobs(temp_ledger)
    roles_list = dash.load_roles(root=temp_roles_dir)
    data = dash.snapshot(temp_ledger, roles_list)

    # Check top-level structure
    assert "generated_at" in data
    assert "base" in data
    assert "roles" in data
    assert "recent" in data

    # Base stats: 5 finished jobs (3 implement + 2 review + 1 unknown), 4 passed
    assert data["base"]["jobs"] == 5
    assert data["base"]["passed"] == 4
    assert data["base"]["pass_rate"] == pytest.approx(4 / 5)

    # Recent: last 7 jobs, newest first
    assert len(data["recent"]) == 7
    assert data["recent"][0]["role"] == "unknown_role"
    assert data["recent"][-1]["role"] == "implement"


def test_snapshot_role_stats(temp_ledger, temp_roles_dir, monkeypatch):
    """Each role's stats count only its finished jobs."""
    import team.dash as dash_mod
    monkeypatch.setattr(dash_mod, "__file__", str(temp_roles_dir / "team" / "dash.py"))
    (temp_roles_dir / "team").mkdir()

    _seed_jobs(temp_ledger)
    roles_list = dash.load_roles(root=temp_roles_dir)
    data = dash.snapshot(temp_ledger, roles_list)

    roles_by_name = {r["name"]: r for r in data["roles"]}

    # implement: 2 finished (1 pass, 1 fail), 1 open
    impl = roles_by_name["implement"]
    assert impl["stats"]["jobs"] == 2
    assert impl["stats"]["passed"] == 1
    assert impl["stats"]["pass_rate"] == pytest.approx(0.5)
    assert len(impl["working_on"]) == 1
    assert impl["working_on"][0]["job_id"] == 3

    # review: 2 finished (2 pass), 0 open
    rev = roles_by_name["review"]
    assert rev["stats"]["jobs"] == 2
    assert rev["stats"]["passed"] == 2
    assert rev["stats"]["pass_rate"] == pytest.approx(1.0)
    assert rev["working_on"] == []

    # boss: 0 finished, 1 open
    boss = roles_by_name["boss"]
    assert boss["stats"]["jobs"] == 0
    assert boss["stats"]["passed"] == 0
    assert boss["stats"]["pass_rate"] is None
    assert len(boss["working_on"]) == 1
    assert boss["working_on"][0]["job_id"] == 6


def test_snapshot_unknown_role_in_base_only(temp_ledger, temp_roles_dir, monkeypatch):
    """Jobs with unknown role count in base but not in any role."""
    import team.dash as dash_mod
    monkeypatch.setattr(dash_mod, "__file__", str(temp_roles_dir / "team" / "dash.py"))
    (temp_roles_dir / "team").mkdir()

    _seed_jobs(temp_ledger)
    roles_list = dash.load_roles(root=temp_roles_dir)
    data = dash.snapshot(temp_ledger, roles_list)

    # Unknown role not in roles list
    role_names = [r["name"] for r in data["roles"]]
    assert "unknown_role" not in role_names

    # But its job counts in base
    assert data["base"]["jobs"] == 5  # includes unknown_role's pass


def test_snapshot_pass_rate_null_when_no_jobs(temp_ledger, temp_roles_dir, monkeypatch):
    """pass_rate is null when a role has no finished jobs."""
    import team.dash as dash_mod
    monkeypatch.setattr(dash_mod, "__file__", str(temp_roles_dir / "team" / "dash.py"))
    (temp_roles_dir / "team").mkdir()

    # No jobs at all
    roles_list = dash.load_roles(root=temp_roles_dir)
    data = dash.snapshot(temp_ledger, roles_list)

    for role in data["roles"]:
        assert role["stats"]["pass_rate"] is None


def test_snapshot_no_ledger_uses_empty(temp_roles_dir, monkeypatch):
    """When ledger file doesn't exist, snapshot uses empty in-memory ledger."""
    import team.dash as dash_mod
    monkeypatch.setattr(dash_mod, "__file__", str(temp_roles_dir / "team" / "dash.py"))
    (temp_roles_dir / "team").mkdir()

    roles_list = dash.load_roles(root=temp_roles_dir)
    data = dash.snapshot(None, roles_list)

    assert data["base"]["jobs"] == 0
    assert data["base"]["pass_rate"] is None
    assert data["recent"] == []
    for role in data["roles"]:
        assert role["stats"]["jobs"] == 0
        assert role["stats"]["pass_rate"] is None
        assert role["working_on"] == []


def test_snapshot_json_serializable(temp_ledger, temp_roles_dir, monkeypatch):
    """snapshot() output is JSON-serializable."""
    import team.dash as dash_mod
    monkeypatch.setattr(dash_mod, "__file__", str(temp_roles_dir / "team" / "dash.py"))
    (temp_roles_dir / "team").mkdir()

    _seed_jobs(temp_ledger)
    roles_list = dash.load_roles(root=temp_roles_dir)
    data = dash.snapshot(temp_ledger, roles_list)

    # Should not raise
    json.dumps(data)


def test_snapshot_generated_at_is_utc_iso(temp_ledger, temp_roles_dir, monkeypatch):
    """generated_at is UTC ISO format with seconds precision."""
    import team.dash as dash_mod
    monkeypatch.setattr(dash_mod, "__file__", str(temp_roles_dir / "team" / "dash.py"))
    (temp_roles_dir / "team").mkdir()

    roles_list = dash.load_roles(root=temp_roles_dir)
    data = dash.snapshot(temp_ledger, roles_list)

    ts = data["generated_at"]
    # Parse it back to verify format
    parsed = datetime.fromisoformat(ts)
    assert parsed.utcoffset() == timezone.utc.utcoffset(parsed)
    assert parsed.microsecond == 0


def test_dash_main_with_json_flag(temp_ledger, temp_roles_dir, monkeypatch, capsys):
    """dash.main() with --json prints JSON to stdout."""
    import team.roles as roles_mod
    monkeypatch.setattr(roles_mod, "__file__", str(temp_roles_dir / "team" / "roles.py"))
    (temp_roles_dir / "team").mkdir()

    # Patch _safe_connect to return our temp ledger
    def mock_safe_connect():
        return temp_ledger
    monkeypatch.setattr(dash, "_safe_connect", mock_safe_connect)

    _seed_jobs(temp_ledger)
    ret = dash.main(argv=["--json"])
    assert ret == 0

    out = capsys.readouterr().out
    data = json.loads(out)
    assert data["base"]["jobs"] == 5


def test_dash_main_without_json_prints_table(temp_ledger, temp_roles_dir, monkeypatch, capsys):
    """dash.main() without --json prints readable table."""
    import team.roles as roles_mod
    monkeypatch.setattr(roles_mod, "__file__", str(temp_roles_dir / "team" / "roles.py"))
    (temp_roles_dir / "team").mkdir()

    def mock_safe_connect():
        return temp_ledger
    monkeypatch.setattr(dash, "_safe_connect", mock_safe_connect)

    _seed_jobs(temp_ledger)
    ret = dash.main(argv=[])
    assert ret == 0

    out = capsys.readouterr().out
    lines = out.strip().split("\n")
    # Should have one line per role + base line (3 test roles + base)
    assert len(lines) == 4  # 3 roles + base
    # Check format: name\tstatus\tworking_on_count\tpass_rate
    for line in lines[:-1]:
        parts = line.split("\t")
        assert len(parts) == 4
    # Base line
    base_parts = lines[-1].split("\t")
    assert base_parts[0] == "base"
    assert len(base_parts) == 4


def test_snapshot_working_on_includes_job_details(temp_ledger, temp_roles_dir, monkeypatch):
    """working_on includes job_id, target, attempt, started."""
    import team.dash as dash_mod
    monkeypatch.setattr(dash_mod, "__file__", str(temp_roles_dir / "team" / "dash.py"))
    (temp_roles_dir / "team").mkdir()

    _seed_jobs(temp_ledger)
    roles_list = dash.load_roles(root=temp_roles_dir)
    data = dash.snapshot(temp_ledger, roles_list)

    impl = next(r for r in data["roles"] if r["name"] == "implement")
    wo = impl["working_on"]
    assert len(wo) == 1
    assert wo[0]["job_id"] == 3
    assert wo[0]["target"] == "team"
    assert wo[0]["attempt"] == 1
    assert wo[0]["started"] is not None


def test_snapshot_role_includes_all_role_fields(temp_ledger, temp_roles_dir, monkeypatch):
    """Each role in snapshot includes all fields from role.toml."""
    import team.dash as dash_mod
    monkeypatch.setattr(dash_mod, "__file__", str(temp_roles_dir / "team" / "dash.py"))
    (temp_roles_dir / "team").mkdir()

    roles_list = dash.load_roles(root=temp_roles_dir)
    data = dash.snapshot(temp_ledger, roles_list)

    impl = next(r for r in data["roles"] if r["name"] == "implement")
    assert impl["title"] == "Implement"
    assert impl["does"] == "Writes the code"
    assert impl["graded_by"] == "pytest"
    assert impl["status"] == "active"
    assert impl["runs_on"] == {"harness": "hermes", "model": "nvidia/nemotron-3-ultra-550b-a55b:free"}
    assert impl["escalation"] == {"harness": "crew", "model": "claude", "after_failures": 2}
    assert impl["budget"] == {"max_turns": 40, "run_budget_s": 900}


def test_snapshot_optional_fields_none_when_missing(temp_ledger, temp_roles_dir, monkeypatch):
    """escalation and budget are null (not missing) when not in role.toml."""
    import team.dash as dash_mod
    monkeypatch.setattr(dash_mod, "__file__", str(temp_roles_dir / "team" / "dash.py"))
    (temp_roles_dir / "team").mkdir()

    roles_list = dash.load_roles(root=temp_roles_dir)
    data = dash.snapshot(temp_ledger, roles_list)

    boss = next(r for r in data["roles"] if r["name"] == "boss")
    assert boss["escalation"] is None
    assert boss["budget"] is None


def test_snapshot_recent_limit_20(temp_ledger, temp_roles_dir, monkeypatch):
    """recent contains at most 20 jobs."""
    import team.dash as dash_mod
    monkeypatch.setattr(dash_mod, "__file__", str(temp_roles_dir / "team" / "dash.py"))
    (temp_roles_dir / "team").mkdir()

    # Add 25 jobs
    for i in range(25):
        j = ledger.start_job(temp_ledger, "team", f"c{i}", "implement", "v1")
        ledger.finish_job(temp_ledger, j, "pass", "pytest")

    roles_list = dash.load_roles(root=temp_roles_dir)
    data = dash.snapshot(temp_ledger, roles_list)

    assert len(data["recent"]) == 20


def test_snapshot_no_hardcoded_paths():
    """No hardcoded user directories in the implementation."""
    import inspect
    source = inspect.getsource(dash.snapshot)
    assert "home" not in source.lower()
    assert "/users/" not in source.lower()
    assert "~" not in source