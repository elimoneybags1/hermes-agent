"""integrations/dashboard/server.py: edits go through build.py, keys stay masked, requests are guarded.

Runs against a copy of the real catalog.json and fake .env files under the
per-test TEAM_STATE; restarts and launchd are stubbed, so nothing running is touched.
"""

from __future__ import annotations

import importlib.util
import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
SERVER = ROOT / "integrations" / "dashboard" / "server.py"
REAL_CATALOG = ROOT / "integrations" / "portkey" / "catalog.json"


def _load():
    spec = importlib.util.spec_from_file_location("team_dashboard", SERVER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


dash = _load()


@pytest.fixture
def setup(team_state, tmp_path, monkeypatch):
    catalog = json.loads(REAL_CATALOG.read_text())
    cat = tmp_path / "catalog.json"
    cat.write_text(dash.dump_catalog(catalog))
    monkeypatch.setattr(dash, "CATALOG", cat)
    keys = {p["key_env"]: f"key-for-{name}-0123456789" for name, p in catalog["providers"].items()}
    (team_state / "portkey").mkdir()
    (team_state / "portkey" / ".env").write_text("".join(f"{k}={v}\n" for k, v in keys.items()))
    (team_state / "litellm").mkdir()
    (team_state / "litellm" / ".env").write_text("LITELLM_MASTER_KEY=sk-master-abcdefghijklmnop\n")
    restarts = []
    monkeypatch.setattr(dash, "restart", lambda name, catalog, wait_s=90: restarts.append(name) or {"service": name, "healthy": True, "seconds": 0})
    monkeypatch.setattr(dash, "launchd", lambda label: {"loaded": True, "state": "running", "pid": "1"})
    monkeypatch.setattr(dash, "http_ok", lambda url: True)
    dash.build.write(team_state, dash.build.render(catalog, keys))
    return {"catalog": catalog, "path": cat, "keys": keys, "state": team_state, "restarts": restarts}


def test_mask_never_shows_the_middle_of_a_key():
    assert dash.mask("sk-or-v1-abcdefghijklmnop") == "sk-o…mnop"
    assert dash.mask("short") == "••••rt"
    assert dash.mask("") == ""
    assert dash.redact("auth sk-or-v1-abcdefghijklmnop failed", ["sk-or-v1-abcdefghijklmnop"]) == "auth sk-o…mnop failed"


def test_set_env_replaces_adds_and_removes_one_line_and_keeps_the_rest(tmp_path):
    p = tmp_path / ".env"
    p.write_text("# comment\nA_KEY=1\nB_KEY=2\n")
    dash.set_env(p, "A_KEY", "new")
    dash.set_env(p, "C_KEY", "3")
    dash.set_env(p, "B_KEY", "")
    assert p.read_text() == "# comment\nA_KEY=new\nC_KEY=3\n"
    assert oct(p.stat().st_mode & 0o777) == "0o600"
    for name, value in (("bad name", "x"), ("lower_key", "x"), ("A_KEY", "two words"), ("A_KEY", "a\nB=1")):
        with pytest.raises(dash.Problem):
            dash.set_env(p, name, value)


def test_a_refused_catalog_is_not_written(setup):
    bad = json.loads(setup["path"].read_text())
    next(m for m in bad["models"] if m.get("tier"))["served_by"][0]["free"] = "no"
    before = setup["path"].read_text()
    with pytest.raises(dash.Problem, match="free is 'no'"):
        dash.save_catalog(bad, dash.digest(before))
    assert setup["path"].read_text() == before
    with pytest.raises(dash.Problem, match="could not read"):
        dash.render({"tiers": []})


def test_save_writes_the_catalog_rebuilds_and_restarts_litellm_only_when_its_config_changed(setup):
    text = setup["path"].read_text()
    cat = json.loads(text)
    cat["policy"]["litellm"]["cooldown_time"] = 120
    r = dash.save_catalog(cat, dash.digest(text))
    assert r["saved"] and "litellm/config.yaml" in r["changes"]["changed"]
    assert setup["restarts"] == ["litellm"]
    assert json.loads(setup["path"].read_text())["policy"]["litellm"]["cooldown_time"] == 120
    assert "cooldown_time: 120" in (setup["state"] / "litellm" / "config.yaml").read_text()
    assert dash.drift()["ok"]
    cat["policy"]["portkey"]["retry_attempts"] = 2
    r = dash.save_catalog(cat, r["base"])
    assert "litellm/config.yaml" not in r["changes"]["changed"]
    assert setup["restarts"] == ["litellm"]


def test_save_refuses_when_the_file_changed_underneath(setup):
    cat = json.loads(setup["path"].read_text())
    with pytest.raises(dash.Problem) as e:
        dash.save_catalog(cat, "stale")
    assert e.value.status == 409


def test_keys_go_only_where_they_belong_and_rebuild_the_configs(setup):
    with pytest.raises(dash.Problem, match="only LITELLM_MASTER_KEY"):
        dash.save_key("litellm", "GROQ_API_KEY", "x" * 20)
    with pytest.raises(dash.Problem, match="belongs in LiteLLM"):
        dash.save_key("portkey", "LITELLM_MASTER_KEY", "x" * 20)
    r = dash.save_key("portkey", "GROQ_API_KEY", "gsk_new_key_value_123456")
    assert r["rebuilt"] and r["mask"] == "gsk_…3456"
    assert "gsk_new_key_value_123456" in (setup["state"] / "portkey" / "configs" / "team-probe-groq.json").read_text()
    assert setup["restarts"] == []
    r = dash.save_key("portkey", "GROQ_API_KEY", "")
    assert not r["rebuilt"] and "GROQ_API_KEY is not set" in r["refused"]
    dash.save_key("litellm", "LITELLM_MASTER_KEY", "sk-master-new-0000000000")
    assert setup["restarts"] == ["litellm"]


def test_linkage_names_every_broken_link(setup):
    st = setup["state"]
    (st / "portkey" / ".env").write_text(
        (st / "portkey" / ".env").read_text().replace("GROQ_API_KEY=", "OLD_GROQ=") + "LITELLM_MASTER_KEY=x\n"
    )
    (st / "litellm" / ".env").write_text("LITELLM_MASTER_KEY=sk-master-abcdefghijklmnop\nOPENAI_API_KEY=sk-x\n")
    prof = st / "hermes" / "profiles" / "implement"
    prof.mkdir(parents=True)
    (prof / ".env").write_text("LITELLM_MASTER_KEY=sk-other\n")
    lk = dash.linkage(json.loads(setup["path"].read_text()))
    text = "\n".join(lk["warnings"])
    assert "GROQ_API_KEY is not set" in text
    assert "holds OLD_GROQ" in text
    assert "Portkey's .env holds LITELLM_MASTER_KEY" in text
    assert "LiteLLM's .env holds OPENAI_API_KEY" in text
    assert "profile implement: LITELLM_MASTER_KEY differs" in text
    assert "cerebras: no routed model" in text
    groq = next(p for p in lk["providers"] if p["name"] == "groq")
    assert groq["key_set"] is False and groq["probe"] == "team-probe-groq"
    assert all(setup["keys"][p["key_env"]] not in json.dumps(lk) for p in lk["providers"] if p["key_env"] in setup["keys"])


def test_overview_carries_no_key(setup):
    body = json.dumps(dash.overview())
    for v in list(setup["keys"].values()) + ["sk-master-abcdefghijklmnop"]:
        assert v not in body


@pytest.fixture
def served(setup):
    dash.Handler.token = "tok"
    httpd = dash.ThreadingHTTPServer(("127.0.0.1", 0), dash.Handler)
    dash.Handler.port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield httpd.server_address[1]
    httpd.shutdown()


def _req(port, path, headers=None, body=None):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=json.dumps(body).encode() if body is not None else None,
        method="POST" if body is not None else "GET", headers=headers or {},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def test_http_guards_host_origin_and_token(served):
    port = served
    status, html = _req(port, "/")
    assert status == 200 and '"tok"' in html and "__TEAM_TOKEN__" not in html
    assert _req(port, "/api/overview")[0] == 403
    assert _req(port, "/api/overview", {"x-team-token": "nope"})[0] == 403
    assert _req(port, "/api/overview", {"x-team-token": "tok", "host": "evil.example:80"})[0] == 403
    assert _req(port, "/api/check", {"x-team-token": "tok", "origin": "http://evil.example"}, {"catalog": {}})[0] == 403
    status, body = _req(port, "/api/overview", {"x-team-token": "tok"})
    assert status == 200 and json.loads(body)["drift"]["ok"]


def test_http_check_reports_refusals_as_400(served, setup):
    cat = json.loads(setup["path"].read_text())
    status, body = _req(served, "/api/check", {"x-team-token": "tok", "content-type": "application/json"}, {"catalog": cat})
    assert status == 200 and json.loads(body)["changes"] == {"added": [], "changed": [], "removed": []}
    cat["blocked_providers"]["groq"] = "test"
    status, body = _req(served, "/api/check", {"x-team-token": "tok"}, {"catalog": cat})
    assert status == 400 and "groq" in json.loads(body)["error"]


def test_verify_tests_every_routed_pair_retries_once_and_saves(setup, monkeypatch):
    calls = []

    def fake(via, model, prompt, max_tokens=256):
        calls.append(model)
        if model.endswith("--groq"):
            return {"status": 429, "meaning": "rate limited", "error": "slow down", "seconds": 0.1}
        return {"status": 200, "answer": "pong", "answered_by": "m", "seconds": 0.1}

    monkeypatch.setattr(dash, "send_test", fake)
    monkeypatch.setattr(dash, "RETRY_WAIT_S", 0)
    cat = json.loads(setup["path"].read_text())
    rows = dash.verify_rows(cat, include_unavailable=False)
    assert rows and all(r["tier"] and not r["unavailable"] for r in rows)
    assert {r["config"] for r in rows} <= {p.stem for p in (setup["state"] / "portkey" / "configs").glob("team-pair-*.json")}
    dash.start_verify(False)
    for _ in range(200):
        if not dash.verify_status()["running"]:
            break
        threading.Event().wait(0.02)
    st = dash.verify_status()
    assert st["total"] == len(rows) == len(st["results"])
    groq = [r for r in st["results"] if r["provider"] == "groq"]
    assert groq and all(not r["ok"] and r["tries"] == 2 and r["first"]["status"] == 429 for r in groq)
    assert all(r["ok"] for r in st["results"] if r["provider"] != "groq")
    assert len(calls) == len(rows) + len(groq)
    assert json.loads(dash.verify_path().read_text())["total"] == len(rows)
    everything = dash.verify_rows(cat, include_unavailable=True)
    assert len(everything) > len(rows) and any(r["unavailable"] for r in everything)


def test_direct_claude_tests_are_refused_once_the_allowance_is_at_its_stop(setup, monkeypatch):
    import datetime as dt
    log = setup["state"] / "portkey" / "requests.jsonl"
    log.write_text(json.dumps({"time": dt.datetime.now(dt.UTC).isoformat(), "provider": "anthropic",
                               "provider_model": "claude-opus-5-5", "prompt_tokens": 25_000_000, "completion_tokens": 0}) + "\n")
    st = dash.spend_status()["anthropic"]
    assert st["over"] and st["spent_usd"] == pytest.approx(100.0)
    with pytest.raises(dash.Problem, match="allowance stop"):
        dash.send_test("portkey", "team-pair-claude-opus-5-5--anthropic", "hi")
    assert dash.overview()["spend"]["anthropic"]["over"]
