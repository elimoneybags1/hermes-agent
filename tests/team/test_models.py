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


def test_candidates_that_pass_about_as_often_are_ranked_by_tokens_per_attempt():
    conn = ledger.connect()
    ledger.init(conn)
    role = {'name': 'implement', 'models': {'candidates': ['tier-simple', 'nvidia:z-ai/glm-5.3']}}
    # 6/6 on the first (0.875 smoothed) against 5/5 (0.857): within RATE_MARGIN, so the one
    # that gets there on a tenth of the input tokens is ranked first.
    for spec, count, tokens in [('tier-simple', 6, 100_000), ('nvidia:z-ai/glm-5.3', 5, 10_000)]:
        for _ in range(count):
            job = ledger.start_job(conn, 'demo', 'sha', 'implement', 'v1', model_choice={'spec': spec})
            ledger.record_call(conn, job, 'litellm', spec, 'implement', tokens, 300, 5, True)
            ledger.finish_job(conn, job, 'pass', 'check', '')
    chosen = models.choose(conn, role, 1, CATALOG, 'demo')
    assert chosen.spec == 'nvidia:z-ai/glm-5.3'
    assert 'then tokens per attempt' in chosen.why and '10,000 input tokens per attempt' in chosen.why
    assert models.choose(conn, role, 2, CATALOG, 'demo').spec == 'tier-simple'
    # a clearly better pass rate still wins whatever it costs
    for _ in range(3):
        job = ledger.start_job(conn, 'demo', 'sha', 'implement', 'v1', model_choice={'spec': 'nvidia:z-ai/glm-5.3'})
        ledger.record_call(conn, job, 'litellm', 'x', 'implement', 10_000, 300, 5, True)
        ledger.finish_job(conn, job, 'fail', 'check', 'assert failed')
    chosen = models.choose(conn, role, 1, CATALOG, 'demo')
    assert chosen.spec == 'tier-simple' and 'then tokens' not in chosen.why
