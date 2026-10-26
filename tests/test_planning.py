import json
from pathlib import Path

import pytest

from team import planning, ledger, targets
from team.hermes_runtime import ROOT


@pytest.fixture
def setup(tmp_path,monkeypatch):
    monkeypatch.setenv('TEAM_STATE',str(tmp_path/'state'))
    repo=tmp_path/'project';(repo/'pricing').mkdir(parents=True);(repo/'auth').mkdir()
    (repo/'AGENTS.md').write_text('Project instructions: inspect only relevant code.')
    (repo/'pricing/rules.py').write_text('def discount(total):\n    return total\n')
    (repo/'pricing/test_rules.py').write_text('assert discount(5) == 5\n')
    (repo/'auth/login.py').write_text('PRIVATE_UNRELATED_SOURCE')
    (repo/'.env').write_text('SECRET=never-read')
    target=targets.targets_dir()/'demo/target.json';target.parent.mkdir(parents=True)
    target.write_text(json.dumps({'name':'demo','path':str(repo),'check':'original-default'}))
    planner=planning.Planner(ROOT)
    result=planner.start('demo','Support checkout discounts')
    return planner,result['plan_id'],repo


def ready(setup):
    p,pid,repo=setup
    found=p.locate(pid,'*rules*')
    p.scope(pid,['pricing'],'Relevant pricing subsystem',[found['evidence_id']])
    p.read(pid,'pricing/rules.py')
    p.read(pid,'pricing/test_rules.py')
    contract={'success':[{'id':'discount','behavior':'Discount is applied once','verification':'run targeted and checkout tests'}],
        'change_scope':['pricing'],'verification_scope':['pricing tests','checkout integration'],
        'exclusions':['Authentication changes'],'stop_conditions':['Ask if pricing policy is ambiguous'],'decisions':[],
        'tasks':[{'id':'implement','title':'Add discounts','body':'Apply the agreed discount once.', 'role':'implement',
                  'files':['pricing/rules.py','pricing/test_rules.py'],'criteria':['discount'],'check':'pytest pricing','depends_on':[]}]}
    assert p.update(pid,contract)['ready']
    return contract


def test_start_is_metadata_and_instructions_only(setup):
    p,pid,repo=setup
    data=p.load(pid)
    assert data['files_read']==['AGENTS.md']
    assert data['inspection_scope']==[]
    with pytest.raises(ValueError,match='Outside inspection'):
        p.read(pid,'pricing/rules.py')
    with pytest.raises(ValueError): p.scope(pid,['.'],'read everything',[])
    with pytest.raises(ValueError): p.scope(pid,['../'],'escape',[])
    with pytest.raises(ValueError): p.read(pid,'.env')
    outside=repo.parent/'outside';outside.write_text('OUTSIDE')
    (repo/'pricing/link.py').symlink_to(outside)
    with pytest.raises(ValueError,match='escapes'):
        p.scope(pid,['pricing/link.py'],'symlink',[])


def test_scope_expansion_and_search_are_bounded(setup):
    p,pid,repo=setup
    p.scope(pid,['pricing'],'Pricing implementation',[])
    results=p.search(pid,'discount')
    assert results['matches']
    assert all(m['path'].startswith('pricing/') for m in results['matches'])
    assert 'auth/login.py' not in p.load(pid)['files_read']
    with pytest.raises(ValueError,match='requires'):
        p.scope(pid,['auth'],'maybe useful',[])
    with pytest.raises(ValueError,match='inspected'):
        p.scope(pid,['auth'],'maybe useful',['made-up'])
    p.scope(pid,['auth'],'Observed dependency needs validation',[results['evidence_id']])
    assert p.load(pid)['expansions'][-1]['reason']=='Observed dependency needs validation'


def test_helper_scope_cannot_expand_or_read_another_plan(setup):
    p,pid,repo=setup
    p.scope(pid,['pricing','auth'],'Both selected by boss',[])
    other=p.start('demo','Another task')['plan_id']
    t=planning.HELPER_PLAN.set(pid);s=planning.HELPER_SCOPE.set(('pricing',))
    try:
        assert p.read(pid,'pricing/rules.py')['file']['text']
        with pytest.raises(ValueError,match='Outside inspection'): p.read(pid,'auth/login.py')
        with pytest.raises(ValueError,match='assigned plan'): p.view(other)
        with pytest.raises(ValueError,match='cannot mutate'): p.scope(pid,['auth'],'expand',[])
        assert all(name.startswith('pricing/') for name in p.locate(pid,'*.py')['paths'])
    finally:
        planning.HELPER_SCOPE.reset(s);planning.HELPER_PLAN.reset(t)


def test_shared_content_budget_stops_reads(setup,monkeypatch):
    p,pid,repo=setup
    p.scope(pid,['pricing'],'Relevant',[])
    monkeypatch.setattr(planning,'MAX_BYTES',p.load(pid)['bytes_read']+10)
    result=p.read(pid,'pricing/rules.py')['file']
    assert result['bytes']==10 and result['next_offset']==10
    with pytest.raises(ValueError,match='budget exhausted'):p.read(pid,'pricing/test_rules.py')


def test_readiness_blocks_unresolved_overlapping_and_stale_plans(setup):
    p,pid,repo=setup
    c=ready(setup)
    c['decisions']=[{'question':'Which discount policy?','answer':''}]
    assert 'Unresolved decisions' in p.update(pid,c)['readiness_errors']
    c['decisions']=[]
    c['tasks'].append({**c['tasks'][0],'id':'second'})
    assert any('Overlapping' in x for x in p.update(pid,c)['readiness_errors'])
    c['tasks'].pop();p.update(pid,c)
    (repo/'pricing/rules.py').write_text('changed after investigation')
    assert any('source changed' in x for x in p.view(pid)['readiness_errors'])
    p.read(pid,'pricing/rules.py')
    assert p.view(pid)['ready']


def test_dispatch_is_gated_idempotent_and_supplies_scoped_brief(setup,monkeypatch):
    from team import hermes_boss
    p,pid,repo=setup
    with pytest.raises(ValueError,match='not ready'): p.dispatch(pid,'implement')
    ready(setup)
    calls=[]
    def create(root,name,args):
        calls.append(args)
        return json.dumps({'goal_id':12,'task_id':'task-12'})
    monkeypatch.setattr(hermes_boss,'execute',create)
    assert p.dispatch(pid,'implement')['goal_id']==12
    assert p.dispatch(pid,'implement')['already_dispatched']
    assert len(calls)==1
    assert 'Team scoped assignment' in calls[0]['body']
    assert 'verification_scope' in calls[0]['body'] and 'inspection_scope' in calls[0]['body']
    assert targets.get('demo')['check']=='original-default'
    with pytest.raises(ValueError,match='dispatched'): p.update(pid,{})


def test_plan_only_turn_cannot_dispatch(setup):
    p,pid,repo=setup;ready(setup)
    token=planning.PLAN_ONLY.set(True)
    try:
        with pytest.raises(ValueError,match='Planning-only'):p.dispatch(pid,'implement')
    finally:planning.PLAN_ONLY.reset(token)


def test_unlanded_dependency_and_unproven_completion_are_blocked(setup):
    p,pid,repo=setup;c=ready(setup)
    c['tasks'].append({**c['tasks'][0],'id':'second','files':['pricing/new.py'],'depends_on':['implement']})
    assert p.update(pid,c)['ready']
    with pytest.raises(ValueError,match='must pass and land'):p.dispatch(pid,'second')
    evidence=p.read(pid,'pricing/rules.py')['file']['evidence_id']
    with pytest.raises(ValueError,match='passed and landed'):
        p.assess(pid,[{'id':'discount','status':'met','reason':'I think so','evidence_ids':[evidence],'goal_ids':[123]}],'All done')
    result=p.assess(pid,[{'id':'discount','status':'unknown','reason':'No execution evidence','evidence_ids':[],'goal_ids':[]}],'Not verified')
    assert result['criteria'][0]['status']=='unknown'


def test_clarification_options_are_rendered(monkeypatch,capsys):
    from team.hermes_native import ask_clarification
    monkeypatch.setattr('builtins.input',lambda _: '2')
    assert ask_clarification('Which subsystem?',['Pricing','Checkout'])=='Checkout'
    assert '2. Checkout' in capsys.readouterr().out


def test_native_plan_turn_uses_scoped_tools_and_blocks_dispatch(setup,tmp_path):
    import subprocess
    from team import hermes_runtime as runtime
    from team.subscription_gateway import serve
    p,pid,repo=setup
    contract=ready(setup)
    stage=0
    def infer(cwd,system,messages,schema,**kwargs):
        nonlocal stage
        tools=json.loads(system.rsplit('\n',1)[-1])['tools']
        names={t['function']['name'] for t in tools}
        if not names:return {'content':'title','tool_calls':[]},{}
        assert 'kanban_create' not in names
        if stage==0:
            assert 'Team integration overrides' in json.dumps(messages)
            stage+=1
            args={'plan_id':pid,'path':'pricing/rules.py'}
            name='planning_read'
        elif stage==1:
            result=json.loads([m for m in messages if m.get('role')=='tool'][-1]['content'])
            assert result['file']['text'].startswith('def discount')
            stage+=1
            name='planning_dispatch';args={'plan_id':pid,'task_id':'implement'}
        else:
            result=json.loads([m for m in messages if m.get('role')=='tool'][-1]['content'])
            assert 'Planning-only' in result['error']
            return {'content':'Saved scoped plan; no work dispatched.','tool_calls':[]},{}
        if name not in names:
            name,args='tool_call',{'name':name,'arguments':args}
        return {'content':'','tool_calls':[{'name':name,'arguments':json.dumps(args)}]},{}
    home=runtime.prepare_profile('boss')
    payload=tmp_path/'native-payload.json'
    payload.write_text(json.dumps(dict(role='boss',root=str(ROOT),cwd=str(ROOT),prompt='/plan Complete the scoped plan only.',
        model='opus',max_turns=6,timeout_s=50)))
    with serve(ROOT,request=infer) as gateway:
        proc=subprocess.run([str(runtime.python_executable()),str(ROOT/'team/hermes_native.py'),str(payload)],
            cwd=ROOT,env=runtime.runtime_env(home,gateway),capture_output=True,text=True,timeout=70)
    assert proc.returncode==0,proc.stdout+proc.stderr
    assert stage==2
    assert p.load(pid)['task_goals']=={}


def test_in_repo_symlinks_cannot_bypass_scope_or_secret_filter(setup):
    p,pid,repo=setup
    p.scope(pid,['pricing'],'Relevant subsystem',[])
    (repo/'pricing/secret.py').symlink_to(repo/'.env')
    (repo/'pricing/unrelated.py').symlink_to(repo/'auth/login.py')
    (repo/'pricing/linked_dir').symlink_to(repo/'auth',target_is_directory=True)
    for path in ('pricing/secret.py','pricing/unrelated.py','pricing/linked_dir/login.py'):
        with pytest.raises(ValueError,match='Symlink'):
            p.read(pid,path)


def test_existing_owned_files_need_scoped_inspection_and_new_files_need_evidence(setup):
    p,pid,repo=setup;c=ready(setup)
    c['change_scope']=['auth']
    c['tasks'][0]['files']=['auth/login.py']
    errors=p.update(pid,c)['readiness_errors']
    assert any('outside inspection scope' in e for e in errors)
    assert any('has not been inspected' in e for e in errors)
    c['change_scope']=['new_adapter.py'];c['tasks'][0]['files']=['new_adapter.py']
    assert any('scope_reason' in e for e in p.update(pid,c)['readiness_errors'])
    c['tasks'][0].update(scope_reason='New adapter for inspected pricing interface',evidence=['file:pricing/rules.py@0'])
    assert p.update(pid,c)['ready']


def test_helper_view_does_not_recheck_files_outside_its_scope(setup,monkeypatch):
    p,pid,repo=setup;c=ready(setup)
    p.scope(pid,['auth'],'Shared interface evidence',['file:pricing/rules.py@0'])
    p.read(pid,'auth/login.py')
    original=p.resolve
    def checked(data,path):
        assert not path.startswith('auth/'), 'helper readiness touched unrelated source'
        return original(data,path)
    monkeypatch.setattr(p,'resolve',checked)
    t=planning.HELPER_PLAN.set(pid);s=planning.HELPER_SCOPE.set(('pricing',))
    try:
        view=p.view(pid)
        assert not view['freshness_checked']
        assert not any(e.get('path')=='auth/login.py' for e in view['evidence'])
    finally:
        planning.HELPER_SCOPE.reset(s);planning.HELPER_PLAN.reset(t)


def test_dispatch_recovery_matches_complete_task_id(setup,monkeypatch):
    from team import hermes_boss
    p,pid,repo=setup;c=ready(setup)
    c['tasks'][0]['id']='api'
    p.update(pid,c)
    with ledger.connect() as conn:
        ledger.init(conn)
        ledger.create_goal(conn,'demo',f'Other task\nTeam plan task: {pid}:api-tests\nDetails','implement','branch','worktree','check')
    calls=[]
    def create(root,name,args):
        calls.append(args)
        return json.dumps({'goal_id':900,'task_id':'new-api'})
    monkeypatch.setattr(hermes_boss,'execute',create)
    assert p.dispatch(pid,'api')['goal_id']==900
    assert len(calls)==1


def test_worker_receives_only_its_relevant_inspection_roots(setup,monkeypatch):
    from team import hermes_boss
    p,pid,repo=setup;ready(setup)
    p.scope(pid,['auth'],'Other part of overall investigation',['file:pricing/rules.py@0'])
    calls=[]
    monkeypatch.setattr(hermes_boss,'execute',lambda root,name,args: calls.append(args) or json.dumps({'goal_id':444,'task_id':'t-444'}))
    p.dispatch(pid,'implement')
    body=calls[0]['body']
    contract=json.JSONDecoder().raw_decode(body.split('Team scoped assignment:\n',1)[1])[0]
    assert contract['inspection_scope']==['pricing']
    assert 'auth' not in contract['change_scope']


def test_task_capabilities_are_validated_and_forwarded(setup, monkeypatch):
    from team import hermes_boss
    p, pid, repo = setup
    contract = ready(setup)
    task = contract['tasks'][0]
    task['recommended_toolsets'] = ['file', 'terminal']
    task['required_skills'] = ['roles/implement/skills/commit-before-done/SKILL.md']
    assert p.update(pid, contract)['ready']
    calls = []
    monkeypatch.setattr(hermes_boss, 'execute', lambda root, name, args: calls.append(args) or json.dumps({'goal_id': 445, 'task_id': 't-445'}))
    p.dispatch(pid, 'implement')
    packet = json.JSONDecoder().raw_decode(calls[0]['body'].split('Team scoped assignment:\n', 1)[1])[0]
    assert packet['recommended_toolsets'] == ['file', 'terminal']
    assert packet['required_skills'] == task['required_skills']


@pytest.mark.parametrize('field,value', [
    ('recommended_toolsets', ['imaginary_tool']),
    ('required_skills', ['roles/boss/skills/scoped-planning/SKILL.md']),
    ('required_skills', ['vendor/hermes-agent/skills/nonexistent/SKILL.md']),
])
def test_invalid_task_capabilities_block_dispatch(setup, field, value):
    p, pid, repo = setup
    contract = ready(setup)
    contract['tasks'][0][field] = value
    assert not p.update(pid, contract)['ready']


def test_search_finds_late_ui_section_without_returning_whole_file(setup):
    p, pid, repo = setup
    source = 'padding\n' * 4000 + 'const TABS = ["Settings"];\n'
    (repo / 'pricing/large.js').write_text(source)
    p.scope(pid, ['pricing/large.js'], 'UI section', [])
    result = p.search(pid, 'const TABS')
    assert result['matches'][0]['offset'] >= planning.PAGE_BYTES
    assert result['matches'][0]['line'] == 4001
    assert len(json.dumps(result)) < 2000
    assert not result['coverage_notes']


def test_repeated_source_reads_do_not_repeat_project_instructions(setup):
    p, pid, repo = setup
    p.scope(pid, ['pricing'], 'Relevant code', [])
    result = p.read(pid, 'pricing/rules.py')
    assert result['instructions'][0]['already_supplied']
    assert 'text' not in result['instructions'][0]
    # A fresh conversation must get governing instructions again.
    fresh = planning.Planner(p.root)
    result = fresh.read(pid, 'pricing/rules.py')
    assert 'Project instructions' in result['instructions'][0]['text']


def test_search_finds_text_across_page_boundary(setup):
    p, pid, repo = setup
    (repo / 'pricing/boundary.txt').write_text('x\n' * (planning.PAGE_BYTES // 2 - 2) + 'abcNEEDLEdef\n')
    p.scope(pid, ['pricing/boundary.txt'], 'Boundary fixture', [])
    assert len(p.search(pid, 'NEEDLE')['matches']) == 1
