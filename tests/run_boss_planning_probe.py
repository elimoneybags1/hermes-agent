"""Opt-in bounded live boss planning check with isolated target, state and worker roster."""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import yaml
from team import hermes_runtime as runtime, targets, ledger, planning


def main():
    with tempfile.TemporaryDirectory(prefix='team-boss-probe-') as tmp:
        base=Path(tmp);repo=base/'repo';repo.mkdir()
        os.environ['TEAM_STATE']=str(base/'state')
        os.environ['HERMES_HOME']=str(base/'workers')
        os.environ['TEAM_WORKER_HERMES_HOME']=str(base/'workers')
        (repo/'widget.py').write_text('def normalize_label(value):\n    return value.upper()\n')
        (repo/'test_widget.py').write_text('from widget import normalize_label\ndef test_spaces():\n    assert normalize_label(" hi ")=="HI"\n')
        (repo/'AGENTS.md').write_text('Modify only widget.py. Keep test expectations unchanged.\n')
        for name in ('team-worker','team-implement'):
            p=base/'workers/profiles'/name;p.mkdir(parents=True)
            (p/'config.yaml').write_text(yaml.safe_dump({'model':{'default':'team-auto'},'agent':{'max_turns':40}}))
        subprocess.run(['git','-C',str(repo),'init','-q','-b','main'],check=True)
        subprocess.run(['git','-C',str(repo),'add','.'],check=True)
        subprocess.run(['git','-C',str(repo),'-c','user.name=Verification','-c','user.email=verification@localhost','commit','-qm','fixture'],check=True)
        targets.attach(repo,'probe')
        with ledger.connect() as conn:ledger.init(conn)
        result_file=base/'result.json'
        prompt=('/plan For attached target probe, normalize_label in widget.py must strip surrounding whitespace before uppercasing. '
                'test_widget.py already specifies this behavior and must stay unchanged. Plan one implement task owning widget.py, '
                'with check `python -m pytest test_widget.py -q`. Inspect only widget.py and test_widget.py plus governing instructions. '
                'Save a ready contract using planning_update, then report its plan ID and readiness. Do not dispatch. '
                'The desired behavior is settled; there are no product decisions to ask about.')
        proc,calls=runtime.launch('boss',root=ROOT,cwd=repo,prompt=prompt,max_turns=12,timeout_s=180,capture=True,result_file=result_file)
        result=json.loads(result_file.read_text()) if result_file.exists() else {}
        plans=[json.loads(p.read_text()) for p in (base/'state/planning').glob('*.json')]
        ready=[planning.Planner(ROOT).readiness(p) for p in plans]
        report={'exit_code':proc.returncode,'result':result,'plans':[{'id':p['id'],'contract':p['contract'],'task_goals':p['task_goals'],'bytes_read':p['bytes_read']} for p in plans],
                'readiness_errors':ready,'calls':calls,'input_tokens':sum(c['usage']['prompt_tokens'] for c in calls),
                'stderr_tail':proc.stderr[-1500:],'stdout_tail':proc.stdout[-1500:]}
        report['passed']=bool(plans) and all(not e for e in ready) and all(not p['task_goals'] for p in plans) and proc.returncode==0
        (ROOT/'reports/boss-planning-baseline.json').write_text(json.dumps(report,indent=2))
        print(json.dumps({k:report[k] for k in ('passed','exit_code','readiness_errors','input_tokens')}))
        return 0 if report['passed'] else 1


if __name__=='__main__':raise SystemExit(main())
