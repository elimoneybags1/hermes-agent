"""Team test fixtures.

Upstream's ``tests/conftest.py`` already sandboxes HERMES_HOME. Team adds an
isolated ``TEAM_STATE`` per test so nothing touches ``$HOME/state/team``.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def team_state(tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("TEAM_STATE", str(state))
    return state
