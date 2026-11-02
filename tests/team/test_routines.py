import subprocess
from pathlib import Path

from team import ledger, routines, targets
from tests.team.test_scout import repo


def test_routine_is_opt_in_once_per_interval_and_respects_target_pause(tmp_path, team_state):
    repo(tmp_path)
    conn = ledger.connect()
    ledger.init(conn)
    p = team_state / 'targets/demo/routines.toml'
    p.write_text('[[routines]]\nname="survey"\naction="scout"\nenabled=true\nevery_s=600\n')
    calls = []
    def scan(target, **kwargs):
        calls.append(target)
        return {'calls': 1, 'pending_chunks': 0, 'errors': []}
    kwargs = dict(runner=subprocess.run, cat=None, log=lambda s: None, scan=scan)
    routines.tick(Path('.'), conn, 'demo', 1000, **kwargs)
    assert not calls  # target disabled
    targets.set_field('demo', 'enabled', True)
    routines.tick(Path('.'), conn, 'demo', 1000, **kwargs)
    routines.tick(Path('.'), conn, 'demo', 1100, **kwargs)
    assert calls == ['demo']
    routines.tick(Path('.'), conn, 'demo', 1200, **kwargs)
    assert calls == ['demo', 'demo']


def test_crashed_claim_does_not_schedule_duplicate_work(tmp_path, team_state):
    repo(tmp_path)
    targets.set_field('demo', 'enabled', True)
    conn = ledger.connect()
    ledger.init(conn)
    p = team_state / 'targets/demo/routines.toml'
    p.write_text('[[routines]]\nname="survey"\naction="scout"\nenabled=true\nevery_s=600\n')
    calls = []
    def scan(target, **kwargs):
        calls.append(target)
        return {'calls': 0, 'pending_chunks': 0, 'errors': []}
    kwargs = dict(runner=subprocess.run, cat=None, log=lambda s: None, scan=scan)
    routines.tick(Path('.'), conn, 'demo', 1000, **kwargs)
    conn.execute("UPDATE routine_runs SET status='claimed'")
    conn.commit()
    routines.tick(Path('.'), conn, 'demo', 1200, **kwargs)
    assert calls == ['demo']
