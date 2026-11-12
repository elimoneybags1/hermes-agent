"""Usage callbacks retain provider accounting without inventing missing usage."""
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def usage_log(monkeypatch, tmp_path):
    # Only the callback base class is a proxy dependency; exercise the real adapter and DB.
    monkeypatch.setitem(sys.modules, "litellm.integrations.custom_logger", SimpleNamespace(CustomLogger=object))
    monkeypatch.setitem(sys.modules, "litellm.constants", SimpleNamespace(RETURN_RAW_MODEL_NAME_METADATA_KEY='raw-model-test'))
    path = Path(__file__).resolve().parents[2] / "litellm/team/usage_log.py"
    spec = importlib.util.spec_from_file_location("usage_log_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "DB", tmp_path / "usage.db")
    return module


def test_cache_and_reasoning_are_subsets_not_added_twice(usage_log):
    usage = {"prompt_tokens": 100, "completion_tokens": 30, "total_tokens": 130,
             "prompt_tokens_details": {"cached_tokens": 80, "cache_write_tokens": 10},
             "completion_tokens_details": {"reasoning_tokens": 20}}
    fields = usage_log.usage_fields({"usage": usage}, {}, True)
    assert (fields["tokens_in"], fields["tokens_out"], fields["tokens_total"]) == (100, 30, 130)
    assert (fields["cache_read_tokens"], fields["cache_write_tokens"], fields["reasoning_tokens"]) == (80, 10, 20)
    assert json.loads(fields["usage_json"]) == usage and fields["usage_reported"] == 1


def test_missing_usage_on_failure_is_unknown_not_zero(usage_log):
    fields = usage_log.usage_fields(None, {"prompt_tokens": 0, "completion_tokens": 0}, False)
    assert fields["tokens_in"] is None and fields["tokens_out"] is None
    assert fields["usage_reported"] == 0 and fields["usage_json"] is None


def test_sdk_usage_and_anthropic_buckets_are_preserved(usage_log):
    response = SimpleNamespace(usage=SimpleNamespace(model_dump=lambda: {
        "input_tokens": 10, "cache_read_input_tokens": 70, "cache_creation_input_tokens": 20, "output_tokens": 5}))
    fields = usage_log.usage_fields(response, {}, True)
    assert fields["tokens_in"] == 100 and fields["tokens_total"] == 105


def test_existing_history_survives_callback_schema_upgrade(usage_log):
    import sqlite3
    with sqlite3.connect(usage_log.DB) as conn:
        conn.execute('CREATE TABLE calls(id INTEGER PRIMARY KEY, ts TEXT, session TEXT, "group" TEXT, tier TEXT, deployment TEXT, config TEXT, model TEXT, tokens_in INTEGER, tokens_out INTEGER, latency_ms INTEGER, ok INTEGER, rate_limited INTEGER, error TEXT)')
        conn.execute('INSERT INTO calls(ts, model, tokens_in, ok) VALUES (?, ?, ?, ?)', ('old', 'old-model', 42, 1))
    kwargs = {"standard_logging_object": {"model": "new-model", "model_group": "tier-simple", "model_id": "tier-simple/new-model"}}
    usage_log.logger._write(kwargs, True, {"usage": {"prompt_tokens": 10, "completion_tokens": 2}})
    with usage_log.connect() as conn:
        rows = conn.execute('SELECT model,tokens_in,usage_reported FROM calls ORDER BY id').fetchall()
    assert rows == [('old-model', 42, 0), ('new-model', 10, 1)]


def test_response_identity_and_requested_tier_survive_fallback_logging(usage_log):
    import asyncio
    original = {'model': 'tier-medium', 'metadata': {'session_id': 'one-call'}, 'messages': []}
    shaped = asyncio.run(usage_log.logger.async_pre_call_hook(None, None, original, 'completion'))
    assert shaped['metadata'][usage_log.RETURN_RAW_MODEL_NAME_METADATA_KEY] is True
    assert shaped['metadata']['team_requested_model'] == 'tier-medium'
    assert original['metadata'] == {'session_id': 'one-call'} and shaped['messages'] is original['messages']
    kwargs = {'litellm_params': {'metadata': {**shaped['metadata'], 'deployment_model_name': 'tier-complex'}},
              'standard_logging_object': {'model_id': 'tier-complex/selected', 'model': 'openai/catalog-model'}}
    usage_log.logger._write(kwargs, False)
    usage_log.logger._write(kwargs, True, {'id': 'response-1', 'model': 'provider/actual-model',
                                          'usage': {'prompt_tokens': 10, 'completion_tokens': 4}})
    with usage_log.connect() as conn:
        rows = conn.execute('SELECT "group",tier,routed_group,response_model,response_id,tokens_in,ok '
                            'FROM calls ORDER BY id').fetchall()
    assert rows == [('tier-medium', 'medium', 'tier-complex', None, None, None, 0),
                    ('tier-medium', 'medium', 'tier-complex', 'provider/actual-model', 'response-1', 10, 1)]


def test_auto_router_decision_is_kept_and_non_model_requests_are_not_rows(usage_log):
    decision = {"router_type": "complexity", "tier": "SIMPLE", "cause": "heuristic_scorer", "score": -0.1}
    kwargs = {"litellm_params": {"metadata": {"team_requested_model": "tier-auto", "deployment_model_name": "tier-simple"}},
              "standard_logging_object": {"model_id": "tier-simple/m", "metadata": {"routing_decision": decision}}}
    usage_log.logger._write(kwargs, True, {"usage": {"prompt_tokens": 1, "completion_tokens": 1}})
    # GET /v1/models without a key: refused before routing, no model, no deployment.
    usage_log.logger._write({"standard_logging_object": {"error_str": "No api key passed in."}}, False)
    with usage_log.connect() as conn:
        rows = conn.execute('SELECT "group",routed_group,route_json FROM calls').fetchall()
    assert len(rows) == 1 and rows[0][:2] == ("tier-auto", "tier-simple")
    assert json.loads(rows[0][2]) == decision


def test_tool_shape_of_request_and_answer_is_recorded(usage_log):
    kwargs = {"tools": [{"type": "function"}], "messages": [{"role": "user", "content": "do it"}],
              "litellm_params": {"metadata": {"team_requested_model": "tier-medium", "deployment_model_name": "tier-medium"}},
              "standard_logging_object": {"model_id": "tier-medium/coder"}}
    answer = {"choices": [{"finish_reason": "tool_calls", "message": {"tool_calls": [{"id": "1"}]}}]}
    usage_log.logger._write(kwargs, True, answer)
    talk = {**kwargs, "messages": [{"role": "tool", "content": "r"}]}
    usage_log.logger._write(talk, True, {"choices": [{"finish_reason": "stop", "message": {"content": "done"}}]})
    usage_log.logger._write(kwargs, False)
    with usage_log.connect() as conn:
        rows = conn.execute("SELECT had_tools, fresh_ask, tool_call, finish_reason FROM calls ORDER BY id").fetchall()
    assert rows == [(1, 1, 1, "tool_calls"), (1, 0, 0, "stop"), (1, 1, None, None)]
