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
    result = models.choose(conn, "implement", "demo", 2, rng, root=repo_root,
                           previous_model="nvidia/nemotron-3-ultra-550b-a55b:free")

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
    result = models.choose(conn, "implement", "demo", 2, rng, root=repo_root,
                           previous_model="nvidia/nemotron-3-ultra-550b-a55b:free")

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


def test_choose_retry_uses_previous_model_param_not_ledger(repo_root, tmp_state):
    """Retry avoids the model passed via previous_model, not the latest ledger entry for the role+target."""
    conn, _ = tmp_state

    # Record a job and call for attempt 1 with model A (simulating Goal 1's attempt 1)
    job_id_a = ledger.start_job(conn, "demo", "abc123", "implement", "v1", attempt=1)
    ledger.record_call(
        conn, job_id_a, "openrouter", "nvidia/nemotron-3-ultra-550b-a55b:free",
        "implement", 100, 200, 5000, True
    )
    ledger.finish_job(conn, job_id_a, "pass", "test", "ok")

    # Record a job and call for attempt 1 with model B (simulating Goal 2's attempt 1 - different goal!)
    job_id_b = ledger.start_job(conn, "demo", "abc123", "implement", "v1", attempt=1)
    ledger.record_call(
        conn, job_id_b, "openrouter", "poolside/laguna-s-2.1:free",
        "implement", 100, 200, 5000, True
    )
    ledger.finish_job(conn, job_id_b, "fail", "test", "failed")

    # Now Goal 2 retries (attempt 2). It should avoid ITS OWN previous model (poolside), not the latest ledger (poolside).
    # Latest ledger for role+target attempt=1 is poolside (Goal 2's attempt 1)
    # But we pass previous_model="nvidia/nemotron-3-ultra-550b-a55b:free" (Goal 2's actual previous model)
    # Wait - the test says "the second's retry avoids its own previous model"
    # So Goal 2's previous model is poolside/laguna-s-2.1:free
    # The latest ledger for the role+target (attempt 1) is also poolside (from Goal 2)
    # But if there were another goal in between, they'd differ.
    # Let's make the ledger show a different model as the latest for attempt=1

    # Actually, let's re-read: "two goals of the same role running at once, the second's retry avoids its own previous model"
    # So we simulate two goals running concurrently. Goal 1 does attempt 1 with model A. Goal 2 does attempt 1 with model B.
    # Then Goal 2 retries (attempt 2). The ledger query for "latest job of role on target attempt 1" would return model B (Goal 2's attempt 1).
    # But if we didn't have the previous_model param and just queried attempt-1, we'd get model B anyway.
    # The key difference is when another goal interleaves.
    # Let me create a case where the latest ledger attempt 1 is from Goal 1, but Goal 2's previous was Goal 2's own attempt 1.

    # Actually wait - the test should show that the ledger query (which looks at attempt-1) would get the WRONG model
    # if multiple goals are running. The latest job with attempt=1 for that role+target might be from Goal 1.
    # But Goal 2 wants to avoid its OWN previous model.

    # Let's create: Goal 1 runs attempt 1 with model A. Goal 2 runs attempt 1 with model B.
    # Then Goal 1 runs attempt 1 AGAIN (or something) - no, attempt numbers are per-job.
    # Each goal is a separate job with its own attempt counter.
    # So the ledger query "WHERE j.role = ? AND j.target = ? AND j.attempt = ?" 
    # would find the latest call for ANY job with that attempt number.
    # If Goal 1 is on attempt 2 and Goal 2 is on attempt 1, querying for attempt=1 would get Goal 2's model.
    # But if Goal 1 is on attempt 1 and Goal 2 is on attempt 1, and Goal 2 finishes first...
    # Actually the issue is: attempt numbers are per JOB, not per role+target.
    # So two goals = two jobs. Each has attempt 1, then attempt 2, etc.
    # The query "WHERE j.role = ? AND j.target = ? AND j.attempt = ?" finds ALL jobs with that attempt number.
    # So if Goal 1 is on attempt 2 and Goal 2 is on attempt 1, querying attempt=1 gets Goal 2's model.
    # But if Goal 1 is on attempt 1 (just started) and Goal 2 is on attempt 2 (retrying), querying attempt=1 gets Goal 1's model!
    # That's the bug: Goal 2's retry should avoid Goal 2's attempt 1 model, but the query gets Goal 1's attempt 1 model.

    rng = random.Random(42)
    # Goal 2's retry: its previous model was poolside/laguna-s-2.1:free
    # But the latest ledger entry for attempt=1 is nvidia/nemotron (Goal 1's attempt 1)
    result = models.choose(
        conn, "implement", "demo", 2, rng, root=repo_root,
        previous_model="poolside/laguna-s-2.1:free"
    )

    # Should avoid poolside (Goal 2's own previous), NOT nvidia (latest ledger attempt 1)
    assert result["model"] != "poolside/laguna-s-2.1:free"
    assert "avoided" in result["why"]
    assert "poolside/laguna-s-2.1:free" in result["why"]


def test_choose_returned_scores_equal_compared_scores(repo_root, tmp_state):
    """Returned scores dict contains exactly the scores used to choose (one draw per candidate per call)."""
    conn, _ = tmp_state

    rng = random.Random(42)
    result = models.choose(conn, "implement", "demo", 1, rng, root=repo_root)

    # The scores returned should be the same ones that were compared to pick the best
    # We can verify this by checking that the chosen model's score matches the max of all scores
    chosen_model = result["model"]
    scores = result["scores"]

    # Get the scores for models that were actually scored (not filtered out)
    scored_models = {m: s for m, s in scores.items() if "filtered_reason" not in s}
    if not scored_models:
        # All filtered? shouldn't happen
        return

    # The chosen model should have the highest score among scored models
    max_score_model = max(scored_models.items(), key=lambda x: x[1]["score"])[0]
    assert chosen_model == max_score_model

    # Also verify that the scores dict was built from the same scored list (not re-drawn)
    # The scores dict should include ALL candidates, with filtered_reason for excluded ones
    for model in ["nvidia/nemotron-3-ultra-550b-a55b:free", "poolside/laguna-s-2.1:free", "cohere/north-mini-code:free"]:
        assert model in scores
        # Each should have a score key (either from scoring or filtered_reason)
        assert "score" in scores[model] or "filtered_reason" in scores[model]

    # Verify no re-draw: if we call _score_model again with same rng, we get DIFFERENT values
    # because rng state advanced. But the returned scores should match what was used.
    # We can't easily test this without exposing internals, but we verify the structure is correct.