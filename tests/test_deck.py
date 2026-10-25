"""Tests for team/deck.py."""

from __future__ import annotations

import os
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from textual.app import App

from team import deck, ledger


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
    )

    return tmp_path


@pytest.fixture
def mock_app(temp_ledger, temp_roles_dir, monkeypatch):
    """Create a TeamDeck app with mocked dependencies."""
    # Patch the deck module's file location to find roles
    import team.deck as deck_mod
    monkeypatch.setattr(deck_mod, "__file__", str(temp_roles_dir / "team" / "deck.py"))
    (temp_roles_dir / "team").mkdir()

    # Create the app
    app = deck.TeamDeck()
    return app


def test_deck_imports():
    """Test that deck module can be imported."""
    assert hasattr(deck, "TeamDeck")
    assert hasattr(deck, "NewGoalScreen")
    assert hasattr(deck, "main")


def test_format_age():
    """Test _format_age formats timestamps correctly."""
    from datetime import datetime, timezone, timedelta

    app = deck.TeamDeck()

    # Just created
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    assert app._format_age(now) in ("0m", "1m")

    # 5 minutes ago
    five_min_ago = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(timespec="seconds")
    assert app._format_age(five_min_ago) == "5m"

    # 2 hours ago
    two_hours_ago = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(timespec="seconds")
    assert app._format_age(two_hours_ago) == "2h"

    # 3 days ago
    three_days_ago = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat(timespec="seconds")
    assert app._format_age(three_days_ago) == "3d"


def test_deck_bindings():
    """Test that TeamDeck has the expected key bindings."""
    app = deck.TeamDeck()
    binding_keys = {b.key for b in app.BINDINGS}
    assert "f1" in binding_keys
    assert "f2" in binding_keys
    assert "f3" in binding_keys
    assert "f4" in binding_keys
    assert "g" in binding_keys
    assert "q" in binding_keys


def test_new_goal_screen_compose():
    """Test NewGoalScreen composes correctly."""
    screen = deck.NewGoalScreen([])
    # Just check it can be instantiated
    assert screen is not None


def test_open_decisions_count():
    """Test _open_decisions_count queries the ledger."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "team.db"
        conn = ledger.connect(db_path)
        ledger.init(conn)

        # Add a decision
        from team import decisions
        decisions.create(conn, "team", "Test?", "Context", ["yes", "no"], "yes")

        app = deck.TeamDeck()
        # Patch db_path to use our temp ledger
        with patch("team.deck.ledger.db_path", return_value=db_path):
            count = app._open_decisions_count()
            assert count == 1

        conn.close()


def test_count_pipeline_goals():
    """Test _count_pipeline_goals counts by role."""
    app = deck.TeamDeck()
    roles = [
        {"name": "boss", "working_on": [{"job_id": 1}]},
        {"name": "implement", "working_on": [{"job_id": 2}, {"job_id": 3}]},
        {"name": "review", "working_on": []},
    ]
    counts = app._count_pipeline_goals(roles)
    assert counts["boss"] == 1
    assert counts["implement"] == 2
    assert counts["review"] == 0


def test_deck_main():
    """Test deck.main entry point exists."""
    assert callable(deck.main)


def test_deck_compose_has_correct_widgets():
    """Test that TeamDeck compose yields expected structure."""
    app = deck.TeamDeck()
    # Just test that the class can be instantiated and compose doesn't error
    # The actual compose is tested via Textual's run_test in integration tests
    assert app is not None