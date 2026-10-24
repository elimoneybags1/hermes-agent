"""Tests for team.models.choose."""

import random
import sqlite3
import tempfile
from pathlib import Path

import pytest

from team import ledger, models, settings
from team.ledger import init


@pytest.fixture
def tmp_state(tmp_path):
    """A temporary ledger database."""
    db_path = tmp_path / "team.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    init(conn)
    return conn, tmp_path


@pytest.fixture
def repo_root(tmp_path):
    """A temporary repo with role.toml files."""
    root = tmp_path / "repo"
    root.mkdir()
    # Create roles directory structure
    for role_name in ("implement", "spec", "test", "review"):
        role_dir = root / "roles" / role_name
        role_dir.mkdir(parents=True)
        (role_dir / "role.toml").write_text(f"""
name = "{role_name}"
title = "{role_name.capitalize()}"
does = "test role"
graded_by = "test"
status = "active"
hands_to = "review"
tools = ["terminal"]

[runs_on]
harness = "hermes"
model = "nvidia/nemotron-3-ultra-550b-a55b:free"

[models]
candidates = [
    "nvidia/nemotron-3-ultra-550b-a55b:free",
    "poolside/laguna-s-2.1:free",
    "cohere/north-mini-code:free",
]
pinned = ""

[escalation]
harness = "claude"
model = "claude"
after_failures = 2

[budget]
max_turns = 40
run_budget_s = 900
""")
    # Create orchestration.toml with model settings
    (root / "orchestration.toml").write_text("""
[models]
claude_cost = 0.3
minute_weight = 0.005
explore = true
""")
    return root


def test_choose_pinned_wins(repo_root, tmp_state):
    """Pinned model is returned immediately."""
    conn, _ = tmp_state
    # Modify role to have a pinned model
    role_file = repo_root / "roles" / "implement" / "role.toml"
    content = role_file.read_text()
    content = content.replace('pinned = ""', 'pinned = "nvidia/nemotron-3-ultra-550b-a55b:free"')
    role_file.write_text(content)

    rng = random.Random(42)
    result = models.choose(conn, "implement", "demo", 1, rng, root=repo_root)

    assert result["model"] == "nvidia/nemotron-3-ultra-550b-a55b:free"
    assert result["why"] == "pinned"
    assert "scores" in result


def test_choose_filters_non_free(repo_root, tmp_state):
    """Non-free models (except claude) are excluded."""
    conn, _ = tmp_state
    # Add a non-free model to candidates
    role_file = repo_root / "roles" / "implement" / "role.toml"
    content = role_file.read_text()
    content = content.replace(
        'candidates = [',
        'candidates = [\n    "openai/gpt-4o",'
    )
    role_file.write_text(content)

    rng = random.Random(42)
    result = models.choose(conn, "implement", "demo", 1, rng, root=repo_root)

    # Should not pick the non-free model
    assert result["model"] != "openai/gpt-4o"
    # Should pick one of the free ones
    assert result["model"] in (
        "nvidia/nemotron-3-ultra-550b-a55b:free",
        "poolside/laguna-s-2.1:free",
        "cohere/north-mini-code:free",
    )


def test_choose_claude_allowed(repo_root, tmp_state):
    """Claude is always allowed as a candidate."""
    conn, _ = tmp_state
    # Modify role to only have claude
    role_file = repo_root / "roles" / "implement" / "role.toml"
    content = role_file.read_text()
    content = content.replace(
        'candidates = [\n    "nvidia/nemotron-3-ultra-550b-a55b:free",\n    "poolside/laguna-s-2.1:free",\n    "cohere/north-mini-code:free",\n]',
        'candidates = ["claude"]'
    )
    role_file.write_text(content)

    rng = random.Random(42)
    result = models.choose(conn, "implement", "demo", 1, rng, root=repo_root)

    assert result["model"] == "claude"


def test_choose_retry_avoids_failed_model(repo_root, tmp_state):
    """On retry, the model that just failed is avoided (if others exist)."""
    conn, _ = tmp_state

    # Record a job and call for attempt 1 with a specific model
    job_id = ledger.start_job(conn, "demo", "abc123", "implement", "v1", attempt=1)
    ledger.record_call(
        conn, job_id, "openrouter", "nvidia/nemotron-3-ultra-550b-a55b:free",
        "implement", 100, 200, 5000, True
    )
    ledger.finish_job(conn, job_id, "fail", "test", "failed")

    rng = random.Random(42)
    result = models.choose(conn, "implement", "demo", 2, rng, root=repo_root)

    # Should avoid the model that just failed
    assert result["model"] != "nvidia/nemotron-3-ultra-550b-a55b:free"
    assert "avoided" in result["why"]


def test_choose_retry_allows_failed_if_only_one_left(repo_root, tmp_state):
    """If only one candidate left, retry uses it even if it failed."""
    conn, _ = tmp_state
    # Modify role to have only one free candidate
    role_file = repo_root / "roles" / "implement" / "role.toml"
    content = role_file.read_text()
    content = content.replace(
        'candidates = [\n    "nvidia/nemotron-3-ultra-550b-a55b:free",\n    "poolside/laguna-s-2.1:free",\n    "cohere/north-mini-code:free",\n]',
        'candidates = ["nvidia/nemotron-3-ultra-550b-a55b:free"]'
    )
    role_file.write_text(content)

    # Record a job and call for attempt 1
    job_id = ledger.start_job(conn, "demo", "abc123", "implement", "v1", attempt=1)
    ledger.record_call(
        conn, job_id, "openrouter", "nvidia/nemotron-3-ultra-550b-a55b:free",
        "implement", 100, 200, 5000, True
    )
    ledger.finish_job(conn, job_id, "fail", "test", "failed")

    rng = random.Random(42)
    result = models.choose(conn, "implement", "demo", 2, rng, root=repo_root)

    # Only one candidate, so it must be used
    assert result["model"] == "nvidia/nemotron-3-ultra-550b-a55b:free"


def test_choose_uses_ledger_stats(repo_root, tmp_state):
    """Model scores are influenced by ledger history."""
    conn, _ = tmp_state

    # Add successful jobs for model A
    for _ in range(5):
        job_id = ledger.start_job(conn, "demo", "abc123", "implement", "v1", attempt=1)
        ledger.record_call(
            conn, job_id, "openrouter", "nvidia/nemotron-3-ultra-550b-a55b:free",
            "implement", 100, 200, 5000, True
        )
        ledger.finish_job(conn, job_id, "pass", "test", "ok")

    # Add failed jobs for model B
    for _ in range(5):
        job_id = ledger.start_job(conn, "demo", "abc123", "implement", "v1", attempt=1)
        ledger.record_call(
            conn, job_id, "openrouter", "poolside/laguna-s-2.1:free",
            "implement", 100, 200, 5000, True
        )
        ledger.finish_job(conn, job_id, "fail", "test", "failed")

    rng = random.Random(42)
    result = models.choose(conn, "implement", "demo", 1, rng, root=repo_root)

    # Model A should be preferred (more passes)
    assert result["model"] == "nvidia/nemotron-3-ultra-550b-a55b:free"
    scores = result["scores"]
    assert scores["nvidia/nemotron-3-ultra-550b-a55b:free"]["p_pass"] > scores["poolside/laguna-s-2.1:free"]["p_pass"]


def test_choose_target_weighting(repo_root, tmp_state):
    """Target's jobs weighted 3x, other targets 1x."""
    conn, _ = tmp_state

    # Modify role to have only two candidates
    role_file = repo_root / "roles" / "implement" / "role.toml"
    content = role_file.read_text()
    content = content.replace(
        'candidates = [\n    "nvidia/nemotron-3-ultra-550b-a55b:free",\n    "poolside/laguna-s-2.1:free",\n    "cohere/north-mini-code:free",\n]',
        'candidates = [\n    "nvidia/nemotron-3-ultra-550b-a55b:free",\n    "poolside/laguna-s-2.1:free",\n]'
    )
    role_file.write_text(content)

    # Add 1 pass for model A on target "demo" (weight 3)
    job_id = ledger.start_job(conn, "demo", "abc123", "implement", "v1", attempt=1)
    ledger.record_call(
        conn, job_id, "openrouter", "nvidia/nemotron-3-ultra-550b-a55b:free",
        "implement", 100, 200, 5000, True
    )
    ledger.finish_job(conn, job_id, "pass", "test", "ok")

    # Add 2 passes for model B on target "other" (weight 1 each = 2 total)
    for _ in range(2):
        job_id = ledger.start_job(conn, "other", "abc123", "implement", "v1", attempt=1)
        ledger.record_call(
            conn, job_id, "openrouter", "poolside/laguna-s-2.1:free",
            "implement", 100, 200, 5000, True
        )
        ledger.finish_job(conn, job_id, "pass", "test", "ok")

    rng = random.Random(42)
    result = models.choose(conn, "implement", "demo", 1, rng, root=repo_root)

    # Model A has effective 3 passes, Model B has 2 passes -> A should win
    assert result["model"] == "nvidia/nemotron-3-ultra-550b-a55b:free"


def test_choose_explore_off_uses_mean(repo_root, tmp_state):
    """explore=false uses mean of Beta, not sampling."""
    conn, _ = tmp_state
    # Disable explore in settings
    settings_file = repo_root / "orchestration.toml"
    content = settings_file.read_text()
    content = content.replace("explore = true", "explore = false")
    settings_file.write_text(content)

    rng = random.Random(42)
    result = models.choose(conn, "implement", "demo", 1, rng, root=repo_root)

    assert "mean" in result["why"] or "explore=off" in result["why"]


def test_choose_seeded_rng_deterministic(repo_root, tmp_state):
    """Same seed gives same result."""
    conn, _ = tmp_state

    rng1 = random.Random(12345)
    result1 = models.choose(conn, "implement", "demo", 1, rng1, root=repo_root)

    rng2 = random.Random(12345)
    result2 = models.choose(conn, "implement", "demo", 1, rng2, root=repo_root)

    assert result1["model"] == result2["model"]
    assert result1["why"] == result2["why"]


def test_choose_returns_scores_for_all_candidates(repo_root, tmp_state):
    """Scores dict includes all valid candidates."""
    conn, _ = tmp_state

    rng = random.Random(42)
    result = models.choose(conn, "implement", "demo", 1, rng, root=repo_root)

    scores = result["scores"]
    # Should have scores for all free candidates plus claude if in candidates
    assert "nvidia/nemotron-3-ultra-550b-a55b:free" in scores
    assert "poolside/laguna-s-2.1:free" in scores
    assert "cohere/north-mini-code:free" in scores
    # Each score entry should have the expected keys
    for model, details in scores.items():
        assert "p_pass" in details
        assert "passes" in details
        assert "fails" in details
        assert "median_minutes" in details
        assert "speed_penalty" in details
        assert "cost_penalty" in details
        assert "score" in details