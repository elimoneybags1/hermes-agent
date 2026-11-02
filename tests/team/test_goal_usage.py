import sqlite3
import pytest
from pathlib import Path

from team import ledger, settings


@pytest.fixture
def conn(tmp_path):
    c = ledger.connect(tmp_path / "team.db")
    ledger.init(c)
    yield c
    c.close()


def _insert_goal(conn, goal_id: int) -> None:
    with conn:
        conn.execute(
            "INSERT INTO goals (id, target, goal, role, branch, worktree, check_cmd, created) "
            "VALUES (?, 't', 'g', 'worker', 'b', 'w', 'pytest -q', '2024-01-01T00:00:00+00:00')",
            (goal_id,),
        )


def test_goal_usage_counts_worker_calls_and_tokens(conn, tmp_path):
    """Test that goal_usage only counts worker calls and tokens, excluding Claude and lead calls."""
    # A call's goal_id references goals(id), so the goals must exist first.
    _insert_goal(conn, 7)
    _insert_goal(conn, 8)
    # Insert test rows
    # 1. Worker call with api_calls=3, tokens_in=100
    ledger.record_call(conn, None, "litellm", "m", "work", 100, 50, 10, True, task_ref='t_1', goal_id=7, api_calls=3)
    # 2. Worker call with api_calls=None (default 1), tokens_in=50
    ledger.record_call(conn, None, "litellm", "m", "work", 50, 25, 10, True, task_ref='t_2', goal_id=7, api_calls=None)
    
    # Test goal 7: should have 4 calls (3 + 1) and 150 tokens_in (100 + 50)
    result = ledger.goal_usage(conn, 7)
    assert result == {"calls": 4, "tokens_in": 150}
    
    # Insert a row for goal 8 to test isolation
    ledger.record_call(conn, None, "litellm", "m", "work", 200, 100, 10, True, task_ref='t_3', goal_id=8, api_calls=2)
    
    # Goal 7 should not be affected by goal 8's data
    result = ledger.goal_usage(conn, 7)
    assert result == {"calls": 4, "tokens_in": 150}
    
    # Test that Claude calls are excluded
    ledger.record_call(conn, None, "litellm", "m", "work", 300, 150, 10, True, task_ref='claude:x', goal_id=7, api_calls=1)
    
    # Claude call should not be counted
    result = ledger.goal_usage(conn, 7)
    assert result == {"calls": 4, "tokens_in": 150}


def test_goal_usage_settings_defaults(tmp_path):
    """Test that the new settings have their expected defaults."""
    # Load settings without a toml file (should use defaults)
    settings_dict = settings.load(tmp_path)
    
    # Check that the new settings exist with correct defaults
    assert settings_dict.get("worker.max_calls_per_goal") == 15
    assert settings_dict.get("worker.max_input_tokens_per_goal") == 400000


def test_goal_usage_empty_goal(conn):
    """Test goal_usage with no worker calls for a goal."""
    # Create a fresh ledger connection
    conn = ledger.connect(":memory:")
    ledger.init(conn)
    
    # Test a goal with no worker calls (should return 0, 0)
    result = ledger.goal_usage(conn, 999)
    assert result == {"calls": 0, "tokens_in": 0}


def test_goal_usage_with_none_values(conn):
    """Test that None api_calls and tokens_in are handled correctly."""
    conn = ledger.connect(":memory:")
    ledger.init(conn)

    _insert_goal(conn, 1)
    # Record a call with None api_calls and tokens_in
    ledger.record_call(conn, None, "litellm", "m", "work", None, None, 10, True, task_ref='t_test', goal_id=1)
    
    result = ledger.goal_usage(conn, 1)
    # None api_calls should default to 1, None tokens_in should default to 0
    assert result == {"calls": 1, "tokens_in": 0}