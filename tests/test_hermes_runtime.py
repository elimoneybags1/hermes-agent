"""Exercise the actual forked Hermes loop with a local deterministic model endpoint."""
import json
import os
import subprocess
from pathlib import Path

import pytest

from team import hermes_runtime as runtime
from team.subscription_gateway import serve


def test_profile_seeds_full_upstream_and_preserves_learning(tmp_path, monkeypatch):
    monkeypatch.setenv('TEAM_STATE', str(tmp_path))
    home = runtime.prepare_profile('implement')
    assert (home / 'SOUL.md').read_text() == (runtime.FORK / 'SOUL.md').read_text()
    upstream = {p.relative_to(runtime.FORK / 'skills') for p in (runtime.FORK / 'skills').rglob('SKILL.md')}
    assert upstream and all((home / 'skills' / p).exists() for p in upstream)
    learned = home / 'skills' / 'learned-test' / 'SKILL.md'
    learned.parent.mkdir()
    learned.write_text('Keep this learned procedure')
    (home / 'SOUL.md').write_text('Personalized role identity')
    runtime.prepare_profile('implement')
    assert learned.read_text() == 'Keep this learned procedure'
    assert (home / 'SOUL.md').read_text() == 'Personalized role identity'
    assert runtime.prepare_profile('boss') != home


def test_native_loop_executes_memory_and_restores_it(tmp_path, monkeypatch):
    monkeypatch.setenv('TEAM_STATE', str(tmp_path))
    home = runtime.prepare_profile('boss')
    seen_tools, seen_prompts = set(), []
    used = False
    listed = False
    def infer(cwd, system, messages, schema, **kwargs):
        nonlocal used, listed
        details = json.loads(system.rsplit('\n', 1)[-1])
        names = {t['function']['name'] for t in details['tools']}
        seen_tools.update(names)
        seen_prompts.append(system)
        if names and not listed:
            listed = True
            return {'content': '', 'tool_calls': [{'name': 'tool_call', 'arguments': json.dumps({'name':'skills_list','arguments':{}})}]}, {}
        if names and not used:
            results = [m for m in messages if m.get('role') == 'tool']
            skill_result = json.loads(results[-1]['content'])
            assert skill_result.get('skills'), skill_result
            used = True
            return {'content': '', 'tool_calls': [{'name': 'memory', 'arguments': json.dumps({'action': 'add', 'target': 'memory', 'content': 'Runtime verification marker: cobalt compass.'})}]}, {'input_tokens': 2, 'output_tokens': 2}
        return {'content': 'native-ok', 'tool_calls': []}, {'input_tokens': 2, 'output_tokens': 2}
    def launch(gateway, suffix):
        path = tmp_path / f'payload-{suffix}.json'
        out = tmp_path / f'result-{suffix}.json'
        path.write_text(json.dumps(dict(role='boss',root=str(runtime.ROOT),cwd=str(runtime.ROOT),prompt='Record the supplied verification marker and respond.',model='opus',max_turns=5,timeout_s=50,result_file=str(out))))
        proc = subprocess.run([str(runtime.python_executable()), str(runtime.ROOT/'team/hermes_native.py'), str(path)],
                              cwd=runtime.ROOT, env=runtime.runtime_env(home, gateway),capture_output=True,text=True,timeout=70)
        assert proc.returncode == 0, proc.stderr + proc.stdout
        assert json.loads(out.read_text())['ok']
    with serve(runtime.ROOT, request=infer) as gateway:
        launch(gateway, 'first')
    assert {'memory','tool_call','tool_describe','kanban_show'} <= seen_tools
    assert 'session_search' in seen_tools or 'tool_search' in seen_tools
    assert 'terminal' not in seen_tools
    assert 'cobalt compass' in (home / 'memories/MEMORY.md').read_text()
    assert (home/'state.db').exists()
    assert all(s.count('## Lifecycle') <= 1 for s in seen_prompts)
    assert all('<available_skills>' not in s for s in seen_prompts)
    seen_prompts.clear()
    with serve(runtime.ROOT, request=infer) as gateway:
        launch(gateway, 'second')
    assert any('cobalt compass' in s for s in seen_prompts)


def test_worker_route_uses_upstream_custom_root_without_inheriting_subscription(tmp_path, monkeypatch):
    worker_root=tmp_path/'free'
    monkeypatch.setenv('HERMES_HOME',str(tmp_path/'subscription/profiles/boss'))
    monkeypatch.setenv('TEAM_WORKER_HERMES_HOME',str(worker_root))
    monkeypatch.setenv('TEAM_INFERENCE_URL','http://127.0.0.1:1/v1')
    monkeypatch.setenv('TEAM_INFERENCE_TOKEN','subscription-token')
    monkeypatch.setenv('OPENAI_BASE_URL','http://127.0.0.1:1/v1')
    monkeypatch.setenv('OPENAI_API_KEY','subscription-token')
    env=runtime.worker_environment()
    assert env['HERMES_HOME']==str(worker_root)
    assert 'OPENAI_API_KEY' not in env and 'TEAM_INFERENCE_URL' not in env
    from team.profiles import _hermes_profile_dir
    assert _hermes_profile_dir('team-implement')==worker_root/'profiles/team-implement'


def test_native_profile_root_honors_isolated_profile(tmp_path, monkeypatch):
    monkeypatch.delenv('TEAM_WORKER_HERMES_HOME',raising=False)
    monkeypatch.setenv('HERMES_HOME',str(tmp_path/'hermes/profiles/boss'))
    assert runtime.native_profile_root()==tmp_path/'hermes'


def test_explicit_native_tool_discovery_setting_is_preserved(tmp_path, monkeypatch):
    import yaml
    monkeypatch.setenv('TEAM_STATE',str(tmp_path))
    home=runtime.prepare_profile('boss')
    path=home/'config.yaml'
    config=yaml.safe_load(path.read_text())
    config['tools']['tool_search']=False
    path.write_text(yaml.safe_dump(config))
    runtime.prepare_profile('boss')
    assert yaml.safe_load(path.read_text())['tools']['tool_search'] is False
