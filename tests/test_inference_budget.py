import json
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError
import pytest
from team.inference_budget import Budget, BudgetExceeded, Limits
from team.subscription_gateway import serve


def test_requests_fail_before_inference_and_reset_only_when_idle():
    b=Budget(Limits(max_calls=1,max_input_tokens=10,max_request_bytes=100))
    with pytest.raises(BudgetExceeded): b.admit(101)
    b.admit(10)
    with pytest.raises(BudgetExceeded): b.begin()
    b.finish(11)
    with pytest.raises(BudgetExceeded): b.admit(10)
    b.begin(); b.admit(10); b.finish()


def test_gateway_blocks_without_spending_and_writes_content_free_metrics(tmp_path):
    calls=[]
    def infer(*args,**kwargs):
        calls.append(1)
        return {'content':'done','tool_calls':[]},{'input_tokens':10,'output_tokens':1}
    trace=tmp_path/'inference.jsonl'
    with serve(Path.cwd(),request=infer,limits=Limits(max_calls=1),trace_path=trace) as gateway:
        def post(path,body):
            req=Request(gateway['url']+path,data=json.dumps(body).encode(),headers={'Authorization':'Bearer '+gateway['token']})
            with urlopen(req) as response:return json.load(response)
        payload={'messages':[{'role':'user','content':'PRIVATE_TEST_CONTENT'}]}
        post('/chat/completions',payload)
        with pytest.raises(HTTPError):post('/chat/completions',payload)
        assert len(calls)==1
        post('/team/begin-turn',{})
        post('/chat/completions',payload)
    assert len(calls)==2
    assert 'PRIVATE_TEST_CONTENT' not in trace.read_text()
    rows=[json.loads(s) for s in trace.read_text().splitlines()]
    assert rows[0]['usage']['prompt_tokens']==10
    assert rows[0]['history_chars']>0


def test_invalid_model_response_still_consumes_reported_usage():
    def infer(*args,**kwargs):
        return {'content':'','tool_calls':[{'name':'not_exposed','arguments':'{}'}]}, {'input_tokens':50}
    with serve(Path.cwd(), request=infer) as gateway:
        req=Request(gateway['url']+'/chat/completions',data=json.dumps({'messages':[{'role':'user','content':'hi'}]}).encode(),headers={'Authorization':'Bearer '+gateway['token']})
        with pytest.raises(HTTPError):urlopen(req)
        assert gateway['budget'].tokens==50
        assert gateway['budget'].active==0
