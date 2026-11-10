"""A scout call's tier, responder and gateway trace survive success and failure."""
import io
import json
import urllib.error
from types import SimpleNamespace

import pytest

from team import ledger
from team.review import backend
from team.scout_usage import tracked_call


@pytest.mark.parametrize('failed', [False, True])
def test_gateway_identity_is_recorded_with_the_call(monkeypatch, failed):
    monkeypatch.setenv('LITELLM_MASTER_KEY', 'test-local-key')
    monkeypatch.setattr(backend.catalog, 'load', lambda: {'litellm': {'host': '127.0.0.1', 'port': 4000}})
    monkeypatch.setattr(backend.catalog, 'resolve', lambda spec, cat: SimpleNamespace(model='tier-simple'))
    captured = []

    def urlopen(request, timeout):
        captured.append(request)
        if failed:
            raise urllib.error.HTTPError(request.full_url, 429, 'limited', {}, None)
        return io.BytesIO(json.dumps({'model': 'actual-model', 'choices': [{'message': {'content': 'summary'}}],
                                     'usage': {'prompt_tokens': 10, 'completion_tokens': 2}}).encode())

    monkeypatch.setattr(backend.urllib.request, 'urlopen', urlopen)
    def call(model, prompt):
        return backend.complete('named-free', prompt, max_tokens=12)

    if failed:
        with pytest.raises(backend.ScoutError, match='429'):
            tracked_call('demo', 'scout', 'tier-simple', 'test input', call)
    else:
        result = tracked_call('demo', 'scout', 'tier-simple', 'test input', call)
        assert result['model'] == 'actual-model'
    with ledger.connect() as conn:
        row = dict(conn.execute('SELECT * FROM calls').fetchone())
    assert row['model'] == 'tier-simple'
    assert row['gateway_session_id'] == captured[0].get_header('X-litellm-session-id')
    assert row['gateway_session_id'].startswith('scout-')
    assert row['response_model'] == (None if failed else 'actual-model')
    assert row['ok'] == int(not failed)
    assert row['tokens_in'] == (None if failed else 10)
