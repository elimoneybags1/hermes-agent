"""integrations/portkey/gateway.py (`gateway show | apply`) and the integrations/bin/lite wrapper.

usage.db is a temporary database with the real schema's columns; nothing touches
$HOME/state/team, and nothing restarts a service.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import sqlite3
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
CATALOG = ROOT / "integrations" / "portkey" / "catalog.json"


def _load():
    spec = importlib.util.spec_from_file_location("team_gateway", ROOT / "integrations" / "portkey" / "gateway.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gw = _load()


@pytest.fixture
def catalog() -> dict:
    return json.loads(CATALOG.read_text())


def _db(tmp_path: Path, rows: list[tuple]) -> Path:
    db = tmp_path / "usage.db"
    con = sqlite3.connect(db)
    con.execute("""CREATE TABLE calls (id INTEGER PRIMARY KEY, ts TEXT, model TEXT, ok INTEGER,
                   rate_limited INTEGER, latency_ms INTEGER, fresh_ask INTEGER, tool_call INTEGER, error TEXT)""")
    con.executemany("INSERT INTO calls (ts, model, ok, rate_limited, latency_ms, fresh_ask, tool_call, error) "
                    "VALUES (?,?,?,?,?,?,?,?)", rows)
    con.commit()
    con.close()
    return db


NOW = "2099-01-01T00:00:00"  # inside any window measured back from today


def test_stats_leave_rate_limits_out_of_the_success_rate(tmp_path):
    rows = ([(NOW, "openai/m", 1, 0, 1000, 1, 1, None)] * 3
            + [(NOW, "openai/m", 0, 1, None, 0, 0, "429")] * 5
            + [(NOW, "openai/m", 0, 0, None, 1, 0, "503 high demand")] * 3)
    s = gw.model_stats(_db(tmp_path, rows), hours=24)["m"]
    assert (s["calls"], s["ok"], s["rate_limited"]) == (11, 3, 5)
    assert gw.judged(s) == (3, 6)                      # 3 of the 6 attempts that were not rate-limited
    assert (s["fresh"], s["fresh_tool"]) == (6, 3)
    assert s["last_error"] == "503 high demand"
    assert gw.verdict(s) == ""                         # exactly 50% is not under the bar


def test_verdicts():
    assert gw.verdict(None) == "no record"
    few = {"calls": 3, "ok": 0, "rate_limited": 0}
    assert gw.verdict(few) == "few calls"
    weak = {"calls": 8, "ok": 1, "rate_limited": 0}
    assert gw.verdict(weak).startswith("WEAK")


def test_old_rows_fall_outside_the_window(tmp_path):
    db = _db(tmp_path, [("2000-01-01T00:00:00", "openai/m", 1, 0, 5, 0, 0, None)])
    assert gw.model_stats(db, hours=24) == {}


def test_changed_names_only_the_tiers_that_moved(catalog):
    new = copy.deepcopy(catalog)
    assert gw.changed(catalog, new) == ([], False)
    complex_models = [m for m in new["models"] if m.get("tier") == "complex"]
    complex_models[0]["order"], complex_models[1]["order"] = complex_models[1]["order"], complex_models[0]["order"]
    assert gw.changed(catalog, new) == (["tier-complex"], False)
    next(r for r in new["auto"] if r["group"] == "tier-work")["efforts"]["SIMPLE"] = "medium"
    assert gw.changed(catalog, new)[0] == ["tier-complex", "tier-work"]
    new["model_fallbacks"]["claude-opus-5-5"] = ["claude-sonnet-5-5", "claude-haiku-5-5"]
    assert gw.changed(catalog, new)[0] == ["tier-complex", "tier-auto", "tier-work"]
    new["policy"]["portkey"]["retry_attempts"] = 2
    assert gw.changed(catalog, new)[1] is True


def test_changed_reads_a_head_catalog_the_build_would_now_refuse(catalog):
    old = copy.deepcopy(catalog)
    m = next(m for m in old["models"] if m.get("tier") == "medium")
    m["size"] = "30B / 3B active"                      # the field build.py now refuses
    assert gw.changed(old, catalog) == ([], False)


def test_show_prints_every_tier_with_its_base_rate(catalog, capsys, monkeypatch, tmp_path):
    monkeypatch.setattr(gw, "usage_db", lambda: _db(tmp_path, [(NOW, "openai/north-mini-code", 1, 0, 3000, 1, 1, None)]))
    assert gw.show([]) == 0
    out = capsys.readouterr().out
    for t in catalog["tiers"]:
        assert f"tier-{t}   base rate:" in out
    assert "north-mini-code" in out and "30B/3B" in out


@pytest.mark.parametrize("args", [["login"], ["claude"], ["keys", "list"], ["models", "delete", "x"], ["up"]])
def test_lite_refuses_what_breaks_this_setup(args, tmp_path):
    r = subprocess.run([str(ROOT / "integrations" / "bin" / "lite"), *args], check=False, capture_output=True, text=True,
                       env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "TEAM_STATE": str(tmp_path)})
    assert r.returncode == 2 and "refused here" in r.stderr
