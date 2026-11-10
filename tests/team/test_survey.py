"""Survey contracts: source stays untouched and reasoners receive only summaries."""
import json

import pytest

from team import ledger, scout, survey, targets
from team.review.backend import ScoutError
from tests.team.test_scout import repo, git


def test_survey_maps_areas_and_attributes_summary_only_reasoning(tmp_path):
    tree = repo(tmp_path)
    (tree / 'lib').mkdir()
    (tree / 'lib/work.py').write_text('RAW_SOURCE_SENTINEL = 42\n')
    before = git(tree, 'status', '--porcelain')
    prompts = []

    def call(model, prompt):
        prompts.append((model, prompt))
        text = 'what: bounded evidence\nrisks: verify behavior' if model == 'tier-simple' else 'Proposal: verify the cited behavior before building.'
        return {'text': text, 'model': model, 'usage': {'prompt_tokens': 100, 'completion_tokens': 20}}

    result = survey.run('demo', max_calls=10, call=call)
    assert result['complete'] and result['areas'] == 2 and result['calls'] == 5
    assert all('RAW_SOURCE_SENTINEL' not in p for m, p in prompts if m == 'tier-reasoning')
    assert git(tree, 'status', '--porcelain') == before
    assert not targets.require('demo')['enabled']
    folder = scout.directory('demo')
    assert json.loads((folder / 'map.json').read_text())['areas']['lib'] == ['lib/work.py']
    assert 'Proposal:' in json.loads((folder / 'ideas.json').read_text())['ideas']
    with ledger.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM calls WHERE task_class='survey'").fetchone()[0] == 3


def test_budget_stops_and_resume_reuses_source_cache(tmp_path):
    repo(tmp_path)
    calls = []

    def call(model, prompt):
        calls.append(model)
        return {'text': 'what: summary' if model == 'tier-simple' else 'Proposal: verify', 'usage': {}}

    with pytest.raises(ScoutError, match='remaining call budget'):
        survey.run('demo', max_calls=1, call=call)
    assert calls == ['tier-simple']
    assert not (scout.directory('demo') / 'ideas.json').exists()
    assert json.loads((scout.directory('demo') / 'review-status.json').read_text())['phase'] == 'stopped'
    survey.run('demo', max_calls=3, call=call)
    assert calls == ['tier-simple', 'tier-reasoning', 'tier-reasoning']


def test_changed_checkout_prevents_publishing_proposals(tmp_path):
    tree = repo(tmp_path)

    def call(model, prompt):
        if model == 'tier-reasoning':
            (tree / 'app.py').write_text('concurrent_change = True\n')
        return {'text': 'what: summary', 'usage': {}}

    with pytest.raises(ScoutError, match='Checkout changed'):
        survey.run('demo', max_calls=5, call=call)
    assert not (scout.directory('demo') / 'ideas.json').exists()
