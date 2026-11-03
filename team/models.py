"""Choose only role-allowed models using graded work, never gateway failures."""
from __future__ import annotations

import json

from team import catalog, roles

# Candidates whose smoothed pass rate is within this of the leader's are ranked by input
# tokens per attempt instead: a model that passes as often for less context wins.
RATE_MARGIN = 0.1


def _tokens_per_attempt(conn, role_name: str, candidates: list[str]) -> dict[str, float]:
    """Input tokens per finished attempt of each candidate (infra deaths left out)."""
    rows = conn.execute(
        "SELECT j.model_choice, COUNT(DISTINCT j.id) AS attempts, COALESCE(SUM(c.tokens_in), 0) AS tokens"
        " FROM jobs j JOIN calls c ON c.job_id = j.id"
        " WHERE j.role = ? AND j.ended IS NOT NULL AND (j.outcome = 'pass' OR j.failure_kind = 'work')"
        " GROUP BY j.model_choice",
        (role_name,),
    ).fetchall()
    totals: dict[str, list[int]] = {spec: [0, 0] for spec in candidates}
    for row in rows:
        spec = json.loads(row['model_choice'] or '{}').get('spec')
        if spec in totals:
            totals[spec][0] += int(row['tokens'] or 0)
            totals[spec][1] += int(row['attempts'] or 0)
    return {spec: (t / n) for spec, (t, n) in totals.items() if n}


def choose(conn, role: dict, attempt: int, cat: dict, target: str | None = None):
    fallback = roles.candidate(role, attempt)
    candidates = role.get('models', {}).get('candidates') or []
    if not fallback:
        raise catalog.GateError(f"role {role['name']} has no candidates")
    if role.get('models', {}).get('pinned'):
        return catalog.resolve(fallback, cat)._replace(why='explicit role pin')
    rows = conn.execute("SELECT target, model_choice, outcome FROM jobs WHERE role = ? "
                        "AND ended IS NOT NULL AND (outcome = 'pass' OR failure_kind = 'work')",
                        (role['name'],)).fetchall()
    local = [r for r in rows if r['target'] == target]
    sample, scope = (local, 'target') if len(local) >= 8 else (rows, 'role pooled')
    counts = {spec: [0, 0] for spec in candidates}
    for row in sample:
        spec = json.loads(row['model_choice'] or '{}').get('spec')
        if spec in counts:
            counts[spec][0] += row['outcome'] == 'pass'
            counts[spec][1] += 1
    total = sum(n for _, n in counts.values())
    passed = sum(p for p, _ in counts.values())
    if total < 8:
        return catalog.resolve(fallback, cat)._replace(why=f'candidate order: only {total} graded work attempts')
    # One in five choices explores the least-observed candidate. Other choices use
    # Laplace-smoothed work pass rate, avoiding a spurious 1/1 perfect winner; candidates
    # that pass about as often as the leader are ordered by input tokens per attempt.
    cost = _tokens_per_attempt(conn, role['name'], candidates)
    if total % 5 == 0:
        spec = min(candidates, key=lambda s: counts[s][1])
        policy = 'explore least observed'
    else:
        rate = {s: (counts[s][0] + 1) / (counts[s][1] + 2) for s in candidates}
        ranked = sorted(candidates, key=lambda s: rate[s], reverse=True)
        near = [s for s in ranked if rate[ranked[0]] - rate[s] <= RATE_MARGIN]
        near.sort(key=lambda s: cost.get(s, float('inf')))
        ranked = near + [s for s in ranked if s not in near]
        spec = ranked[min(attempt - 1, len(ranked) - 1)]
        policy = 'ranked work pass rate, then tokens per attempt' if len(near) > 1 else 'ranked work pass rate'
    p, n = counts[spec]
    why = f'{policy}, {scope}: {p}/{n} passed; baseline {passed}/{total}; infra excluded'
    if spec in cost:
        why += f'; {int(cost[spec]):,} input tokens per attempt'
    return catalog.resolve(spec, cat)._replace(why=why)
