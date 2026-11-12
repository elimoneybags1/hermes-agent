"""litellm/team/agent_gate.py: tool-carrying tier calls reach only capable, proven models."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

PATH = Path(__file__).resolve().parents[2] / "litellm" / "team" / "agent_gate.py"
spec = importlib.util.spec_from_file_location("agent_gate", PATH)
ag = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ag)

POL = {"window_hours": 72, "min_samples": 10, "min_ok_pct": 50, "min_fresh_tool_pct": 25}


def dep(slug, tools=True, group="tier-medium"):
    return {"model_info": {"id": f"{group}/{slug}", "supports_function_calling": tools}}


def rec(n=0, ok=0, limited=0, fresh=0, fresh_tool=0):
    return {"n": n, "ok": ok, "limited": limited, "fresh": fresh, "fresh_tool": fresh_tool}


def names(deps):
    return [ag.slug(d) for d in deps]


def test_without_tools_or_by_name_nothing_is_filtered():
    deps = [dep("a", tools=False), dep("b")]
    assert ag.gate("tier-medium", deps, False, {}, POL)[0] == deps
    assert ag.gate("gemini-2.5-flash", deps, True, {}, POL)[0] == deps


def test_only_tool_capable_models_take_tool_requests():
    kept, notes = ag.gate("tier-simple", [dep("glm", tools=None), dep("mini", tools=False), dep("gpt")], True, {}, POL)
    assert names(kept) == ["gpt"] and len(notes) == 2


def test_no_capable_model_empties_the_tier_so_litellm_falls_back():
    assert ag.gate("tier-simple", [dep("glm", tools=None)], True, {}, POL)[0] == []


def test_a_model_that_talks_instead_of_acting_is_dropped():
    recs = {"lite": rec(n=20, ok=20, fresh=12, fresh_tool=1), "coder": rec(n=20, ok=19, fresh=12, fresh_tool=9)}
    kept, notes = ag.gate("tier-medium", [dep("lite"), dep("coder")], True, recs, POL)
    assert names(kept) == ["coder"] and "1/12 fresh asks" in notes[0]


def test_failures_drop_a_model_but_rate_limits_and_thin_records_do_not():
    recs = {"flaky": rec(n=12, ok=3), "busy": rec(n=30, ok=5, limited=25), "new": rec(n=3, ok=0)}
    kept, _ = ag.gate("tier-medium", [dep("flaky"), dep("busy"), dep("new")], True, recs, POL)
    assert names(kept) == ["busy", "new"]


def test_the_record_narrows_but_never_empties():
    recs = {"a": rec(n=12, ok=1), "b": rec(n=12, ok=2)}
    kept, notes = ag.gate("tier-medium", [dep("a"), dep("b")], True, recs, POL)
    assert names(kept) == ["a", "b"] and "keeping them" in notes[-1]


def test_hook_reads_usage_db_and_policy_and_fails_open(tmp_path):
    (tmp_path / "agent_gate.json").write_text(json.dumps(POL))
    with sqlite3.connect(tmp_path / "usage.db") as conn:
        conn.execute('CREATE TABLE calls(id INTEGER PRIMARY KEY, ts TEXT, deployment TEXT, ok INTEGER, '
                     'rate_limited INTEGER, had_tools INTEGER, fresh_ask INTEGER, tool_call INTEGER)')
        conn.executemany('INSERT INTO calls(ts, deployment, ok, rate_limited, had_tools, fresh_ask, tool_call) '
                         'VALUES (datetime("now"), ?, 1, 0, 1, 1, ?)',
                         [("tier-simple/lite", 0)] * 10 + [("tier-medium/coder", 1)] * 10)
    conn.close()
    hook = ag.AgentGate(tmp_path)
    run = lambda kw, state=hook: asyncio.run(state.async_filter_deployments(
        "tier-medium", [dep("lite"), dep("coder")], [], request_kwargs=kw))
    assert names(run({"tools": [{"type": "function"}]})) == ["coder"]
    assert names(run({})) == ["lite", "coder"]
    broken = ag.AgentGate(tmp_path / "missing")
    assert names(run({"tools": [{}]}, broken)) == ["lite", "coder"]
