"""Model selection for each role attempt: Thompson sampling over the ledger."""

from __future__ import annotations

import random
import sqlite3
import tomllib
from pathlib import Path
from typing import Any

from team.free import is_free_model
from team.settings import load as load_settings


def _read_role_models(root: Path, role: str) -> tuple[list[str], str]:
    """Return (candidates, pinned) from roles/<role>/role.toml [models]."""
    role_file = root / "roles" / role / "role.toml"
    if not role_file.exists():
        return [], ""
    with role_file.open("rb") as f:
        data = tomllib.load(f)
    models = data.get("models", {})
    candidates = models.get("candidates", [])
    pinned = models.get("pinned", "")
    if not isinstance(candidates, list):
        candidates = []
    if not isinstance(pinned, str):
        pinned = ""
    return [str(c) for c in candidates], pinned


def _ledger_stats(
    conn: sqlite3.Connection, role: str, target: str, days: int = 60
) -> dict[str, dict[str, float]]:
    """
    Return per-model stats for the role:
    {model: {"passes": n, "fails": n, "total": n, "median_minutes": float}}
    Target's jobs weighted 3x, other targets 1x, last N days.
    """
    sql = """
        SELECT
            c.model,
            j.target,
            j.outcome,
            j.started,
            j.ended
        FROM jobs j
        JOIN calls c ON c.job_id = j.id
        WHERE j.role = ?
          AND j.outcome IS NOT NULL
          AND j.started >= datetime('now', ?)
    """
    cutoff = f"-{days} days"
    rows = conn.execute(sql, (role, cutoff)).fetchall()

    stats: dict[str, dict[str, float]] = {}
    durations_map: dict[str, list[float]] = {}
    for row in rows:
        model = row["model"]
        is_target = row["target"] == target
        weight = 3 if is_target else 1
        outcome = row["outcome"]
        if model not in stats:
            stats[model] = {"passes": 0.0, "fails": 0.0, "total": 0.0}
            durations_map[model] = []
        if outcome == "pass":
            stats[model]["passes"] += weight
        elif outcome in ("fail", "error", "timeout"):
            stats[model]["fails"] += weight
        stats[model]["total"] += weight
        # Duration in minutes
        if row["started"] and row["ended"]:
            try:
                from datetime import datetime
                start = datetime.fromisoformat(row["started"].replace("Z", "+00:00"))
                end = datetime.fromisoformat(row["ended"].replace("Z", "+00:00"))
                mins = (end - start).total_seconds() / 60.0
                if mins > 0:
                    durations_map[model].append(mins)
            except Exception:
                pass

    # Compute median minutes
    for model, s in stats.items():
        durations = durations_map.get(model, [])
        if durations:
            durations.sort()
            mid = len(durations) // 2
            s["median_minutes"] = durations[mid] if len(durations) % 2 == 1 else (durations[mid - 1] + durations[mid]) / 2
        else:
            s["median_minutes"] = 0.0

    return stats


def _score_model(
    model: str,
    stats: dict[str, dict[str, float]],
    settings: dict[str, Any],
    rng: random.Random,
    explore: bool,
) -> tuple[float, dict]:
    """
    Score a model: Beta(passes+1, fails+1) sample minus speed and cost penalties.
    Returns (score, details_dict).
    """
    s = stats.get(model, {"passes": 0, "fails": 0, "total": 0, "median_minutes": 0.0})
    passes = s["passes"]
    fails = s["fails"]
    median_minutes = s.get("median_minutes", 0.0)

    # Thompson sampling: Beta(passes+1, fails+1)
    if explore:
        p_pass = rng.betavariate(passes + 1, fails + 1)
    else:
        # Mean of Beta
        p_pass = (passes + 1) / (passes + fails + 2)

    # Speed penalty: median minutes * minute_weight
    minute_weight = settings.get("models.minute_weight", 0.005)
    speed_penalty = median_minutes * minute_weight

    # Cost penalty
    if model == "claude":
        cost_penalty = settings.get("models.claude_cost", 0.3)
    else:
        cost_penalty = 0.0

    score = p_pass - speed_penalty - cost_penalty

    details = {
        "p_pass": p_pass,
        "passes": passes,
        "fails": fails,
        "median_minutes": median_minutes,
        "speed_penalty": speed_penalty,
        "cost_penalty": cost_penalty,
        "score": score,
    }
    return score, details


def choose(
    conn: sqlite3.Connection,
    role: str,
    target: str,
    attempt: int,
    rng: random.Random,
    *,
    root: Path,
    previous_model: str | None = None,
) -> dict[str, Any]:
    """
    Choose a model for this attempt.

    Returns dict with keys: model, why, scores (dict model->details).
    """
    # 1. Load role's candidates and pinned
    candidates, pinned = _read_role_models(root, role)

    # Filter to free models only (fail closed)
    free_candidates = [c for c in candidates if is_free_model(c)]
    if not free_candidates and "claude" not in candidates:
        raise ValueError(f"role {role}: no free candidates and claude not allowed")

    # If pinned is set and valid, use it
    if pinned:
        if pinned not in candidates:
            raise ValueError(f"role {role}: pinned model {pinned!r} not in candidates")
        if pinned != "claude" and not is_free_model(pinned):
            raise ValueError(f"role {role}: pinned model {pinned!r} is not free")
        # Pinned wins - still compute scores for logging
        settings = load_settings(root)
        stats = _ledger_stats(conn, role, target)
        scores = {}
        for m in candidates:
            score, details = _score_model(m, stats, settings, rng, settings.get("models.explore", True))
            scores[m] = details
        return {"model": pinned, "why": "pinned", "scores": scores}

    # 2. Get settings
    settings = load_settings(root)

    # 3. Get ledger stats
    stats = _ledger_stats(conn, role, target)

    # 4. On retry, avoid the model that just failed (passed by caller)
    failed_model = previous_model

    # 5. Score each candidate
    explore = settings.get("models.explore", True)
    scored = []
    for model in candidates:
        if model == "claude":
            # Claude is always allowed (subscription)
            pass
        elif not is_free_model(model):
            # Non-free, non-claude: skip (fail closed)
            continue
        score, details = _score_model(model, stats, settings, rng, explore)
        scored.append((model, score, details))

    # 6. On retry, avoid the model that just failed (unless it's the only one)
    if failed_model and len(scored) > 1:
        scored = [(m, s, d) for m, s, d in scored if m != failed_model]
        if not scored:
            # Only the failed model left - allow it
            scored = [(m, s, d) for m, s, d in scored if m == failed_model]

    # 7. Pick the best score
    scored.sort(key=lambda x: x[1], reverse=True)
    best_model, best_score, best_details = scored[0]

    # Build scores dict for all candidates (for logging) - use the same scored list
    all_scores = {}
    for model, score, details in scored:
        all_scores[model] = details
    # Include filtered-out candidates with a reason
    for model in candidates:
        if model not in all_scores:
            if model == "claude" or is_free_model(model):
                # This was filtered out due to retry avoidance
                _, details = _score_model(model, stats, settings, rng, explore)
                all_scores[model] = details
                all_scores[model]["filtered_reason"] = "avoided on retry"
            else:
                all_scores[model] = {"filtered_reason": "not free"}

    why = "thompson"
    if failed_model and best_model != failed_model:
        why = f"thompson (avoided {failed_model})"
    elif not explore:
        why = "mean (explore=off)"

    return {
        "model": best_model,
        "why": why,
        "scores": all_scores,
    }