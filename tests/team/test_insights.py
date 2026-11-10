"""Dashboard contracts: attribution, isolated intake, and effective configuration."""
from pathlib import Path

import pytest

from team import ledger, overlay, scout, targets, usage
from team.app import insights, metrics, server
from tests.team.test_scout import repo

ROOT = Path(__file__).resolve().parents[2]


def connect():
    conn = ledger.connect()
    ledger.init(conn)
    return conn


def test_usage_includes_scout_but_never_counts_cached_reads_as_new_calls(tmp_path):
    repo(tmp_path)
    def call(model, prompt):
        return {'text': 'what: a tiny app', 'model': 'reported-small',
                'usage': {'prompt_tokens': 100, 'completion_tokens': 20,
                          'prompt_tokens_details': {'cached_tokens': 80}}}
    scout.scan('demo', call=call)
    scout.scan('demo', call=call)
    with connect() as conn:
        result = metrics.history(conn, 'demo')
        assert result['total']['records'] == 1
        assert result['total']['input'] == 100 and result['total']['cached'] == 80
        assert result['total']['unknown_cost'] == 1
        assert result['records'][0]['response_model'] == 'reported-small'
        assert usage.usage_breakdown(conn, 'demo')['total']['input'] == 100


def test_history_filters_direct_goal_and_job_attribution_without_double_counting(tmp_path):
    repo(tmp_path)
    with connect() as conn:
        goal = ledger.create_goal(conn, 'demo', 'Fix it', 'implement', 'fix', '/tmp/worktree', 'true')
        job = ledger.start_job(conn, 'demo', 'sha', 'implement', 'v')
        ledger.record_call(conn, job, 'gateway', 'requested', 'implement', 10, 2, 3, True,
                           goal_id=goal, cost_usd=.03, cost_basis='reported')
        ledger.record_call(conn, None, 'gateway', 'other', 'ideas', 5, None, 2, False, target='demo')
        ledger.record_call(conn, None, 'gateway', 'unrelated', 'ideas', 9000, 10, 2, True, target='elsewhere')
        d = metrics.history(conn, 'demo')
        assert d['total']['input'] == 15 and d['total']['records'] == 2
        assert d['total']['known_cost_usd'] == .03 and d['total']['unknown_cost'] == 1
        assert d['total']['failed'] == 1 and d['total']['missing_output'] == 1
        assert metrics.history(conn, 'demo', model='other')['total']['input'] == 5
        assert metrics.history(conn, 'demo', role='implement')['total']['input'] == 10
        assert sum(p['input'] for p in d['daily']) == d['total']['input']


def test_reset_starts_fresh_preserves_history_and_counts_only_new_calls(tmp_path):
    repo(tmp_path)
    app = server.App(ROOT, chat=object())
    with connect() as conn:
        ledger.record_call(conn, None, 'gateway', 'old', 'ideas', 100, 20, 3, True, target='demo')
    reset = app.post('/api/metrics/reset', {})
    assert reset['history_preserved']
    assert app.get('/api/metrics', {})['total']['records'] == 0
    with connect() as conn:
        ledger.record_call(conn, None, 'gateway', 'new', 'ideas', 10, 2, 3, True, target='demo')
        current = metrics.history(conn, 'demo')
        assert current['total']['input'] == 10 and current['total']['records'] == 1
        assert current['baseline_at'] == reset['started_at']
        assert conn.execute('SELECT COUNT(*) FROM calls').fetchone()[0] == 2
    historical = app.get('/api/metrics', {'target': ['demo'], 'include_history': ['1']})
    assert historical['total']['input'] == 110 and historical['total']['records'] == 2
    from team.app import edit
    with pytest.raises(edit.EditError, match='Preview'):
        server.App(ROOT, chat=object(), preview=True).post('/api/metrics/reset', {})


def test_tier_usage_keeps_requested_routes_and_separates_unknown_responders(tmp_path):
    repo(tmp_path)
    with connect() as conn:
        for requested, reported, amount in [('tier-medium', 'actual-model', 10),
                                             ('tier-reasoning', 'actual-model', 20),
                                             ('tier-medium', 'tier-medium', 30),
                                             ('tier-medium', None, 40),
                                             ('direct-model', 'direct-model', 50),
                                             ('direct-model', None, 60)]:
            ledger.record_call(conn, None, 'gateway', requested, 'scout', amount, 2, 3, True,
                               response_model=reported, target='demo')
        d = metrics.history(conn, 'demo')
        groups = d['by_tier']
        assert groups['tier-medium']['total']['input'] == 80
        medium = {m['model']: m['usage']['input'] for m in groups['tier-medium']['models']}
        assert medium == {'actual-model': 10, None: 70}
        assert groups['tier-reasoning']['models'][0]['usage']['input'] == 20
        assert len(groups['direct']['models']) == 2
        assert sum(g['total']['input'] for g in groups.values()) == d['total']['input'] == 210
        for group in groups.values():
            assert sum(m['usage']['records'] for m in group['models']) == group['total']['records']
        filtered = metrics.history(conn, 'demo', model='actual-model')['by_tier']
        assert set(filtered) == {'tier-medium', 'tier-reasoning'}


def test_idea_intake_is_durable_without_dispatch_and_links_only_same_project(tmp_path):
    repo(tmp_path)
    app = server.App(ROOT, chat=object())
    item = app.post('/api/ideas', {'target': 'demo', 'title': 'Explore search', 'body': '<script>literal evidence</script>'})
    with connect() as conn:
        assert ledger.list_goals(conn, all_goals=True) == []
        saved = insights.snapshot(conn, 'demo')['ideas'][0]
        assert saved['id'] == item['id'] and '<script>' in saved['body']
        other = ledger.create_goal(conn, 'elsewhere', 'Other', 'implement', 'fix', '/tmp/other', 'true')
        with pytest.raises(ValueError, match='same project'):
            insights.update_idea(conn, {'id': item['id'], 'status': 'linked', 'goal_id': other})
        own = ledger.create_goal(conn, 'demo', 'Search', 'implement', 'fix', '/tmp/own', 'true')
        insights.update_idea(conn, {'id': item['id'], 'status': 'linked', 'goal_id': own})
    assert app.get('/api/insights', {'target': ['demo']})['ideas'][0]['goal_id'] == own


def test_inspector_resolves_target_overlay_and_preserves_historical_role_version(tmp_path):
    repo(tmp_path)
    name = 'implement'
    directory = overlay.role_dir('demo', name)
    directory.mkdir(parents=True)
    (directory / 'SOUL.md').write_text('Project-specific instruction sentinel')
    with connect() as conn:
        job = ledger.start_job(conn, 'demo', 'sha', name, 'historical-version')
        before = insights.agent_detail(ROOT, conn, name)
        after = insights.agent_detail(ROOT, conn, name, 'demo')
        assert 'Project-specific instruction sentinel' in after['role']['soul']
        assert after['fingerprint'] != before['fingerprint']
        assert after['runs'][0]['role_version'] == 'historical-version'
        assert any(t['toolset'] == 'memory' for t in after['tools'])
        assert insights.job_detail(conn, job)['job']['target_commit'] == 'sha'
        with pytest.raises(ValueError, match='unknown registered'):
            insights.agent_detail(ROOT, conn, '../_base')


def test_capability_excludes_outages_and_reports_comparable_baseline(tmp_path):
    repo(tmp_path)
    with connect() as conn:
        for spec, result, grader, detail in [('small', 'pass', 'check', 'passed'),
                                              ('large', 'fail', 'check', 'assertion failed'),
                                              ('small', 'error', 'kanban', 'Error code: 502')]:
            job = ledger.start_job(conn, 'demo', 'sha', 'implement', 'v', model_choice={'spec': spec})
            ledger.finish_job(conn, job, result, grader, detail)
        rows = {r['model']: r for r in metrics.capability(conn, 'demo')}
        assert rows['small']['passed'] == rows['small']['work'] == 1
        assert rows['small']['infra'] == 1
        assert rows['small']['baseline'] == {'passed': 1, 'work': 2}
        assert rows['large']['passed'] == 0 and rows['large']['work'] == 1


def test_snapshot_includes_disabled_targets_without_reading_checkout(tmp_path):
    path = repo(tmp_path)
    scout._save(scout.directory('demo') / 'review-status.json',
                {'phase': 'scanning', 'updated_at': '2026-11-03T00:00:00+00:00'})
    (path / 'app.py').unlink()
    # Even an inaccessible checkout can be inspected from its saved Team records.
    path.rename(tmp_path / 'moved')
    with connect() as conn:
        snapshot = insights.snapshot(conn)
        assert snapshot['targets'] == ['demo']
        assert snapshot['projects'][0]['enabled'] is False
        assert snapshot['projects'][0]['scout_report'] is None
        assert snapshot['projects'][0]['review_status']['phase'] == 'scanning'
    assert targets.require('demo')['enabled'] is False


def test_preview_changes_only_disposable_state_and_refuses_execution(tmp_path):
    from team.app import edit
    from team.app.preview import snapshot_state

    repo(tmp_path)
    with connect() as conn:
        original = insights.save_idea(conn, {'target': 'demo', 'title': 'Original'})
    original_state = ledger.state_dir()
    with snapshot_state() as preview:
        assert ledger.state_dir() == preview and preview != original_state
        app = server.App(ROOT, chat=object(), preview=True)
        app.post('/api/ideas', {'target': 'demo', 'title': 'Disposable'})
        with pytest.raises(edit.EditError, match='Preview'):
            app.post('/api/chat', {'text': 'Do work'})
        with pytest.raises(edit.EditError, match='Preview'):
            app.post('/api/gateway/restart', {'service': 'litellm'})
        assert len(app.get('/api/insights', {})['ideas']) == 2
    assert ledger.state_dir() == original_state
    with connect() as conn:
        rows = insights.snapshot(conn)['ideas']
        assert len(rows) == 1 and rows[0]['id'] == original['id']


def test_gateway_attempts_require_session_link_not_just_overlapping_time(tmp_path, team_state):
    import sqlite3

    repo(tmp_path)
    with connect() as conn:
        job = ledger.start_job(conn, 'demo', 'sha', 'implement', 'v')
        ledger.record_call(conn, job, 'gateway', 'tier-medium', 'implement', 10, 2, 1, True,
                           gateway_session_id='owned-session')
        path = team_state / 'litellm' / 'usage.db'
        path.parent.mkdir()
        with sqlite3.connect(path) as gateway:
            gateway.execute('CREATE TABLE calls(id INTEGER PRIMARY KEY,ts TEXT,session TEXT,model TEXT,'
                            'deployment TEXT,ok INTEGER,rate_limited INTEGER,error TEXT)')
            for session, model in [('unrelated', 'other'), ('owned-session', 'actual')]:
                gateway.execute('INSERT INTO calls(ts,session,model,deployment,ok,rate_limited) VALUES(?,?,?,?,?,?)',
                                (ledger._now(), session, model, 'deployment', 1, 0))
        detail = insights.job_detail(conn, job)
        assert [r['model'] for r in detail['gateway_attempts']['records']] == ['actual']
