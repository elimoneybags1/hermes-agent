"""Hermes reuse with explicit Team boundaries; native tests use isolated boards."""
import json
import subprocess
from pathlib import Path

import pytest

from team import hermes_boss, targets
from team.harness import Session

ROOT = Path(__file__).resolve().parents[1]


def test_boss_context_contains_derived_skill_and_full_schemas(tmp_path, monkeypatch):
    monkeypatch.setenv('TEAM_STATE', str(tmp_path / 'state'))
    session = Session(ROOT, ROOT, 'boss')
    assert 'Decision ownership' in session.system
    assert 'parents' in session.system
    assert 'kanban_show' in session.executor.allowed
    assert 'kanban_create' in session.executor.allowed
    assert 'team_roster' in session.executor.allowed
    assert 'kanban_complete' not in session.executor.allowed
    assert 'exec' not in session.executor.allowed
    assert session.model == 'opus'
    assert '48c0c3a873bc5adaf20c632b5b7630a4fac000b4' in session.system


def test_source_drift_refused(tmp_path):
    with pytest.raises(ValueError, match='source drift'):
        hermes_boss.verify_source(tmp_path)


def test_scope_and_schema_reject_before_handler(tmp_path, monkeypatch):
    monkeypatch.setenv('TEAM_STATE', str(tmp_path / 'state'))
    for args in ({}, {'board': '../escape'}, {'board': 'unknown'}, {'board': '..'}):
        with pytest.raises(ValueError):
            hermes_boss.validate('kanban_list', args)
    with pytest.raises(ValueError):
        hermes_boss.validate('kanban_comment', {'board': 'demo', 'task_id': 'x', 'body': 'hi', 'author': 'admin'})
    assert 'model' not in hermes_boss.schemas()['kanban_create']['parameters']['properties']


def test_env_cannot_target_foreign_board_or_impersonate_worker(tmp_path, monkeypatch):
    monkeypatch.setenv('TEAM_STATE', str(tmp_path))
    for key in ('HERMES_KANBAN_DB', 'HERMES_KANBAN_TASK', 'HERMES_KANBAN_BOARD', 'HERMES_SESSION_ID', 'PYTHONPATH'):
        monkeypatch.setenv(key, 'bad')
    env = hermes_boss.bridge_env()
    assert env['HERMES_KANBAN_HOME'] == str(tmp_path / 'kanban')
    assert env['HERMES_PROFILE'] == 'team-boss'
    assert not any(k in env for k in ('HERMES_KANBAN_DB', 'HERMES_KANBAN_TASK', 'HERMES_KANBAN_BOARD', 'HERMES_SESSION_ID', 'PYTHONPATH'))


def test_native_hermes_handlers_roundtrip_on_isolated_board(tmp_path, monkeypatch):
    source = hermes_boss.installed_source()
    from team.hermes_runtime import python_executable
    interpreter = python_executable()
    if not interpreter.exists():
        pytest.skip('installed Hermes needed for native integration test')
    hermes_boss.verify_source(source)
    monkeypatch.setenv('TEAM_STATE', str(tmp_path / 'state'))
    targets._write({'name': 'demo', 'path': str(tmp_path), 'commit': 'test'})
    setup = '''from hermes_cli import kanban_db as kb
kb.create_board('demo')
conn = kb.connect(board='demo')
task = kb.create_task(conn, title='Integration fixture', body='No model dispatch', assignee='fixture', initial_status='blocked')
print(task)
conn.close()
'''
    proc = subprocess.run([str(interpreter), '-c', setup], cwd=source,
                          env=hermes_boss.bridge_env(), capture_output=True, text=True, check=True)
    task_id = proc.stdout.strip()
    listing = json.loads(hermes_boss.execute(ROOT, 'kanban_list', {'board': 'demo'}))
    assert any(t['id'] == task_id for t in listing['tasks'])
    result = json.loads(hermes_boss.execute(ROOT, 'kanban_comment', {'board': 'demo', 'task_id': task_id, 'body': 'Integration evidence'}))
    assert result.get('comment_id') is not None
    shown = json.loads(hermes_boss.execute(ROOT, 'kanban_show', {'board': 'demo', 'task_id': task_id}))
    assert shown['task']['title'] == 'Integration fixture'
    assert any(c['author'] == 'team-boss' and c['body'] == 'Integration evidence' for c in shown['comments'])
    assert 'worker_context' in shown
