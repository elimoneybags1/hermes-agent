"""Opt-in per-target routines, claimed durably before work so restarts cannot duplicate it."""
from __future__ import annotations

import hashlib
import json
import tomllib
from pathlib import Path

from team import ledger, targets


class RoutineError(ValueError):
    pass


def load(target: str) -> list[dict]:
    targets.require(target)
    path = ledger.state_dir() / 'targets' / target / 'routines.toml'
    if not path.exists():
        return []
    with path.open('rb') as f:
        data = tomllib.load(f)
    routines = data.get('routines', [])
    names = set()
    for r in routines:
        name = r.get('name', '')
        if not targets.NAME_RE.fullmatch(name) or name in names:
            raise RoutineError('routine names must be unique target-style names')
        names.add(name)
        if not isinstance(r.get('enabled', False), bool):
            raise RoutineError(f'{name}: enabled must be boolean')
        interval = r.get('every_s')
        if type(interval) is not int or interval < 60:
            raise RoutineError(f'{name}: every_s must be an integer >= 60')
        if r.get('action') not in ('scout', 'goal'):
            raise RoutineError(f'{name}: action must be scout or goal')
        if r['action'] == 'goal' and not isinstance(r.get('goal'), str):
            raise RoutineError(f'{name}: goal text required')
    return routines


def tick(root: Path, conn, target: str, now: float, *, runner, cat, log, scan=None, plan=None):
    from team import run, scout

    record = targets.require(target)
    if not record.get('enabled', True) or ledger.active_pause(conn, target, now):
        return
    scan = scan or scout.scan
    plan = plan or run.plan_goal
    for r in load(target):
        if not r.get('enabled', False):
            continue
        if r['action'] == 'goal' and ledger.active_goals(conn, target):
            continue
        if r['action'] == 'goal' and not record.get('check'):
            log(f"routine {r['name']}: onboard this target's check first")
            continue
        key = hashlib.sha256(json.dumps(r, sort_keys=True).encode()).hexdigest()
        slot = int(now // r['every_s'])
        with conn:
            conn.execute('CREATE TABLE IF NOT EXISTS routine_runs ('
                         'target TEXT, name TEXT, config TEXT, slot INTEGER, '
                         'status TEXT, detail TEXT, PRIMARY KEY(target, name, config, slot))')
            cur = conn.execute('INSERT OR IGNORE INTO routine_runs VALUES (?, ?, ?, ?, ?, ?)',
                               (target, r['name'], key, slot, 'claimed', ''))
        if not cur.rowcount:
            continue
        # A crash leaves claimed, not retryable: inspect the ledger before retrying a
        # partially-created plan. A new interval must not bypass that unresolved claim.
        prior = conn.execute("SELECT 1 FROM routine_runs WHERE target=? AND name=? "
                             "AND status='claimed' AND NOT(config=? AND slot=?)",
                             (target, r['name'], key, slot)).fetchone()
        status, detail = 'blocked', 'unresolved previous claim; inspect before scheduling again'
        if not prior:
            try:
                if r['action'] == 'scout':
                    result = scan(target, max_calls=4)
                    if result['errors']:
                        raise RoutineError('; '.join(result['errors']))
                    detail = f"scout: {result['calls']} calls, {result['pending_chunks']} chunks pending"
                else:
                    goals = plan(root, conn, target, r['goal'], runner=runner, cat=cat, log=log)
                    detail = 'goals: ' + ', '.join(str(g['id']) for g in goals)
                status = 'done'
            except (ValueError, run.RunError, OSError) as e:
                status, detail = 'error', str(e)
        with conn:
            conn.execute('UPDATE routine_runs SET status=?, detail=? WHERE target=? AND name=? AND config=? AND slot=?',
                         (status, detail, target, r['name'], key, slot))
        log(f"routine {target}/{r['name']}: {status}: {detail}")
