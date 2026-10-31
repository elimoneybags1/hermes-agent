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
    path = Path(__file__).resolve().parents[2] / "integrations/litellm/usage_log.py"
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
