"""Team owns tools and state; the CLI owns only subscription model transport."""
import json
import subprocess
from pathlib import Path

import pytest

from team import claude_run, claude_transport, harness
from team.harness_tools import ToolExecutor


def action(kind='final', text='done', name='', **arguments):
    return dict(kind=kind, text=text, name=name, arguments=json.dumps(arguments))


class Model:
    def __init__(self, *actions):
        self.actions = iter(actions)
        self.calls = []

    def __call__(self, cmd, **kwargs):
        self.calls.append((cmd, kwargs))
        if cmd[:3] == ['claude', 'auth', 'status']:
            data = dict(loggedIn=True, authMethod='claude.ai', apiProvider='firstParty')
        elif cmd[0] == 'claude':
            data = dict(structured_output=next(self.actions), usage={'input_tokens': 2})
        else:
            return subprocess.run(cmd, **kwargs)
        return subprocess.CompletedProcess(cmd, 0, json.dumps(data), '')


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv('TEAM_STATE', str(tmp_path / 'state'))
    wt = tmp_path / 'worktree'
    wt.mkdir()
    return wt


def test_isolated_transport_and_real_file_tool(workspace):
    model = Model(action('tool', name='write', path='hello.txt', content='hello'),
                  action('tool', name='read', path='hello.txt'), action())
    result = claude_run.legacy_run(workspace, 'write hello', runner=model)
    assert result['ok']
    assert result['num_turns'] == 3
    assert result['usage']['input_tokens'] == 6
    assert (workspace / 'hello.txt').read_text() == 'hello'
    calls = [c for c in model.calls if c[0][1] == '-p']
    for cmd, kw in calls:
        assert cmd[cmd.index('--tools') + 1] == ''
        assert cmd[cmd.index('--setting-sources') + 1] == ''
        assert '--safe-mode' in cmd and '--strict-mcp-config' in cmd
        assert '--dangerously-skip-permissions' not in cmd
        assert '--plugin-dir' not in cmd and '--bare' not in cmd
        assert not (set(claude_transport.SCRUB) & set(kw['env']))
    assert 'hello' in calls[-1][1]['input']
    events = list((workspace.parent / 'state/harness/sessions').glob('*.jsonl'))
    assert len(events) == 1
    assert events[0].stat().st_mode & 0o777 == 0o600


def test_paid_auth_fails_before_model(workspace):
    calls = []
    def runner(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, json.dumps({'loggedIn': True, 'authMethod': 'api_key'}), '')
    result = claude_run.legacy_run(workspace, 'hello', runner=runner)
    assert not result['ok'] and 'subscription' in result['result']
    assert len(calls) == 1


def test_tools_denied_and_path_escape(workspace):
    outside = workspace.parent / 'outside'
    outside.write_text('private')
    (workspace / 'link').symlink_to(outside)
    executor = ToolExecutor(workspace, ['read', 'write'], allowed_files=['ok.txt'])
    for path in ['../outside', str(outside), 'link', '.git/config', '.env']:
        with pytest.raises(ValueError):
            executor.execute('read', {'path': path}, 1)
    with pytest.raises(ValueError):
        executor.execute('write', {'path': 'no.txt', 'content': 'no'}, 1)
    with pytest.raises(ValueError):
        executor.execute('exec', {'argv': ['echo', 'oops']}, 1)
    executor.execute('write', {'path': 'ok.txt', 'content': 'ok'}, 1)
    assert (workspace / 'ok.txt').read_text() == 'ok'


def test_boss_only_dispatches_through_team(workspace):
    model = Model(action('tool', name='exec', argv=['touch', 'oops']), action())
    root = Path(__file__).resolve().parents[1]
    session = harness.Session(root, workspace, 'boss', runner=model)
    assert session.turn('inspect')['ok']
    assert not (workspace / 'oops').exists()
    assert 'tool not allowed' in json.dumps(session.messages)
    for argv in [['run'], ['--'], ['decide', 'answer', '1', 'yes']]:
        with pytest.raises(ValueError):
            session.executor.execute('team', {'argv': argv}, 1)


def test_decision_writes_host_selected_path(workspace):
    model = Model(action('decision', text='Need decision', question='Which?', context='Choice',
                         options=['A', 'B'], recommendation='A'))
    destination = workspace.parent / 'decision.json'
    result = claude_run.legacy_run(workspace, 'work', runner=model, decision_file=destination)
    assert not result['ok']
    assert json.loads(destination.read_text())['question'] == 'Which?'


def test_turn_limit_and_malformed_action(workspace):
    result = claude_run.legacy_run(workspace, 'work', runner=Model(action('tool', name='list', path='.')), max_turns=1)
    assert not result['ok'] and 'turn limit' in result['result']
    result = claude_run.legacy_run(workspace, 'work', runner=Model({'kind': 'final'}))
    assert not result['ok'] and 'Invalid' in result['result']


def test_check_failure_cannot_be_declared_success(workspace):
    result = claude_run.legacy_run(workspace, 'work', runner=Model(action(), action()),
                            check='exit 1', max_turns=2)
    assert not result['ok'] and 'turn limit' in result['result']


def test_resume_is_team_history(workspace):
    root = Path(__file__).resolve().parents[1]
    first = harness.Session(root, workspace, 'boss', runner=Model(action(text='first')))
    assert first.turn('remember this')['ok']
    second = harness.Session(root, workspace, 'boss', session_id=first.id, runner=Model(action()))
    assert 'remember this' in json.dumps(second.messages)
    assert second.turn('continue')['ok']
    with pytest.raises(ValueError):
        harness.Session(root, workspace, 'boss', session_id='../escape')
    with pytest.raises(ValueError):
        harness.Session(root, workspace.parent, 'boss', session_id=first.id)


@pytest.mark.parametrize('stdout', ['oops', '[]', '{}', '{"is_error": true}'])
def test_bad_transport_output(workspace, stdout):
    def runner(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 0, stdout, '')
    with pytest.raises(claude_transport.TransportError):
        claude_transport.request(workspace, '', [], {}, runner=runner)


def test_timeout_returns_failure(workspace):
    def runner(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 1)
    result = claude_run.legacy_run(workspace, 'work', runner=runner)
    assert not result['ok']


def test_clean_env_preserves_subscription_token():
    env = {k: 'bad' for k in claude_transport.SCRUB}
    env.update(PATH='/bin', CLAUDE_CODE_OAUTH_TOKEN='subscription')
    assert claude_run.clean_env(env) == {'PATH': '/bin', 'CLAUDE_CODE_OAUTH_TOKEN': 'subscription'}
