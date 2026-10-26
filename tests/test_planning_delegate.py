"""Capability, scope propagation, timeout and native lifecycle integration."""
import contextvars
import json
import subprocess
import sys
import threading
from types import ModuleType, SimpleNamespace

import pytest

from team import planning_delegate as helpers


@pytest.fixture
def fake_native(monkeypatch):
    monkeypatch.setenv('TEAM_INFERENCE_URL', 'http://127.0.0.1:12345/v1')
    monkeypatch.setenv('TEAM_INFERENCE_TOKEN', 'local-token')
    parent = SimpleNamespace(provider='custom', base_url='http://127.0.0.1:12345/v1', api_key='local-token',
                             model='opus', enabled_toolsets=['team_planning_read', 'team_boss', 'terminal'],
                             _active_children=[], _active_children_lock=threading.RLock())
    child = SimpleNamespace(valid_tool_names=set(helpers.READ_TOOLS), skip_context_files=True,
                            session_id='child-session', closed=False, interrupted=False)
    child.close = lambda: setattr(child, 'closed', True)
    child.interrupt = lambda *args, **kwargs: setattr(child, 'interrupted', True)
    native = SimpleNamespace(_get_worktree_isolation=lambda: False, _get_max_spawn_depth=lambda: 3)
    calls = []
    def build(native_module, **kwargs):
        calls.append(kwargs)
        parent._active_children.append(child)
        return child
    monkeypatch.setattr(helpers, '_build_native_child', build)
    native._run_child_lifecycle = lambda *args: {'status':'completed','summary':'findings'}
    tools = ModuleType('tools')
    tools.delegate_tool = native
    monkeypatch.setitem(sys.modules, 'tools', tools)
    monkeypatch.setattr(helpers, '_SLOTS', threading.BoundedSemaphore(2))
    return parent, child, native, calls


def test_narrows_capabilities_and_copies_scope_without_mutating_parent(fake_native):
    parent, child, native, calls = fake_native
    scope = contextvars.ContextVar('plan_scope', default=None)
    token = scope.set('bound-plan')
    def lifecycle(index, question, actual_child, view):
        assert scope.get() == 'bound-plan'
        assert actual_child.skip_background_review
        assert actual_child.run_budget_seconds == 120
        assert actual_child._fallback_chain == []
        assert view.enabled_toolsets == ['team_planning_read']
        assert view._memory_manager is None
        return {'status':'completed','summary':'Bound findings'}
    native._run_child_lifecycle = lifecycle
    try:
        answer = helpers.run_helper(parent, 'Inspect pricing tests only', 'explorer')
    finally:
        scope.reset(token)
    assert answer['summary'] == 'Bound findings'
    assert parent.enabled_toolsets == ['team_planning_read','team_boss','terminal']
    assert calls[0]['override_provider'] == 'custom'
    assert calls[0]['override_base_url'] == parent.base_url
    assert calls[0]['override_api_key'] == parent.api_key
    assert calls[0]['max_iterations'] == 8
    assert calls[0]['parent_agent'] is not parent


def test_rejects_unexpected_tools_before_any_inference(fake_native):
    parent, child, native, _ = fake_native
    child.valid_tool_names.add('terminal')
    native._run_child_lifecycle = lambda *a: pytest.fail('must not execute')
    with pytest.raises(ValueError, match='unexpected tools'):
        helpers.run_helper(parent, 'Inspect', 'critic')
    assert child.closed
    assert child not in parent._active_children


def test_refuses_other_provider_worktree_mutation_and_large_budgets(fake_native):
    parent, _, native, calls = fake_native
    parent.provider = 'openrouter'
    with pytest.raises(ValueError, match='subscription gateway'):
        helpers.run_helper(parent, 'Inspect', 'explorer')
    parent.provider = 'custom'
    native._get_worktree_isolation = lambda: True
    with pytest.raises(ValueError, match='worktree_isolation'):
        helpers.run_helper(parent, 'Inspect', 'explorer')
    with pytest.raises(ValueError, match='max_iterations'):
        helpers.run_helper(parent, 'Inspect', 'explorer', max_iterations=1000)
    assert not calls


def test_timeout_retains_slot_until_native_lifecycle_exits(fake_native, monkeypatch):
    parent, child, native, _ = fake_native
    monkeypatch.setattr(helpers, '_SLOTS', threading.BoundedSemaphore(1))
    release = threading.Event()
    finished = threading.Event()
    def lifecycle(*args):
        release.wait(2)
        finished.set()
        return {'status':'completed','summary':'late'}
    native._run_child_lifecycle = lifecycle
    try:
        answer = helpers.run_helper(parent, 'Inspect', 'verifier', timeout_s=0.02)
        assert answer['status'] == 'timeout'
        assert child.interrupted
        assert helpers.run_helper(parent, 'Another', 'explorer')['status'] == 'busy'
    finally:
        release.set()
        assert finished.wait(2)


def test_real_native_child_uses_only_bound_reads_and_no_project_context(tmp_path, monkeypatch):
    from team import hermes_runtime as runtime
    from team.subscription_gateway import serve
    monkeypatch.setenv('TEAM_STATE', str(tmp_path))
    home = runtime.prepare_profile('boss')
    (tmp_path/'AGENTS.md').write_text('PROJECT_CONTEXT_MUST_NOT_LEAK_INTO_HELPER')
    seen = []
    requested = False
    parent_requested = False
    def infer(cwd, system, messages, schema, **kwargs):
        nonlocal requested, parent_requested
        tools = json.loads(system.rsplit('\n', 1)[-1])['tools']
        names = {t['function']['name'] for t in tools}
        assert 'PROJECT_CONTEXT_MUST_NOT_LEAK_INTO_HELPER' not in system
        if not names:
            return {'content':'title','tool_calls':[]}, {}
        if 'You are a read-only Team planning explorer.' not in system:
            if parent_requested:
                return {'content':'Parent complete.','tool_calls':[]}, {}
            parent_requested = True
            name, args = 'planning_helper_probe', {}
            if name not in names:
                name, args = 'tool_call', {'name':name,'arguments':args}
            return {'content':'','tool_calls':[{'name':name,'arguments':json.dumps(args)}]}, {}
        seen.append(names)
        assert not names & {'terminal','write_file','skill_manage','memory','delegate_task','kanban_create'}
        if not requested:
            requested = True
            name, args = 'planning_read', {'plan_id':'bound-plan'}
            if name not in names:
                name, args = 'tool_call', {'name':name,'arguments':args}
            return {'content':'','tool_calls':[{'name':name,'arguments':json.dumps(args)}]}, {}
        results = [m for m in messages if m.get('role') == 'tool']
        assert any('bound-plan' in str(m.get('content')) for m in results)
        return {'content':'Scoped findings verified.','tool_calls':[]}, {}
    script = r'''
import contextvars,json,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1]);sys.path.insert(0,sys.argv[2])
from tools.registry import registry
from toolsets import create_custom_toolset
from run_agent import AIAgent
from team.planning_delegate import run_helper
import os
scope=contextvars.ContextVar('test_bound_plan',default=None)
registry.register(name='planning_read',toolset='team_planning_read',schema={'name':'planning_read','description':'Read bound plan','parameters':{'type':'object','properties':{'plan_id':{'type':'string'}},'required':['plan_id']}},handler=lambda args,**kw: json.dumps({'scope':scope.get(),'requested':args['plan_id']}))
create_custom_toolset('team_planning_read','Bound planning reads',tools=['planning_read'])
holder={}
def probe(args,**kwargs):
 holder['result']=run_helper(parent,'Inspect only bound-plan using planning_read.','explorer',allowed_tools=['planning_read'],timeout_s=45)
 return json.dumps(holder['result'])
registry.register(name='planning_helper_probe',toolset='team_plan_parent',schema={'name':'planning_helper_probe','description':'Launch bounded helper','parameters':{'type':'object','properties':{}}},handler=probe)
create_custom_toolset('team_plan_parent','Parent helper launch',tools=['planning_helper_probe'])
from hermes_state import SessionDB
db=SessionDB()
parent=AIAgent(provider='custom',api_mode='chat_completions',model='opus',base_url=os.environ['TEAM_INFERENCE_URL'],api_key=os.environ['TEAM_INFERENCE_TOKEN'],enabled_toolsets=['team_planning_read','team_plan_parent'],quiet_mode=True,skip_context_files=True,skip_memory=True,skip_background_review=True,session_id='test-planning-parent',session_db=db)
token=scope.set('bound-plan')
# Simulate the ambient cwd installed by Hermes normal tool dispatch. The
# adapter must neither unset it nor read project instructions through it.
os.environ['TERMINAL_CWD']=os.getcwd()
import agent.prompt_builder as pb
import run_agent
original=pb.build_context_files_prompt
context_reads=[]
def forbidden_context(*args,**kwargs):
 context_reads.append((args,kwargs))
 raise AssertionError('Unscoped project context read')
pb.build_context_files_prompt=forbidden_context
run_agent.build_context_files_prompt=forbidden_context
try:
 parent.run_conversation('Use planning_helper_probe to inspect the bound plan.')
 result=holder['result']
 assert db.get_session(result['session_id'])['parent_session_id']=='test-planning-parent'
 assert os.environ['TERMINAL_CWD']==os.getcwd()
 assert not context_reads, context_reads
 print('RESULT:'+json.dumps(result))
finally:
 scope.reset(token);parent.close()
'''
    with serve(runtime.ROOT, request=infer) as gateway:
        proc = subprocess.run([str(runtime.python_executable()), '-c', script, str(runtime.ROOT), str(runtime.FORK)],
                              cwd=tmp_path, env=runtime.runtime_env(home,gateway), capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr+proc.stdout
    line = next(line for line in proc.stdout.splitlines() if line.startswith('RESULT:'))
    result = json.loads(line.removeprefix('RESULT:'))
    assert result['status'] == 'completed', result
    assert 'Scoped findings verified' in result['summary']
    assert requested and parent_requested and seen
