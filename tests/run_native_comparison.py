"""Opt-in bounded live verification: python tests/run_native_comparison.py --help."""
from __future__ import annotations
import argparse
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import yaml
from team import hermes_runtime as runtime
from team import claude_transport
from team.subscription_gateway import completion

PROMPT = ('Read widget.py and test_widget.py. Fix normalize_label so it strips surrounding whitespace '
          'before uppercasing. Modify only widget.py with the native file tools, read the result back, '
          'then report completion. Do not investigate any other files.')


def run(backend, compress=False):
    calls = []
    lock = threading.Lock()
    with tempfile.TemporaryDirectory(prefix='team-native-baseline-') as tmp:
        base = Path(tmp); repo = base/'repo'; repo.mkdir()
        (repo/'widget.py').write_text('def normalize_label(value):\n    return value.upper()\n')
        test = 'import unittest\nfrom widget import normalize_label\nclass TestLabel(unittest.TestCase):\n    def test_spaces(self):\n        self.assertEqual(normalize_label(" hi "), "HI")\n'
        (repo/'test_widget.py').write_text(test)
        (repo/'AGENTS.md').write_text('Make only the requested change. Do not modify test expectations.\n')
        home=base/'profile'; home.mkdir()
        (home/'SOUL.md').write_text((runtime.FORK/'SOUL.md').read_text())
        model = 'tier-medium' if backend=='direct' else 'opus'
        if backend=='direct':
            from team.proxy_tier import _key
            key = _key()
        else:
            claude_transport.subscription_ready(repo)
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args): pass
            def do_POST(self):
                started=time.monotonic()
                body=self.rfile.read(int(self.headers.get('Content-Length',0)))
                payload=json.loads(body); streaming=bool(payload.get('stream')); payload['stream']=False
                if self.path != '/v1/chat/completions':
                    self.send_response(404);self.end_headers();return
                stats={'request_bytes':len(body),'stream':streaming,
                       'system_chars':sum(len(str(m.get('content',''))) for m in payload.get('messages',[]) if m.get('role')=='system'),
                       'history_chars':sum(len(json.dumps(m)) for m in payload.get('messages',[]) if m.get('role')!='system'),
                       'tool_schema_chars':len(json.dumps(payload.get('tools',[]))),
                       'tool_count':len(payload.get('tools',[]))}
                with lock:
                    refuse = len(calls)>=12 or sum(c.get('usage',{}).get('prompt_tokens',0) for c in calls)>=80000 or len(body)>150000
                    calls.append(stats)
                try:
                    if refuse: raise ValueError('verification budget exhausted; no further model calls')
                    if backend=='direct':
                        req=urllib.request.Request('http://127.0.0.1:4000/v1/chat/completions',data=json.dumps(payload).encode(),
                            headers={'Authorization':'Bearer '+key,'Content-Type':'application/json','x-litellm-session-id':'team-baseline-verification'})
                        with urllib.request.urlopen(req,timeout=60) as response: result=json.load(response)
                    else:
                        result=completion(payload,repo,timeout_s=60)
                    stats['usage']=result.get('usage',{})
                    stats['transport_usage']=result.get('team_transport_usage',{})
                    stats['requested_tools']=[t['function']['name'] for t in result['choices'][0]['message'].get('tool_calls',[])]
                    if streaming:
                        choice=result['choices'][0]
                        delta=dict(choice['message'])
                        if delta.get('tool_calls'):
                            delta['tool_calls']=[{'index':i,**c} for i,c in enumerate(delta['tool_calls'])]
                        chunk={k:result[k] for k in ('id','created','model')}
                        chunk.update(object='chat.completion.chunk',choices=[{'index':0,'delta':delta,'finish_reason':None}])
                        wire='data: '+json.dumps(chunk)+'\n\n'
                        chunk.update(choices=[{'index':0,'delta':{},'finish_reason':choice['finish_reason']}],usage=result.get('usage',{}))
                        wire+='data: '+json.dumps(chunk)+'\n\ndata: [DONE]\n\n'
                        encoded=wire.encode()
                    else:
                        encoded=json.dumps(result).encode()
                    self.send_response(200)
                except Exception as exc:
                    stats['error']=f'{type(exc).__name__}: {str(exc)[:250]}'
                    encoded=json.dumps({'error':{'message':stats['error'],'type':'invalid_request_error'}}).encode(); self.send_response(400)
                stats['duration_s']=round(time.monotonic()-started,3)
                self.send_header('Content-Type','text/event-stream' if streaming and 'error' not in stats else 'application/json');self.send_header('Content-Length',str(len(encoded)));self.end_headers()
                try: self.wfile.write(encoded)
                except BrokenPipeError: pass
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler);server.daemon_threads=True
        threading.Thread(target=server.serve_forever,daemon=True).start()
        gateway={'url':f'http://127.0.0.1:{server.server_port}/v1','token':'isolated-probe'}
        config={'model':{'provider':'custom','default':model,'base_url':gateway['url'],'api_key':'isolated-probe','api_mode':'chat_completions','context_length':160000,'streaming':False},
                'fallback_providers':[], 'compression':{'enabled':True},'tools':{'tool_search':{'enabled':'off'}},
                'auxiliary':{'free_only':True}}
        for purpose in ('compression','title_generation'):
            config['auxiliary'][purpose]={'provider':'custom','model':model,'base_url':gateway['url'],'api_key':'isolated-probe','fallback_chain':[]}
        (home/'config.yaml').write_text(yaml.safe_dump(config))
        payload=base/'probe.json';output=base/'result.json'
        payload.write_text(json.dumps({'model':model,'prompt':PROMPT,'compress':compress,'result':str(output)}))
        os.environ['TEAM_STATE']=str(base/'state')
        env=runtime.runtime_env(home,gateway);env['TERMINAL_CWD']=str(repo)
        try:
            proc=subprocess.run([str(runtime.python_executable()),str(ROOT/'tests/native_pipeline_probe.py'),str(payload)],cwd=repo,env=env,capture_output=True,text=True,timeout=240)
            result=json.loads(output.read_text()) if output.exists() else {}
            result.update(backend=backend,exit_code=proc.returncode,calls=calls,stderr_tail=proc.stderr[-1500:],stdout_tail=proc.stdout[-1500:])
            check=subprocess.run([sys.executable,'-m','unittest','-q'],cwd=repo,capture_output=True,text=True)
            result['check_passed']=check.returncode==0
            result['tests_unchanged']=(repo/'test_widget.py').read_text()==test
            result['total_input_tokens']=sum(c.get('usage',{}).get('prompt_tokens',0) for c in calls)
            result['total_output_tokens']=sum(c.get('usage',{}).get('completion_tokens',0) for c in calls)
            return result
        finally:
            server.shutdown();server.server_close()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backend',choices=['direct','subscription'],required=True)
    parser.add_argument('--compress',action='store_true')
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    result=run(args.backend,args.compress)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(result,indent=2))
    print(json.dumps({k:result.get(k) for k in ('backend','completed','error','check_passed','tests_unchanged','total_input_tokens','total_output_tokens','compression','continuation')}))
    sys.exit(0 if result.get('completed') and result.get('check_passed') and result.get('tests_unchanged') else 1)
