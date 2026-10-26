"""Opt-in live test of Team -> real Kanban dispatch -> native worker -> grading/landing."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import yaml
from team import ledger,targets,run,kanban,hermes_runtime,profiles,planning
from team.proxy_tier import _key


def git(repo,*args):
    return subprocess.run(['git','-C',str(repo),*args],capture_output=True,text=True,check=True).stdout.strip()


def main():
    scripted = '--scripted' in sys.argv
    report={'scripted_model':scripted}
    model_calls=[]
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_POST(self):
            payload=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            if self.path!='/v1/chat/completions':self.send_response(404);self.end_headers();return
            tools={t['function']['name'] for t in payload.get('tools',[])}
            messages=payload['messages']
            results=[m for m in messages if m.get('role')=='tool']
            record={'tool_count':len(tools),'last_result':str(results[-1].get('content',''))[:350] if results else None}
            calls=[]
            if tools:
                turn=sum(bool(c['tool_count']) for c in model_calls)
                actions=[
                    [('kanban_show',{})],
                    [('read_file',{'path':'widget.py'}),('read_file',{'path':'test_widget.py'})],
                    [('patch',{'mode':'replace','path':'widget.py','old_string':'return value.upper()','new_string':'return value.strip().upper()'})],
                    [('terminal',{'command':f'{sys.executable} -m unittest -q && git add widget.py && git commit -m fix','timeout':20})],
                    [('kanban_request_review',{'summary':'Fixed whitespace handling; unittest passed and change committed.'})]]
                if turn<len(actions):
                    for name,args in actions[turn]:
                        assert name in tools, (name,tools)
                        calls.append({'id':f'probe-{turn}-{len(calls)}','type':'function','function':{'name':name,'arguments':json.dumps(args)}})
            record['requested_tools']=[c['function']['name'] for c in calls];model_calls.append(record)
            message={'role':'assistant','content':'Done' if not calls else ''}
            if calls:message['tool_calls']=calls
            result={'id':'probe','object':'chat.completion','created':int(time.time()),'model':'tier-medium',
                    'choices':[{'index':0,'message':message,'finish_reason':'tool_calls' if calls else 'stop'}],
                    'usage':{'prompt_tokens':10,'completion_tokens':5,'total_tokens':15}}
            body=json.dumps(result).encode();self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
    server=None
    if scripted:
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler);server.daemon_threads=True
        threading.Thread(target=server.serve_forever,daemon=True).start()
    with tempfile.TemporaryDirectory(prefix='team-e2e-') as tmp:
        base=Path(tmp); repo=base/'repo';repo.mkdir()
        root=base/'team';root.mkdir()
        os.environ['TEAM_STATE']=str(base/'state')
        os.environ['HERMES_HOME']=str(base/'hermes')
        os.environ.pop('HERMES_PROFILE',None)
        os.environ['PATH']=hermes_runtime.fork_environment()['PATH']
        os.environ['HERMES_BIN']=str(hermes_runtime.FORK/'hermes')
        os.environ['HERMES_YOLO']='1'
        os.environ['TERMINAL_CWD']=str(repo)
        for p in (repo,root):
            git(p,'init','-q','-b','main');git(p,'config','user.name','Team Verification');git(p,'config','user.email','verification@localhost')
        (repo/'widget.py').write_text('def normalize_label(value):\n    return value.upper()\n')
        test='import unittest\nfrom widget import normalize_label\nclass TestLabel(unittest.TestCase):\n    def test_spaces(self):\n        self.assertEqual(normalize_label(" hi "), "HI")\n'
        (repo/'test_widget.py').write_text(test)
        (repo/'.gitignore').write_text('__pycache__/\n')
        (repo/'AGENTS.md').write_text('Modify only widget.py. Keep tests unchanged. Commit the fix and request Kanban review.\n')
        git(repo,'add','.');git(repo,'commit','-qm','fixture')
        dest=root/'roles/implement';dest.mkdir(parents=True)
        manifest=(ROOT/'roles/implement/role.toml').read_text().replace('"team-auto"','"tier-medium"').replace('max_turns = 40','max_turns = 10').replace('run_budget_s = 900','run_budget_s = 240').replace('after_failures = 2','after_failures = 0')
        (dest/'role.toml').write_text(manifest)
        (root/'orchestration.toml').write_text('[escalation]\nclaude=false\n[loop]\nmax_new_per_tick=1\n')
        git(root,'add','.');git(root,'commit','-qm','verification role')
        config={'model':{'provider':'custom','default':'tier-medium','base_url':f'http://127.0.0.1:{server.server_port}/v1' if scripted else 'http://127.0.0.1:4000/v1','api_key':'fixture' if scripted else _key(),'api_mode':'chat_completions','streaming':False,'context_length':160000},
                'agent':{'max_turns':10},'platform_toolsets':{'cli':['file','terminal','kanban']},
                'tools':{'tool_search':{'enabled':'off'}},'fallback_providers':[],
                'kanban':{'auto_decompose':False,'review_dispatch':False},
                'auxiliary':{'free_only':True,'background_review':{'enabled':False}},'compression':{'enabled':True}}
        for name in ('team-worker','team-implement'):
            home=profiles._hermes_profile_dir(name);home.mkdir(parents=True)
            path=home/'config.yaml';path.write_text(yaml.safe_dump(config));path.chmod(0o600)
            (home/'SOUL.md').write_text((hermes_runtime.FORK/'SOUL.md').read_text()+'\n'+(ROOT/'roles/implement/SOUL.md').read_text())
        targets.attach(repo,'probe')
        with ledger.connect() as conn:
            ledger.init(conn)
            planner=planning.Planner(root)
            pid=planner.start('probe','Strip whitespace before uppercasing')['plan_id']
            planner.scope(pid,['widget.py','test_widget.py'],'The requested behavior and its test',[])
            planner.read(pid,'widget.py');planner.read(pid,'test_widget.py')
            contract={'success':[{'id':'spaces','behavior':'Normalize surrounding whitespace','verification':'unittest'}],
                      'change_scope':['widget.py'],'verification_scope':['test_widget.py'],'exclusions':['test changes'],
                      'stop_conditions':['Unknown behavior'],'decisions':[],
                      'tasks':[{'id':'fix','title':'Normalize whitespace','body':'Read widget.py and test_widget.py. Change normalize_label to strip surrounding whitespace before uppercasing. Modify only widget.py. Run the check, commit and request review.',
                                'role':'implement','files':['widget.py'],'criteria':['spaces'],'check':f'{sys.executable} -m unittest -q'}]}
            assert planner.update(pid,contract)['ready']
            dispatched_goal=planner.dispatch(pid,'fix')
            goal=ledger.get_goal(conn,dispatched_goal['goal_id'])
            report['plan_id']=pid
            report['goal_id']=goal['id'];report['task_id']=goal['current_task']
            dispatched=kanban.dispatch('probe',max_spawns=1)
            report['dispatch']=dispatched
            print('Dispatched isolated task',goal['current_task'],flush=True)
            end=time.monotonic()+260
            observed=[]
            while time.monotonic()<end:
                full=kanban.show_full('probe',goal['current_task'])
                status=full['task']['status']
                if not observed or observed[-1]!=status:
                    observed.append(status);print('Task status:',status,flush=True)
                if status in ('review','done'):
                    run.Loop(root,conn).step(ledger.get_goal(conn,goal['id']),full)
                    break
                if status=='blocked':break
                time.sleep(2)
            final=ledger.get_goal(conn,goal['id'])
            report.update(statuses=observed,goal_outcome=final['outcome'],detail=final['detail'],
                          landed_source=(repo/'widget.py').read_text(),tests_unchanged=(repo/'test_widget.py').read_text()==test,
                          jobs=[dict(r) for r in conn.execute('select * from jobs')],calls=[dict(r) for r in conn.execute('select * from calls')])
            if final['outcome']!='pass':
                report['board']=kanban.show_full('probe',goal['current_task'])
                report['worker_log']=kanban.log('probe',goal['current_task'],tail=3000)
                # Stop only a worker spawned in this isolated board before cleanup.
                for r in report['board'].get('runs',[]):
                    pid=r.get('worker_pid')
                    if pid and not r.get('ended_at'):
                        try:os.kill(pid,15)
                        except ProcessLookupError:pass
        report['model_calls']=model_calls
        if server:server.shutdown();server.server_close()
        out=Path(sys.argv[sys.argv.index('--output')+1]) if '--output' in sys.argv else ROOT/('reports/team-native-scripted-end-to-end.json' if scripted else 'reports/team-native-end-to-end.json');out.write_text(json.dumps(report,indent=2))
        print(json.dumps({'outcome':report.get('goal_outcome'),'statuses':report.get('statuses'),'tests_unchanged':report.get('tests_unchanged'),'detail':report.get('detail')}))
        return 0 if report.get('goal_outcome')=='pass' else 1


if __name__=='__main__':
    raise SystemExit(main())
