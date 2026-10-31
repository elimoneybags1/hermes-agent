"""Tests for all_goals_landed utility."""

from team.goals_landed import all_goals_landed

P = {"id": 1, "target": "team"}

def row(outcome):
    return {"outcome": outcome}


def test_all_landed():
    assert all_goals_landed(P, [row("pass"), row("pass")]) is True


def test_one_failed():
    assert all_goals_landed(P, [row("pass"), row("fail")]) is False


def test_one_running():
    assert all_goals_landed(P, [row("pass"), row(None)]) is False


def test_zero_goals():
    assert all_goals_landed(P, []) is False