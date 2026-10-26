"""A real native dispatcher/worker/tool/grade cycle; model responses are scripted."""
import json
import subprocess
import sys
from pathlib import Path


def test_native_plan_dispatch_review_grade_and_land(tmp_path):
    root=Path(__file__).resolve().parents[1]
    output=tmp_path/'result.json'
    proc=subprocess.run([sys.executable,str(root/'tests/run_team_pipeline.py'),'--scripted','--output',str(output)],
                        cwd=root,capture_output=True,text=True,timeout=90)
    assert proc.returncode==0,proc.stdout+proc.stderr
    report=json.loads(output.read_text())
    assert report['goal_outcome']=='pass'
    assert report['statuses']==['running','review']
    assert report['tests_unchanged']
    assert 'strip().upper()' in report['landed_source']
    assert report['calls'] and report['calls'][0]['model']=='tier-medium'
    assert all('File not found' not in (c.get('last_result') or '') for c in report['model_calls'])
