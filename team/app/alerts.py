"""In-app activity from recorded work, findings and conversation replies; no model calls."""
from __future__ import annotations

import hashlib
from datetime import UTC, datetime

from team import targets
from team.app.insights import _rows, _table, receipt

READ_SCHEMA = '''CREATE TABLE IF NOT EXISTS dashboard_alert_reads (
    event_id TEXT PRIMARY KEY, read_at TEXT NOT NULL)'''


def _event(key, kind, title, body, at, href, target=None):
    return {'id': key, 'kind': kind, 'title': title, 'body': (body or '')[:500],
            'at': at, 'href': href, 'target': target}


def _epoch(value):
    try:
        return datetime.fromisoformat(value).timestamp() if value else 0
    except ValueError:
        return 0


def collect(conn, conversation):
    events = []
    for job in conn.execute('SELECT * FROM jobs ORDER BY julianday(COALESCE(ended,started)) DESC LIMIT 200'):
        events.append(_event(f'job:{job["id"]}:start', 'run', f'{job["role"]} started a run',
                             f'Attempt {job["attempt"]} · {job["target"]}', job['started'],
                             f'#orchestration?run={job["id"]}', job['target']))
        if job['ended']:
            events.append(_event(f'job:{job["id"]}:end', 'success' if job['outcome'] == 'pass' else 'attention',
                                 f'{job["role"]} · {job["outcome"]}', job['grader_detail'], job['ended'],
                                 f'#orchestration?run={job["id"]}', job['target']))
    for d in _rows(conn, 'decisions', None):
        if d['status'] == 'open':
            events.append(_event(f'decision:{d["id"]}', 'decision', d['question'], d['context'],
                                 d['created'], '#work', d['target']))
    for r in _rows(conn, 'reviews', None):
        events.append(_event(f'review:{r["id"]}', 'idea', 'New review findings', r['summary'],
                             r['created'], '#ideas', r['target']))
    for t in targets.list_targets():
        r = receipt(t['name'], 'ideas')
        if r and r.get('ideas'):
            key = hashlib.sha256((r['ideas'] + r['saved_at']).encode()).hexdigest()[:24]
            events.append(_event(f'ideas:{t["name"]}:{key}', 'idea', 'New ideas to explore',
                                 r['ideas'], r['saved_at'], '#ideas', t['name']))
    for index, m in enumerate(conversation.get('messages', [])):
        if m.get('role') not in ('boss', 'aide'):
            continue
        key = hashlib.sha256(f'{index}:{m.get("at")}:{m.get("role")}:{m.get("text")}'.encode()).hexdigest()[:24]
        events.append(_event(f'message:{key}', 'message', f'{m["role"].title()} replied',
                             m.get('text') or m.get('error'), m.get('done') or m.get('at'), '#messages'))
    events.sort(key=lambda e: (_epoch(e['at']), e['id']), reverse=True)
    events = events[:200]
    read = set()
    if events and _table(conn, 'dashboard_alert_reads'):
        marks = ','.join('?' for _ in events)
        read = {r[0] for r in conn.execute(f'SELECT event_id FROM dashboard_alert_reads WHERE event_id IN ({marks})',
                                          [e['id'] for e in events])}
    for event in events:
        event['read'] = event['id'] in read
    return {'events': events, 'unread': sum(not e['read'] for e in events), 'limit': 200,
            'generated_at': datetime.now(UTC).isoformat()}


def mark_read(conn, ids):
    if not isinstance(ids, list) or len(ids) > 200 or any(not isinstance(i, str) or len(i) > 200 for i in ids):
        raise ValueError('Expected up to 200 event IDs')
    with conn:
        conn.execute(READ_SCHEMA)
        conn.executemany('INSERT OR IGNORE INTO dashboard_alert_reads VALUES (?,?)',
                         [(key, datetime.now(UTC).isoformat()) for key in ids])
    return {'ok': True}
