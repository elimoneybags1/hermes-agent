"""Choose only role-allowed models using graded work, never gateway failures."""
from __future__ import annotations

import json

from team import catalog, roles


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
    # Laplace-smoothed work pass rate, avoiding a spurious 1/1 perfect winner.
    if total % 5 == 0:
        spec = min(candidates, key=lambda s: counts[s][1])
        policy = 'explore least observed'
    else:
        ranked = sorted(candidates, key=lambda s: (counts[s][0] + 1) / (counts[s][1] + 2), reverse=True)
        spec = ranked[min(attempt - 1, len(ranked) - 1)]
        policy = 'ranked work pass rate'
    p, n = counts[spec]
    return catalog.resolve(spec, cat)._replace(
        why=f'{policy}, {scope}: {p}/{n} passed; baseline {passed}/{total}; infra excluded')
