"""The Team Hermes plugin's ledger sink."""

import importlib.util
import json
from pathlib import Path

from team import ledger
from tests.team.test_catalog import CATALOG

REPO = Path(__file__).resolve().parents[2]


def load_sink():
    spec = importlib.util.spec_from_file_location(
        "team_ledger_sink", REPO / "plugins" / "team" / "ledger_sink.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_plugin_manifest_declares_the_hooks():
    text = (REPO / "plugins" / "team" / "plugin.yaml").read_text()
    assert "name: team" in text and "kind: standalone" in text
    assert "- post_api_request" in text and "- api_request_error" in text


def test_register_binds_both_hooks():
    spec = importlib.util.spec_from_file_location(
        "team_plugin", REPO / "plugins" / "team" / "__init__.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    bound = {}

    class Ctx:
        def register_hook(self, name, cb, **kw):
            bound[name] = cb

    module.register(Ctx())
    assert set(bound) == {"post_api_request", "api_request_error"}


def test_classify():
    sink = load_sink()
    assert sink.classify(
        "litellm", "http://127.0.0.1:4000/v1", "tier-medium", "tier-medium", CATALOG
    ) == (None, None)
    assert sink.classify(
        "litellm", "http://127.0.0.1:4000/v1", "tier-medium", "z-ai/glm-5.3", CATALOG
    ) == (None, None)
    assert sink.classify(
        "litellm", "http://127.0.0.1:4000/v1", "glm", "z-ai/glm-5.3", CATALOG
    ) == (None, None)
    assert sink.classify(
        "subscription", "http://127.0.0.1:8790/v1", "opus", "claude-opus", CATALOG
    ) == (None, None)
    kind, detail = sink.classify(
        "openrouter", "https://openrouter.ai/api/v1", "x", "x", CATALOG
    )
    assert kind == "off-gateway" and "openrouter" in detail
    kind, detail = sink.classify(
        "litellm", "http://127.0.0.1:4000/v1", "gpt-5", "gpt-5", CATALOG
    )
    assert kind == "non-catalog" and "not in the catalog" in detail
    kind, _ = sink.classify(
        "litellm", "http://127.0.0.1:4000/v1", "paid-thing", "paid-thing", CATALOG
    )
    assert kind == "non-catalog"
    assert sink.classify(
        "litellm", "http://127.0.0.1:4000/v1", "anything", None, None
    ) == (None, None)


def test_record_writes_a_call_and_a_violation(team_state, monkeypatch):
    sink = load_sink()
    monkeypatch.setattr(sink.catalog, "load", lambda path=None: CATALOG)
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_7")
    monkeypatch.setenv("HERMES_PROFILE", "implement")
    conn = ledger.connect()
    ledger.init(conn)
    job = ledger.start_job(conn, "team", "c", "implement", "v", task_ref="t_7")
    sink.record(
        provider="litellm",
        base_url="http://127.0.0.1:4000/v1",
        model="tier-medium",
        response_model="z-ai/glm-5.3",
        session_id="s1",
        api_call_count=3,
        api_duration=1.25,
        usage={"prompt_tokens": 120, "completion_tokens": 8, "total_tokens": 128},
    )
    [call] = ledger.calls_for(conn, task_ref="t_7")
    assert (
        call["job_id"] == job
        and call["profile"] == "implement"
        and call["session_id"] == "s1"
    )
    assert (call["tokens_in"], call["tokens_out"], call["latency_ms"], call["ok"]) == (
        120,
        8,
        1250,
        1,
    )
    assert call["response_model"] == "z-ai/glm-5.3" and call["api_calls"] == 3
    assert ledger.violations_for(conn, "t_7") == []

    sink.record(
        provider="litellm",
        base_url="http://127.0.0.1:4000/v1",
        model="gpt-5",
        response_model="gpt-5",
        usage=None,
    )
    calls = ledger.calls_for(conn, task_ref="t_7")
    assert calls[-1]["ok"] == 0 and "not in the catalog" in calls[-1]["error"]
    [violation] = ledger.violations_for(conn, "t_7")
    assert violation["kind"] == "non-catalog"

    sink.record(
        provider="anthropic",
        base_url="https://api.anthropic.com",
        model="claude",
        response_model=None,
    )
    assert [v["kind"] for v in ledger.violations_for(conn, "t_7")] == [
        "non-catalog",
        "off-gateway",
    ]
    conn.close()


def test_profile_name(monkeypatch, tmp_path):
    sink = load_sink()
    monkeypatch.delenv("HERMES_PROFILE", raising=False)
    monkeypatch.setenv(
        "HERMES_HOME", str(tmp_path / "hermes" / "profiles" / "implement")
    )
    assert sink.profile_name() == "implement"
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    assert sink.profile_name() == "default"
    monkeypatch.setenv("HERMES_PROFILE", "boss")
    assert sink.profile_name() == "boss"


def test_record_never_raises(team_state, monkeypatch):
    sink = load_sink()
    monkeypatch.setattr(
        sink.ledger,
        "record_call",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    sink.record(provider="litellm", base_url="http://127.0.0.1:4000/v1", model="m")
    log = (team_state / "logs" / "plugin.log").read_text()
    assert "ledger sink failed: boom" in log
    assert json.loads('{"ok": true}')["ok"]
