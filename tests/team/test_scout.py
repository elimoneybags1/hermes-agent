"""An existing dirty checkout is evidence, never a worktree to clean or overwrite."""
import subprocess
from pathlib import Path

from team import scout, targets, lead
from team.review.summarize import merge_notes


def git(repo, *args):
    return subprocess.check_output(['git', '-C', str(repo), *args], text=True).strip()


def repo(tmp_path):
    p = tmp_path / 'repo'
    p.mkdir()
    git(p, 'init', '-q', '-b', 'main')
    git(p, 'config', 'user.name', 'Test')
    git(p, 'config', 'user.email', 'test@example.invalid')
    (p / 'app.py').write_text('original = 1\n')
    (p / '.gitignore').write_text('.data/\n')
    git(p, 'add', '.')
    git(p, 'commit', '-qm', 'initial')
    targets.attach(p, name='demo', enabled=False)
    return p


def test_scout_dirty_source_cache_and_stale_evidence(tmp_path):
    p = repo(tmp_path)
    (p / 'app.py').write_text('dirty = 2\n')
    (p / 'new.ts').write_text('export const unseen = 3;\n')
    (p / '.env').write_text('NEVER_SEND=credential\n')
    (p / '.data').mkdir()
    (p / '.data/private.txt').write_text('private student transcript')
    (p / 'linked.py').symlink_to(p / '.env')
    before = git(p, 'status', '--porcelain'), (p / 'app.py').read_bytes()
    calls = []

    def call(model, prompt):
        calls.append(prompt)
        return {'text': 'what: dirty sentinel\nrisks: inspect the contract',
                'usage': {'prompt_tokens': 100, 'completion_tokens': 20}}

    first = scout.scan('demo', call=call, max_calls=1)
    assert not first['complete'] and first['pending_chunks'] == 1
    second = scout.scan('demo', call=call)
    assert second['complete'] and second['cache_hits'] == 1
    assert len(calls) == 2
    assert 'dirty = 2' in calls[0] and 'unseen = 3' in calls[1]
    assert all('NEVER_SEND' not in x and 'private student' not in x for x in calls)
    assert first['input_tokens'] == 100 and second['input_tokens'] == 100
    assert before == (git(p, 'status', '--porcelain'), (p / 'app.py').read_bytes())
    assert targets.require('demo')['enabled'] is False
    assert 'dirty sentinel' in scout.context('demo')
    (p / 'app.py').write_text('new_version = 4\n')
    (p / 'new.ts').unlink()
    assert 'dirty sentinel' not in scout.context('demo')
    scout.scan('demo', call=call)
    assert len(calls) == 3


def test_scout_brief_replaces_bulk_source_and_keeps_instructions(tmp_path):
    p = repo(tmp_path)
    (p / 'AGENTS.md').write_text('Do not change the API.')
    (p / 'app.py').write_text('secret_bulk_marker = 42\n')
    def call(model, prompt):
        return {'text': 'what: a tiny app', 'usage': {}}
    report = scout.scan('demo', call=call)
    context = scout.context('demo', tree=p, report=report)
    briefing = lead.briefing(p, 'fix app.py', 12000, scout_context=context)
    assert 'Do not change the API.' in briefing
    assert 'a tiny app' in briefing and 'secret_bulk_marker' not in briefing
    assert 'app.py:1-1' in briefing


def test_summary_gives_later_sections_their_own_budget():
    text = merge_notes([{'what': ['a' * 140] * 3, 'structure': ['b' * 140],
                         'risks': ['Important risk'], 'improve': ['Useful improvement']}], 800)
    assert 'Important risk' in text and 'Useful improvement' in text
    assert len(text) <= 800


def test_wrapped_model_notes_keep_the_evidence():
    notes = scout.parse_notes('what: **\nA lecture study app\nstructure: **\n- Local audio\n'
                              '- Remote text\nrisks: none proven', 'README.md:1-20')
    assert 'lecture study app' in notes['what'][0]
    assert 'Local audio Remote text' in notes['structure'][0]
    assert notes['risks'] == ['none proven (README.md:1-20)']


def test_reasoner_only_gets_summary_not_raw_code(tmp_path):
    p = repo(tmp_path)
    (p / 'app.py').write_text('UNIQUE_RAW_SOURCE = 123\n')
    scout.scan('demo', call=lambda m, p: {'text': 'what: a study tool', 'usage': {}})
    prompts = []
    def reason(model, prompt):
        prompts.append(prompt)
        return {'text': 'Proposal: verify the study flow first.', 'usage': {'prompt_tokens': 25}}
    result = scout.ideas('demo', call=reason)
    assert 'a study tool' in prompts[0] and 'UNIQUE_RAW_SOURCE' not in prompts[0]
    assert result['usage']['prompt_tokens'] == 25
    assert 'Proposal: verify' in scout.read_ideas('demo', scout.context('demo'))
    assert (p / 'app.py').read_text() == 'UNIQUE_RAW_SOURCE = 123\n'



def test_summary_prioritizes_runtime_evidence_over_repeated_policy_notes():
    files = {
        'CLAUDE.md': {'notes': [{'risks': ['policy detail ' * 100], 'improve': ['add generic checks']} for _ in range(8)]},
        'lib/pipeline.ts': {'notes': [{'risks': ['live capture can be interrupted (lib/pipeline.ts:7-15)'],
                                     'improve': ['investigate capture recovery (lib/pipeline.ts:7-15)']}]},
    }
    text = scout.summarize_report(files, max_chars=800)
    assert 'live capture can be interrupted' in text
    assert 'investigate capture recovery' in text
    assert len(text) <= 800
