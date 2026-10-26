"""Tests for recording the real models behind a LiteLLM-proxy worker session."""

import sqlite3

from team import proxy_usage, run


def _usage_db(path, rows):
    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE calls (id INTEGER PRIMARY KEY, ts TEXT, session TEXT, tier TEXT,
        cause TEXT, deployment TEXT, provider TEXT, model TEXT, tokens_in INTEGER,
        tokens_out INTEGER, latency_ms INTEGER, ok INTEGER, rate_limited INTEGER, error TEXT)""")
    conn.executemany(
        "INSERT INTO calls (session, deployment, tokens_in, tokens_out, latency_ms, ok, error)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
    conn.commit()
    conn.close()


def test_one_row_per_model_and_outcome(tmp_path):
    db = tmp_path / "usage.db"
    _usage_db(db, [
        ("t_1", "nvidia:poolside/laguna-xs-2.1", 0, 0, 1900, 0, "ResourceExhausted"),
        ("t_1", "openrouter:poolside/laguna-s-2.1:free", 100, 10, 900, 1, None),
        ("t_1", "openrouter:poolside/laguna-s-2.1:free", 200, 20, 1100, 1, None),
        ("t_2", "gemini:gemini-2.5-flash", 5, 5, 10, 1, None),
    ])
    rows = proxy_usage.calls_for_task("t_1", db)
    assert rows == [
        {"provider": "nvidia", "model": "poolside/laguna-xs-2.1", "tokens_in": 0, "tokens_out": 0,
         "latency_ms": 1900, "ok": False, "error": "ResourceExhausted", "api_calls": 1},
        {"provider": "openrouter", "model": "poolside/laguna-s-2.1:free", "tokens_in": 300,
         "tokens_out": 30, "latency_ms": 2000, "ok": True, "error": None, "api_calls": 2},
    ]


def test_no_usage_db_means_no_rows(tmp_path):
    assert proxy_usage.calls_for_task("t_1", tmp_path / "missing.db") == []


def test_proxy_session_uses_usage_log(monkeypatch):
    real = [{"provider": "openrouter", "model": "m:free", "tokens_in": 1, "tokens_out": 1,
             "latency_ms": 5, "ok": True, "error": None, "api_calls": 1}]
    monkeypatch.setattr(proxy_usage, "calls_for_task", lambda task_id: real if task_id == "t_9" else [])
    assert run._call_rows({"model": "team-auto"}, "t_9") == real


def test_proxy_session_without_log_falls_back_to_session(monkeypatch):
    monkeypatch.setattr(proxy_usage, "calls_for_task", lambda task_id: [])
    rows = run._call_rows({"model": "team-auto", "billing_provider": "custom"}, "t_9")
    assert [(r["provider"], r["model"]) for r in rows] == [("custom", "team-auto")]


def test_direct_session_is_recorded_as_is():
    rows = run._call_rows({"model": "x/y:free", "billing_provider": "openrouter"}, "t_9")
    assert [(r["provider"], r["model"]) for r in rows] == [("openrouter", "x/y:free")]


def test_tier_floor_and_retry_bump():
    from team import proxy_tier
    assert proxy_tier.for_task("x", 1, classify=lambda t: "SIMPLE")[0] == "tier-medium"
    assert proxy_tier.for_task("x", 1, classify=lambda t: "COMPLEX")[0] == "tier-complex"
    assert proxy_tier.for_task("x", 2, classify=lambda t: "MEDIUM")[0] == "tier-complex"
    assert proxy_tier.for_task("x", 5, classify=lambda t: "COMPLEX")[0] == "tier-reasoning"


def test_tier_falls_back_to_floor_when_proxy_is_down():
    from team import proxy_tier

    def down(text):
        raise ConnectionRefusedError()
    model, why = proxy_tier.for_task("x", 1, classify=down)
    assert model == "tier-medium" and "unavailable" in why


def test_tier_sessions_record_real_models(monkeypatch):
    real = [{"provider": "nvidia", "model": "m", "tokens_in": 1, "tokens_out": 1,
             "latency_ms": 5, "ok": True, "error": None, "api_calls": 1}]
    monkeypatch.setattr(proxy_usage, "calls_for_task", lambda task_id: real)
    assert run._call_rows({"model": "tier-complex"}, "t_9") == real
