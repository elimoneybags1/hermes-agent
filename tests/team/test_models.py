from team import ledger, models
from tests.team.test_catalog import CATALOG


def test_ranking_excludes_infra_and_respects_pin_and_candidate_boundary():
    conn = ledger.connect()
    ledger.init(conn)
    role = {'name': 'implement', 'models': {'candidates': ['tier-simple', 'nvidia:z-ai/glm-5.3']}}
    for spec, result, detail, count in [('tier-simple', 'fail', 'failed assertion', 8),
                                        ('nvidia:z-ai/glm-5.3', 'pass', '', 4),
                                        ('nvidia:z-ai/glm-5.3', 'error', 'quota exhausted', 20)]:
        for _ in range(count):
            job = ledger.start_job(conn, 'demo', 'sha', 'implement', 'v1', model_choice={'spec': spec})
            ledger.finish_job(conn, job, result, 'worker' if result == 'error' else 'check', detail)
    chosen = models.choose(conn, role, 1, CATALOG, 'demo')
    assert chosen.spec == 'nvidia:z-ai/glm-5.3'
    assert '4/4' in chosen.why and 'baseline 4/12' in chosen.why
    role['models']['pinned'] = 'tier-simple'
    assert models.choose(conn, role, 1, CATALOG, 'demo').spec == 'tier-simple'
