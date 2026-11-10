"""Activity has stable identities, durable read state and no model-call side effects."""
from pathlib import Path

from team import ledger
from team.app import alerts, chat, server
from tests.team.test_app import Proc, popen_with, result
from tests.team.test_scout import repo

ROOT = Path(__file__).resolve().parents[2]


def connect():
    conn = ledger.connect()
    ledger.init(conn)
    return conn


def test_started_and_finished_runs_are_distinct_and_acknowledgements_survive_reload(tmp_path):
    repo(tmp_path)
    with connect() as conn:
        job = ledger.start_job(conn, 'demo', 'sha', 'review', 'v')
        first = alerts.collect(conn, {'messages': []})
        event = next(e for e in first['events'] if e['id'] == f'job:{job}:start')
        assert event['href'] == f'#orchestration?run={job}' and not event['read']
        alerts.mark_read(conn, [event['id']])
        ledger.finish_job(conn, job, 'pass', 'check', 'Verified')
    with connect() as conn:
        events = {e['id']: e for e in alerts.collect(conn, {'messages': []})['events']}
        assert events[f'job:{job}:start']['read'] is True
        assert events[f'job:{job}:end']['read'] is False
        assert events[f'job:{job}:end']['kind'] == 'success'
        assert events[f'job:{job}:end']['body'] == 'Verified'


def test_plain_message_goes_to_boss_without_project_form_and_reply_is_an_alert(tmp_path):
    repo(tmp_path)
    process = popen_with(Proc([result('conversation-1', 'Let’s explore the idea together.', 20, 8, 0, 0)]))
    conversation = chat.Chat(ROOT, popen=process, refresh=lambda root: None, aide=False)
    app = server.App(ROOT, chat=conversation)
    assert app.post('/api/chat', {'text': 'I have an idea but have not picked a project.', 'to': 'boss'})['to'] == 'boss'
    conversation.drain()
    assert len(process.calls) == 1
    feed = app.get('/api/alerts', {})
    reply = next(e for e in feed['events'] if e['kind'] == 'message')
    assert reply['body'] == 'Let’s explore the idea together.'
    assert reply['href'] == '#messages'
    app.post('/api/alerts/read', {'ids': [reply['id']]})
    assert next(e for e in app.get('/api/alerts', {})['events'] if e['id'] == reply['id'])['read']
    assert len(process.calls) == 1  # Polling alerts never starts a model request.


def test_alert_poll_does_not_consume_boss_notes(tmp_path, monkeypatch):
    repo(tmp_path)
    conversation = chat.Chat(ROOT, aide=False)
    monkeypatch.setattr(conversation, 'poll', lambda: (_ for _ in ()).throw(AssertionError('must not poll')))
    app = server.App(ROOT, chat=conversation)
    assert app.get('/api/alerts', {})['events'] == []


def test_failed_reply_is_visible_and_preview_can_mark_alerts_read(tmp_path):
    repo(tmp_path)
    conversation = chat.Chat(ROOT, aide=False)
    conversation.data['messages'] = [{'role': 'boss', 'text': '', 'error': 'Gateway unavailable',
                                       'at': ledger._now(), 'done': ledger._now()}]
    app = server.App(ROOT, chat=conversation, preview=True)
    feed = app.get('/api/alerts', {})
    assert feed['events'][0]['body'] == 'Gateway unavailable'
    app.post('/api/alerts/read', {'ids': [feed['events'][0]['id']]})
    assert app.get('/api/alerts', {})['unread'] == 0
