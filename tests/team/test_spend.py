"""The allowance spend guard: build.py's checks, spend.py's totals, and the LiteLLM filter.

Runs against a copy of the real catalog.json and a fake requests.jsonl under the
per-test TEAM_STATE; LiteLLM itself is stubbed, so the guard's filter is tested alone.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
CATALOG = ROOT / "integrations" / "portkey" / "catalog.json"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


build = _load("portkey_build", ROOT / "integrations" / "portkey" / "build.py")
spend = _load("team_spend", ROOT / "integrations" / "portkey" / "spend.py")


@pytest.fixture
def catalog() -> dict:
    return json.loads(CATALOG.read_text())


@pytest.fixture
def keys(catalog) -> dict:
    return {p["key_env"]: f"key-for-{name}" for name, p in catalog["providers"].items()}


def write_state(state: Path, catalog: dict, keys: dict) -> None:
    build.write(state, build.render(catalog, keys))


def log(state: Path, *rows: dict) -> None:
    p = state / "portkey" / "requests.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a") as f:
        f.writelines(json.dumps(r) + "\n" for r in rows)


def row(model, tin, tout, provider="anthropic", time="2026-10-29T12:00:00.000Z"):
    return {"time": time, "provider": provider, "provider_model": model, "prompt_tokens": tin, "completion_tokens": tout}


def test_the_real_catalog_meters_claude_and_lists_every_config_that_reaches_it(catalog, keys):
    files = build.render(catalog, keys)
    a = json.loads(files["litellm/allowances.json"])["anthropic"]
    assert (a["monthly_usd"], a["stop_at_usd"], a["cycle_day"]) == (100, 90, 1)
    assert a["prices"]["claude-opus-5-5"] == {"input_per_mtok": 4.0, "output_per_mtok": 20.0}
    assert "team-pair-claude-opus-5-5--anthropic" in a["configs"]
    assert all(c in {p.rsplit("/", 1)[1][:-5] for p in files} for c in a["configs"])
    assert "anthropic" not in catalog["blocked_providers"]


@pytest.mark.parametrize(
    "edit,match",
    [
        (lambda c: c["providers"]["anthropic"]["allowance"].update(stop_at_usd=150), "stop_at_usd"),
        (lambda c: c["providers"]["anthropic"]["allowance"].update(cycle_day=31), "cycle_day"),
        (lambda c: c["providers"]["anthropic"]["allowance"].update(monthly_usd=0), "monthly_usd"),
        (lambda c: next(m for m in c["models"] if m["id"] == "claude-opus-5-5").pop("price"), "needs a price"),
        (lambda c: next(m for m in c["models"] if m["id"] == "claude-haiku-5-5")["price"].pop("long_input_per_mtok"), "or none"),
        (lambda c: next(m for m in c["models"] if m["id"] == "claude-opus-5-5")["served_by"].append(
            {"provider": "groq", "provider_model_id": "x", "free": "yes"}), "served by that provider alone"),
        (lambda c: next(m for m in c["models"] if m["id"] == "claude-opus-5-5")["served_by"][0].update(free="yes"), "free: allowance"),
    ],
)
def test_build_refuses_an_unmeterable_allowance(catalog, keys, edit, match):
    edit(catalog)
    with pytest.raises(build.Refusal, match=match):
        build.render(catalog, keys)


def test_cycle_start_and_cost():
    now = dt.datetime(2026, 10, 3, 9, tzinfo=dt.UTC)
    assert spend.cycle_start(1, now) == dt.datetime(2026, 10, 1, tzinfo=dt.UTC)
    assert spend.cycle_start(15, now) == dt.datetime(2026, 9, 15, tzinfo=dt.UTC)
    assert spend.cycle_start(15, dt.datetime(2026, 1, 3, tzinfo=dt.UTC)).month == 12
    haiku = {"input_per_mtok": 0.1, "output_per_mtok": 0.5, "long_prompt_over": 100000,
             "long_input_per_mtok": 0.5, "long_output_per_mtok": 2.5}
    assert spend.cost(haiku, 1_000_000, 0) == pytest.approx(0.5)  # over 100K: long rate
    assert spend.cost(haiku, 100_000, 1_000_000) == pytest.approx(0.01 + 0.5)


def test_ledger_totals_this_cycle_only_reads_new_lines_and_trips_the_stop(team_state, catalog, keys):
    write_state(team_state, catalog, keys)
    now = dt.datetime(2026, 10, 29, 13, tzinfo=dt.UTC)
    log(team_state,
        row("claude-opus-5-5", 1_000_000, 100_000),                       # 4 + 2 = $6
        row("claude-opus-5-5", 1_000_000, 0, time="2026-09-30T23:00:00Z"),  # last cycle
        row("gpt-oss-20b", 9_000_000, 9_000_000, provider="groq"),        # not metered
        {"time": "2026-10-29T12:01:00Z", "provider": "anthropic", "provider_model": "claude-opus-5-5"})  # failed, no tokens
    led = spend.Ledger(team_state)
    s = led.status(now)["anthropic"]
    assert s["spent_usd"] == pytest.approx(6.0) and s["requests"] == 1 and not s["over"]
    assert led.blocked_configs() == set()
    log(team_state, row("claude-fable-5-1", 1_000_000, 1_700_000))  # 10 + 85 = $95 more
    s = led.status(now)["anthropic"]
    assert s["spent_usd"] == pytest.approx(101.0) and s["over"]
    assert "team-pair-claude-opus-5-5--anthropic" in led.blocked_configs()
    log(team_state, row("claude-mystery", 1_000_000, 0))  # unpriced: counted at the highest price
    s = led.status(now)["anthropic"]
    assert s["unpriced"] == {"claude-mystery": 1} and s["spent_usd"] == pytest.approx(111.0)


def test_a_half_written_last_line_waits(team_state, catalog, keys):
    write_state(team_state, catalog, keys)
    p = team_state / "portkey" / "requests.jsonl"
    line = json.dumps(row("claude-opus-5-5", 1_000_000, 0))
    p.write_text(line[:20])
    led = spend.Ledger(team_state)
    now = dt.datetime(2026, 10, 29, 13, tzinfo=dt.UTC)
    assert led.status(now)["anthropic"]["requests"] == 0
    p.write_text(line + "\n")
    assert led.status(now)["anthropic"]["spent_usd"] == pytest.approx(4.0)


def test_the_litellm_guard_withholds_only_metered_deployments_when_over(team_state, catalog, keys, monkeypatch):
    stub = types.ModuleType("litellm.integrations.custom_logger")
    stub.CustomLogger = type("CustomLogger", (), {"__init__": lambda self: None})
    for name in ("litellm", "litellm.integrations"):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    monkeypatch.setitem(sys.modules, "litellm.integrations.custom_logger", stub)
    guard_mod = _load("team_spend_guard", ROOT / "integrations" / "litellm" / "spend_guard.py")
    catalog["models"][-2]["tier"], catalog["models"][-2]["order"] = "reasoning", 99  # route claude-opus-5-5
    write_state(team_state, catalog, keys)
    deps = [{"litellm_params": {"extra_headers": {"x-portkey-config": c}}}
            for c in ("team-model-claude-opus-5-5", "team-model-command-a-plus")]
    g = guard_mod.SpendGuard()
    run = lambda: asyncio.run(g.async_filter_deployments("tier-reasoning", deps, []))
    assert run() == deps
    log(team_state, row("claude-opus-5-5", 25_000_000, 0, time=dt.datetime.now(dt.UTC).isoformat()))  # $100
    assert [d["litellm_params"]["extra_headers"]["x-portkey-config"] for d in run()] == ["team-model-command-a-plus"]
