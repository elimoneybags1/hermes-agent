import json
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError

import pytest

from team.subscription_gateway import completion, serve


def test_preserves_native_prompt_history_and_normalizes_tools():
    seen = {}
    def infer(cwd, system, messages, schema, **kwargs):
        seen.update(system=system, messages=messages)
        return {'content': '', 'tool_calls': [{'name': 'memory', 'arguments': '{"action":"add"}'}]}, {'input_tokens': 2, 'cache_read_input_tokens': 7, 'output_tokens': 3}
    history = [{'role': 'system', 'content': 'Original Hermes instructions'}, {'role': 'user', 'content': 'Remember it'}]
    result = completion({'model': 'opus', 'messages': history, 'tools': [{'type': 'function', 'function': {'name': 'memory'}}]}, Path.cwd(), request=infer)
    assert seen['system'].startswith('Original Hermes instructions')
    assert seen['messages'] == history[1:]
    assert result['choices'][0]['finish_reason'] == 'tool_calls'
    assert result['choices'][0]['message']['tool_calls'][0]['function']['name'] == 'memory'
    assert result['usage']['prompt_tokens'] == 9
    assert result['usage']['prompt_tokens_details']['cached_tokens'] == 7
    assert result['team_transport_usage']['input_tokens'] == 2
    assert 'NEVER invoke these names through native' in seen['system']


def test_unknown_tool_and_multimodal_fail_explicitly():
    def infer(*a, **kw):
        return {'content': '', 'tool_calls': [{'name': 'unexposed', 'arguments': '{}'}]}, {}
    with pytest.raises(ValueError, match='unexposed'):
        completion({'messages': [{'role': 'user', 'content': 'x'}]}, Path.cwd(), request=infer)
    with pytest.raises(ValueError, match='text content only'):
        completion({'messages': [{'role': 'user', 'content': [{'type': 'image_url'}]}]}, Path.cwd(), request=infer)


def test_local_http_auth_and_streaming():
    def infer(*a, **kw):
        return {'content': 'ok', 'tool_calls': []}, {}
    with serve(Path.cwd(), request=infer) as gateway:
        data = json.dumps({'messages': [{'role': 'user', 'content': 'hi'}], 'stream': True}).encode()
        with pytest.raises(HTTPError) as exc:
            urlopen(Request(gateway['url'] + '/chat/completions', data=data))
        assert exc.value.code == 401
        response = urlopen(Request(gateway['url'] + '/chat/completions', data=data,
                           headers={'Authorization': 'Bearer ' + gateway['token']}))
        wire = response.read().decode()
        assert 'data: [DONE]' in wire and '"content": "ok"' in wire
        assert len(gateway['calls']) == 1
