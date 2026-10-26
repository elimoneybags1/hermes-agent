import json
import sqlite3
import subprocess
from pathlib import Path

import pytest

from team import ledger, optimizer, hermes_runtime as runtime
from team.subscription_gateway import serve


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setenv('TEAM_STATE', str(tmp_path))
    with ledger.connect() as conn:
        ledger.init(conn)
        for target, outcome in [('alpha','pass'), ('alpha','fail'), ('beta','pass')]:
            job = ledger.start_job(conn, target, 'base', 'implement', 'role-base')
            ledger.finish_job(conn, job, outcome, 'pytest', 'actual check output')
    return tmp_path


def test_sample_is_filtered_counted_and_read_only(state):
    before = ledger.db_path().read_bytes()
    e = optimizer.Evidence(runtime.ROOT)
    data = e.collect(target='alpha')['data']
    assert len(data['jobs']) == 2
    assert data['sample_outcomes'] == [dict(target='alpha',role='implement',attempts=2,finished=2,passed=1)]
    assert ledger.db_path().read_bytes() == before
    assert 'source:team/run.py' in data['sources']
    with pytest.raises(ValueError):
        e.collect(limit=100000)


def test_proposals_require_inspected_evidence_and_preserve_revisions(state):
    e = optimizer.Evidence(runtime.ROOT)
    fields = dict(subject='implement', scope='alpha test tasks',location='roles/implement/SOUL.md',
                  before='Current procedure',after='Proposed exact replacement',reason='Observed failure',
                  validation='Replay comparable tasks against unchanged baseline',uncertainty='Only two observations')
    with pytest.raises(ValueError):
        e.recommend(**fields,evidence_ids=['job:999'])
    item = e.collect(target='alpha')
    first = e.recommend(**fields,evidence_ids=[item['id']])
    assert first['status'] == 'proposed'
    second = e.recommend(**{**fields,'after':'Revised replacement'},evidence_ids=[item['id']],supersedes=first['id'])
    saved = {p['id']:p for p in optimizer.proposals()}
    assert saved[first['id']]['after'] == 'Proposed exact replacement'
    assert saved[first['id']]['status'] == 'superseded'
    assert saved[first['id']]['superseded_by'] == second['id']
    assert saved[second['id']]['supersedes'] == first['id']
    assert saved[first['id']]['evidence'][0]['data']['jobs']


def test_source_pagination_and_no_arbitrary_file_access(state, tmp_path):
    root = tmp_path/'repo'
    (root/'team').mkdir(parents=True)
    (root/'roles').mkdir()
    (root/'team/example.py').write_text('a'*6000+'b'*100)
    e = optimizer.Evidence(root)
    first = e.source('source:team/example.py')['data']
    second = e.source('source:team/example.py',first['next_offset'])['data']
    assert first['text'] + second['text'] == 'a'*6000+'b'*100
    with pytest.raises(ValueError):
        e.source('source:../../.env')


def test_native_optimizer_can_collect_and_propose_without_execution_tools(state):
    home = runtime.prepare_profile('optimizer')
    stage = 0
    def infer(cwd, system, messages, schema, **kwargs):
        nonlocal stage
        definitions = json.loads(system.rsplit('\n',1)[-1])['tools']
        names = {t['function']['name'] for t in definitions}
        if not names:
            return {'content':'title','tool_calls':[]}, {}
        assert not names & {'terminal','execute_code','write_file','kanban_create','kanban_comment','skill_manage','memory'}
        def call(name, args):
            if name not in names:
                assert 'tool_call' in names, names
                name, args = 'tool_call', {'name': name, 'arguments': args}
            return {'content':'','tool_calls':[{'name':name,'arguments':json.dumps(args)}]}, {}
        if stage == 0:
            stage += 1
            return call('optimizer_evidence', {'target':'alpha'})
        if stage == 1:
            stage += 1
            evidence = json.loads([m for m in messages if m.get('role')=='tool'][-1]['content'])
            proposal = dict(subject='implement',scope='alpha',location='roles/implement/SOUL.md',
                before='Existing behavior',after='Concrete proposed behavior',reason='Sample evidence',
                validation='Compare to baseline',uncertainty='Test fixture; not a real recommendation',evidence_ids=[evidence['id']])
            return call('optimizer_recommend', proposal)
        return {'content':'Proposal saved, no changes applied.','tool_calls':[]}, {}
    payload = state/'payload.json'
    payload.write_text(json.dumps(dict(role='optimizer',root=str(runtime.ROOT),cwd=str(runtime.ROOT),
        prompt=None,initial_prompt='Inspect evidence and propose a change.',model='opus',max_turns=6,timeout_s=45)))
    with serve(runtime.ROOT,request=infer) as gateway:
        p = subprocess.run([str(runtime.python_executable()),str(runtime.ROOT/'team/hermes_native.py'),str(payload)],
            env=runtime.runtime_env(home,gateway),cwd=runtime.ROOT,text=True,capture_output=True,input='/quit\n',timeout=60)
    assert p.returncode == 0, p.stdout+p.stderr
    assert stage == 2
    assert len(optimizer.proposals()) == 1
    assert optimizer.proposals()[0]['status'] == 'proposed'


def test_cli_routes_optimizer_and_list_without_inference(state, monkeypatch, capsys):
    from team import cli
    seen = []
    monkeypatch.setattr(optimizer, 'main', lambda root, argv: seen.append(argv) or 0)
    assert cli.main(['optimize', '--resume', 'session-123']) == 0
    assert seen == [['--resume', 'session-123']]


def test_session_read_is_paginated_without_migration(state):
    home = state/'hermes/profiles/implement'
    home.mkdir(parents=True)
    db = home/'state.db'
    with sqlite3.connect(db) as conn:
        conn.execute('CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT)')
        conn.executemany('INSERT INTO messages VALUES (?, ?, ?, ?)',
            [(i,'test-session','tool',f'result-{i}') for i in range(25)])
    before = db.read_bytes()
    e = optimizer.Evidence(runtime.ROOT)
    first = e.session('implement','subscription','test-session')['data']
    assert len(first['messages']) == 20 and first['next_offset'] == 20
    second = e.session('implement','subscription','test-session',20)['data']
    assert len(second['messages']) == 5 and second['next_offset'] is None
    assert db.read_bytes() == before
