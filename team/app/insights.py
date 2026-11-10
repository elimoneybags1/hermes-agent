"""Dashboard projections over durable Team records; never execute project work."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from team import gates, ledger, profiles, roles, routines, targets

IDEA_SCHEMA = """CREATE TABLE IF NOT EXISTS dashboard_ideas (
    id INTEGER PRIMARY KEY, target TEXT NOT NULL, title TEXT NOT NULL,
    body TEXT NOT NULL, source TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'inbox',
    created TEXT NOT NULL, updated TEXT NOT NULL, goal_id INTEGER REFERENCES goals(id))"""


def _table(conn, name):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()


def _rows(conn, table, target, limit=200):
    # Table names are internal constants, never request parameters.
    if not _table(conn, table):
        return []
    sql = f'SELECT * FROM {table}'
    args = []
    if target:
        sql += ' WHERE target=?'
        args.append(target)
    return [dict(r) for r in conn.execute(sql + ' ORDER BY rowid DESC LIMIT ?', [*args, limit])]


def receipt(target, name):
    path = ledger.state_dir() / 'targets' / target / 'scout' / f'{name}.json'
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        if not isinstance(data, dict):
            raise ValueError('receipt must be an object')
    except (ValueError, OSError) as exc:
        return {'error': str(exc)}
    keys = ('head', 'dirty', 'model', 'calls', 'cache_hits', 'pending_chunks', 'complete',
            'input_tokens', 'output_tokens', 'usage_missing', 'errors', 'summary',
            'ideas', 'answered_by', 'usage', 'evidence_chars', 'phase', 'updated_at', 'note')
    return {**{k: data[k] for k in keys if k in data},
            'saved_at': datetime.fromtimestamp(path.stat().st_mtime, UTC).isoformat(),
            'freshness': 'Saved receipt; current checkout freshness has not been checked'}


def snapshot(conn, target=None):
    if target:
        targets.require(target)
    projects = []
    routine_rows = []
    for t in targets.list_targets():
        if target and t['name'] != target:
            continue
        projects.append({k: t.get(k) for k in ('name', 'path', 'enabled', 'check', 'scout', 'attached_at')}
                        | {'scout_report': receipt(t['name'], 'report'), 'ideas_report': receipt(t['name'], 'ideas'),
                           'review_status': receipt(t['name'], 'review-status')})
        try:
            routine_rows.extend({**r, 'target': t['name'], 'target_enabled': t.get('enabled', True)}
                                for r in routines.load(t['name']))
        except (ValueError, OSError) as exc:
            routine_rows.append({'target': t['name'], 'name': 'Configuration error', 'error': str(exc)})
    jobs = ledger.list_jobs(conn, target=target, limit=500)
    goals = _rows(conn, 'goals', target)
    # Joining by part/order includes earlier attempts, not only the goal's current job.
    for job in jobs:
        order = conn.execute('SELECT goal_id FROM orders WHERE id=?', (job.get('order_id'),)).fetchone()
        job['goal_ids'] = [g['id'] for g in goals if g['job_id'] == job['id']
                           or (job.get('part_id') is not None and g['part_id'] == job['part_id'])
                           or (order and g['id'] == order['goal_id'])]
    return {
        'generated_at': datetime.now(UTC).isoformat(), 'target': target,
        'targets': [t['name'] for t in targets.list_targets()], 'projects': projects,
        'jobs': jobs, 'goals': goals, 'ideas': _rows(conn, 'dashboard_ideas', target),
        'decisions': _rows(conn, 'decisions', target), 'reviews': _rows(conn, 'reviews', target),
        'routines': routine_rows,
        'routine_runs': _rows(conn, 'routine_runs', target),
        'pipelines': [{'type': k, 'landing': gates.LANDING[k],
                       'stages': [{'name': s.name, 'role': s.role, 'gate': s.gate} for s in v]}
                      for k, v in gates.PIPELINES.items()],
        'limits': {'jobs': 500, 'other_records': 200},
    }


def job_detail(conn, job_id):
    job = ledger.get_job(conn, job_id)
    if job is None:
        raise ValueError('unknown job')
    calls = [dict(r) for r in conn.execute(
        'SELECT id, ts, provider, model, response_model, tokens_in, tokens_out, '
        'cache_read_tokens, cost_usd, cost_basis, ok, latency_ms, error, goal_id '
        'FROM calls WHERE job_id=? ORDER BY id', (job_id,))]
    order = conn.execute('SELECT body, version, created FROM orders WHERE id=?', (job.get('order_id'),)).fetchone()
    diagnoses = [dict(r) for r in conn.execute('SELECT * FROM diagnoses WHERE job_id=? ORDER BY id', (job_id,))]
    return {'job': job, 'calls': calls, 'order': dict(order) if order else None, 'diagnoses': diagnoses,
            'gateway_attempts': gateway_attempts(conn, job),
            'limitations': 'Tool-call traces and parent-session links are not recorded in this ledger. '
                            'Reported provider may identify the gateway rather than the upstream deployment.'}


def gateway_attempts(conn, job):
    """Only attribute gateway attempts with a recorded session link, never time alone."""
    sessions = {s for row in conn.execute('SELECT session_id,gateway_session_id FROM calls WHERE job_id=?',
                                         (job['id'],)) for s in row if s}
    path = ledger.state_dir() / 'litellm' / 'usage.db'
    if not sessions or not path.exists():
        return {'records': [], 'note': 'No session-linked gateway evidence available'}
    marks = ','.join('?' for _ in sessions)
    try:
        with sqlite3.connect(f'{path.as_uri()}?mode=ro', uri=True) as gateway:
            gateway.row_factory = sqlite3.Row
            rows = gateway.execute(
                'SELECT ts,model,deployment,ok,rate_limited,error FROM calls '
                f'WHERE session IN ({marks}) AND julianday(ts)>=julianday(?) '
                'AND julianday(ts)<=julianday(?) ORDER BY ts,id',
                (*sessions, job['started'], job.get('ended') or ledger._now()),
            ).fetchall()
        return {'records': [dict(r) for r in rows],
                'note': 'Gateway attempts linked by session and attempt time. Ordering alone does not prove the fallback trigger.'}
    except sqlite3.Error as exc:
        return {'records': [], 'note': f'Gateway evidence unavailable: {exc}'}


def agent_detail(root: Path, conn, name, target=None):
    if name not in roles.names(root):
        raise ValueError('unknown registered agent')
    if target:
        targets.require(target)
    base = roles.load(root, name)
    effective = roles.load(root, name, target=target)
    cfg = profiles.role_config(root, effective)
    toolsets = cfg.get('platform_toolsets', {}).get('cli', [])
    from toolsets import resolve_toolset

    tools = [{'toolset': t, 'tools': resolve_toolset(t, include_registry=False),
              'origin': 'role/target configuration' if t in effective.get('tools', []) else 'profile configuration'}
             for t in toolsets]
    skills = [{'name': s, 'content': (Path(p) / 'SKILL.md').read_text(),
               'origin': 'target overlay' if ledger.state_dir() in Path(p).parents else 'role'}
              for s, p in effective['skill_dirs'].items()]
    public = {k: effective.get(k) for k in ('name', 'title', 'does', 'status', 'gate', 'graded_by',
                                            'runs_on', 'models', 'budget', 'escalation', 'soul', 'overlay')}
    fingerprint = hashlib.sha256(json.dumps([public, tools, skills], sort_keys=True).encode()).hexdigest()
    return {'role': public, 'target': target, 'tools': tools, 'skills': skills,
            'base_tools': base.get('tools', []), 'base_instructions': base.get('soul'),
            'fingerprint': fingerprint, 'runs': ledger.list_jobs(conn, target=target, role=name, limit=30),
            'tool_note': 'Static toolset expansion. Service-gated/plugin tools and per-run restrictions '
                         'must be checked in runtime traces; this is not a claim all tools were callable.'}


def save_idea(conn, data):
    target = str(data.get('target') or '')
    targets.require(target)
    title, body = str(data.get('title') or '').strip(), str(data.get('body') or '').strip()
    if not title or len(title) > 200 or len(body) > 20000:
        raise ValueError('Provide a title (1–200 characters) and context up to 20,000 characters')
    source = str(data.get('source') or 'user').strip()
    if len(source) > 2000:
        raise ValueError('Source reference is too long')
    now = datetime.now(UTC).isoformat()
    with conn:
        conn.execute(IDEA_SCHEMA)
        cur = conn.execute('INSERT INTO dashboard_ideas(target,title,body,source,created,updated) VALUES(?,?,?,?,?,?)',
                           (target, title, body, source, now, now))
    return {'id': cur.lastrowid, 'status': 'inbox'}


def update_idea(conn, data):
    status = data.get('status')
    if status not in ('inbox', 'investigating', 'parked', 'linked', 'dismissed'):
        raise ValueError('Invalid idea status')
    if not _table(conn, 'dashboard_ideas'):
        raise ValueError('unknown idea')
    idea = conn.execute('SELECT * FROM dashboard_ideas WHERE id=?', (int(data['id']),)).fetchone()
    if idea is None:
        raise ValueError('unknown idea')
    goal_id = data.get('goal_id') if status == 'linked' else None
    if status == 'linked':
        goal = ledger.get_goal(conn, int(goal_id)) if goal_id else None
        if goal is None or goal['target'] != idea['target']:
            raise ValueError('Link an existing goal in the same project')
    with conn:
        conn.execute('UPDATE dashboard_ideas SET status=?,goal_id=?,updated=? WHERE id=?',
                     (status, goal_id, datetime.now(UTC).isoformat(), idea['id']))
    return {'id': idea['id'], 'status': status}
