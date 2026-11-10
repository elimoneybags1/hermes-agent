"""Historical usage and capability evidence with unknown values kept explicit."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from team import ledger, targets


def _bucket():
    return {'records': 0, 'input': 0, 'output': 0, 'cached': 0, 'known_cost_usd': 0,
            'missing_input': 0, 'missing_output': 0, 'missing_cache': 0,
            'unknown_cost': 0, 'estimated_cost': 0, 'failed': 0}


def _add(bucket, call):
    bucket['records'] += 1
    bucket['failed'] += not call['ok']
    for key, col in (('input', 'tokens_in'), ('output', 'tokens_out'), ('cached', 'cache_read_tokens')):
        bucket[key] += call[col] or 0
        bucket[{'input': 'missing_input', 'output': 'missing_output', 'cached': 'missing_cache'}[key]] += call[col] is None
    if call['cost_usd'] is None or call['cost_basis'] not in ('reported', 'estimated'):
        bucket['unknown_cost'] += 1
    else:
        bucket['known_cost_usd'] += call['cost_usd']
        bucket['estimated_cost'] += call['cost_basis'] == 'estimated'


def reset_baseline(conn):
    """Start a new measurement period without deleting calls or grading evidence."""
    with conn:
        conn.execute('CREATE TABLE IF NOT EXISTS usage_baseline '
                     '(id INTEGER PRIMARY KEY CHECK(id=1), call_id INTEGER NOT NULL, started_at TEXT NOT NULL)')
        call_id = conn.execute('SELECT COALESCE(MAX(id),0) FROM calls').fetchone()[0]
        started = datetime.now(UTC).isoformat()
        conn.execute('INSERT OR REPLACE INTO usage_baseline VALUES (1,?,?)', (call_id, started))
    return {'started_at': started, 'after_call_id': call_id, 'history_preserved': True}


def _tier_usage(rows):
    """Group by the route actually requested, never today's catalog membership."""
    groups = {}
    for row in rows:
        requested = row['model'] or ''
        tier = requested if requested.startswith('tier-') else 'direct'
        group = groups.setdefault(tier, {'label': tier[5:].replace('-', ' ').title() if tier != 'direct'
                                        else 'Direct model requests', 'total': _bucket(), 'models': {}})
        reported = (row['response_model'] or '').strip()
        if not reported or reported.rsplit('/', 1)[-1].startswith('tier-'):
            reported = None
        key = ('reported', reported) if reported else ('unreported', requested)
        model = group['models'].setdefault(key, {'model': reported,
                    'requested_model': requested if not reported else None, 'usage': _bucket()})
        _add(model['usage'], row)
        _add(group['total'], row)
    return {tier: {**group, 'models': list(group['models'].values())} for tier, group in groups.items()}


def history(conn, target=None, days=30, model=None, role=None, *, include_history=False):
    if target:
        targets.require(target)
    if not 1 <= days <= 366:
        raise ValueError('days must be between 1 and 366')
    since = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    sql = ('SELECT c.id, c.ts, c.job_id, c.goal_id, c.provider, c.model, c.response_model, '
           'c.tokens_in,c.tokens_out,c.cache_read_tokens,c.cost_usd,c.cost_basis,c.ok,c.latency_ms, '
           'COALESCE(c.target,j.target,g.target) AS target, '
           "COALESCE(NULLIF(NULLIF(c.task_class,'unknown'),''),j.role,'unknown') AS role "
           'FROM calls c LEFT JOIN jobs j ON j.id=c.job_id LEFT JOIN goals g ON g.id=c.goal_id '
           'WHERE julianday(c.ts)>=julianday(?)')
    args = [since]
    baseline = None
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='usage_baseline'").fetchone():
        baseline = conn.execute('SELECT call_id,started_at FROM usage_baseline WHERE id=1').fetchone()
    if baseline and not include_history:
        sql += ' AND c.id>?'
        args.append(baseline['call_id'])
    if target:
        sql += ' AND COALESCE(c.target,j.target,g.target)=?'
        args.append(target)
    rows = [dict(r) for r in conn.execute(sql + ' ORDER BY c.ts,c.id', args)]
    available_models = sorted({r['response_model'] or r['model'] for r in rows})
    available_roles = sorted({r['role'] for r in rows})
    rows = [r for r in rows if (not model or (r['response_model'] or r['model']) == model)
            and (not role or r['role'] == role)]
    daily, models, total = {}, {}, _bucket()
    for row in rows:
        # Offset-aware timestamps grouped in UTC, matching the filter and chart label.
        day = datetime.fromisoformat(row['ts']).astimezone(UTC).date().isoformat()
        _add(daily.setdefault(day, _bucket()), row)
        _add(models.setdefault(row['response_model'] or row['model'], _bucket()), row)
        _add(total, row)
    return {'since': since, 'days': days, 'timezone': 'UTC', 'total': total,
            'baseline_at': baseline['started_at'] if baseline else None,
            'includes_history': include_history,
            'daily': [{'day': k, **v} for k, v in sorted(daily.items())], 'by_model': models,
            'by_tier': _tier_usage(rows),
            'models': available_models, 'roles': available_roles, 'records': rows[-200:],
            'records_total': len(rows), 'records_limit': 200,
            'note': 'Recorded usage only. Legacy scout receipts are shown on Overview, not backfilled '
                    'as dated calls. Cached tokens may be included in input; do not add them. '
                    'Subscription usage is not an API invoice. Missing costs remain unknown.'}


def capability(conn, target=None):
    rows = ledger.list_jobs(conn, target=target)
    groups, baselines = {}, {}
    for r in rows:
        if not r.get('ended') or r['role'] == ledger.CODE_ROLE:
            continue
        # Use the work order or current goal linkage; unknown category stays explicit.
        goal = conn.execute('SELECT g.type FROM goals g LEFT JOIN orders o ON o.goal_id=g.id '
                            'WHERE g.job_id=? OR o.id=? OR (g.part_id IS NOT NULL AND g.part_id=?) '
                            'ORDER BY g.id DESC LIMIT 1', (r['id'], r.get('order_id'), r.get('part_id'))).fetchone()
        kind = goal['type'] if goal else 'unclassified'
        model = (r.get('model_choice') or {}).get('spec') or 'unreported'
        key = (r['role'], kind, model)
        bucket = groups.setdefault(key, {'passed': 0, 'work': 0, 'infra': 0, 'unclassified': 0})
        baseline = baselines.setdefault((r['role'], kind), {'passed': 0, 'work': 0})
        if r['outcome'] == 'pass' or r.get('failure_kind') == 'work':
            bucket['work'] += 1
            bucket['passed'] += r['outcome'] == 'pass'
            baseline['work'] += 1
            baseline['passed'] += r['outcome'] == 'pass'
        else:
            bucket['infra' if r.get('failure_kind') == 'infra' else 'unclassified'] += 1
    return [{'role': r, 'task_type': t, 'model': m, **v, 'baseline': baselines[(r, t)]}
            for (r, t, m), v in sorted(groups.items())]


def routing(cat):
    if cat is None:
        return {'models': [], 'fallbacks': {}, 'error': 'Catalog unavailable'}
    return {'models': [{'id': m['id'], 'tier': m.get('tier'), 'order': m.get('order'),
                        'providers': [{'provider': p['provider'], 'unavailable': p.get('unavailable')}
                                      for p in m.get('served_by', [])]}
                       for m in cat.get('models', [])],
            'fallbacks': cat.get('tier_fallbacks', {}),
            'context_fallbacks': cat.get('policy', {}).get('litellm', {}).get('context_window_fallbacks', {}),
            'note': 'Configured routes, not proof of availability or upstream identity. '
                    'Candidate ranking and quality escalation belong to each agent policy.'}
