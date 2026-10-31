"""clone_repo: shallow clone of a local bare repo, and the failure paths."""

import subprocess
import pytest
from team.review.clone import CloneError, clone_repo


def _git(*args, cwd):
    subprocess.run(['git', *args], cwd=cwd, check=True, capture_output=True, text=True)


@pytest.fixture
def bare_repo(tmp_path):
    work = tmp_path / 'work'
    work.mkdir()
    _git('init', '-b', 'main', cwd=work)
    (work / 'user.email').write_text('t@t')
    (work / 'user.name').write_text('t')
    _git('config', 'user.email', 't@t', cwd=work)
    _git('config', 'user.name', 't', cwd=work)
    (work / 'a.txt').write_text('content_a')
    _git('add', '.', cwd=work)
    _git('commit', '-m', 'one', cwd=work)
    (work / 'b.txt').write_text('content_b')
    _git('add', '.', cwd=work)
    _git('commit', '-m', 'two', cwd=work)
    _git('clone', '--bare', str(work), str(tmp_path / 'origin.git'), cwd=work)
    return f'file://{tmp_path / "origin.git"}'


def test_clone_is_shallow_and_returns_path(bare_repo, tmp_path):
    out = clone_repo(bare_repo, tmp_path / 'dest')
    assert out == (tmp_path / 'dest').resolve()
    assert (out / 'b.txt').read_text() == 'content_b'
    result = subprocess.run(['git', '-C', str(out), 'rev-list', '--count', 'HEAD'],
                           capture_output=True, text=True, check=False)
    assert result.stdout.strip() == '1'


def test_clone_rejects_non_empty_dest(tmp_path):
    dest = tmp_path / 'nonempty'
    dest.mkdir()
    (dest / 'somefile').write_text('exists')
    call_count = []
    def fake_runner(*args, **kwargs):
        call_count.append(1)
        raise AssertionError('git should not run')
    with pytest.raises(CloneError, match=rf'{dest} exists and is not empty'):
        clone_repo('https://example.com/repo.git', dest, runner=fake_runner)
    assert not call_count  # Should not call the fake runner


def test_clone_failure_raises_with_stderr(tmp_path):
    fake_runner = lambda cmd, **kw: subprocess.CompletedProcess(cmd, 128, stdout='', stderr='fatal: repository not found')
    with pytest.raises(CloneError, match='repository not found'):
        clone_repo('https://example.com/repo.git', tmp_path / 'dest', runner=fake_runner)


def test_clone_passes_no_credentials_and_disables_prompt(tmp_path):
    call_record = []
    def fake_runner(cmd, **kwargs):
        call_record.append((cmd, kwargs))
        return subprocess.CompletedProcess(cmd, 0, stdout='', stderr='')

    clone_repo('file://dummy', tmp_path / 'dest', runner=fake_runner)
    assert call_record
    cmd, kwargs = call_record[0]
    assert cmd[:4] == ['git', 'clone', '--depth', '1']
    assert kwargs['env']['GIT_TERMINAL_PROMPT'] == '0'
    assert not any('token' in str(arg) or 'Authorization' in str(arg) for arg in cmd)