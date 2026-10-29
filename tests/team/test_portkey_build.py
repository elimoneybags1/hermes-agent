"""integrations/portkey/build.py: refuses anything not free, and its outputs are deterministic.

Runs against a copy of the real catalog.json and a fake .env under the per-test
TEAM_STATE, so nothing touches $HOME/state/team.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
BUILD = ROOT / "integrations" / "portkey" / "build.py"
CATALOG = ROOT / "integrations" / "portkey" / "catalog.json"


def _load_build():
    spec = importlib.util.spec_from_file_location("portkey_build", BUILD)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


build = _load_build()


@pytest.fixture
def catalog() -> dict:
    return json.loads(CATALOG.read_text())


@pytest.fixture
def keys(catalog) -> dict:
    return {p["key_env"]: f"key-for-{name}" for name, p in catalog["providers"].items()}


def first_routed(catalog: dict) -> dict:
    return next(m for m in catalog["models"] if m.get("tier"))


def test_the_real_catalog_renders_and_every_portkey_config_is_small(catalog, keys):
    files = build.render(catalog, keys)
    assert "litellm/config.yaml" in files
    tiers = [f for f in files if "/team-tier-" in f]
    assert sorted(tiers) == sorted(
        f"portkey/configs/team-tier-{t}.json" for t in catalog["tiers"]
    )
    assert len([f for f in files if "/team-model-" in f]) == len(build.routed(catalog))
    for path, text in files.items():
        if path.startswith("portkey/configs/"):
            assert len(text.encode()) <= build.MAX_CONFIG_BYTES, path


def test_refuses_free_no(catalog, keys):
    m = first_routed(catalog)
    m["served_by"][0]["free"] = "no"
    with pytest.raises(build.Refusal, match="free is 'no'"):
        build.render(catalog, keys)


def test_refuses_a_blocked_provider(catalog, keys):
    catalog["providers"]["anthropic"] = {
        "portkey": "anthropic",
        "key_env": "ANTHROPIC_API_KEY",
    }
    m = first_routed(catalog)
    m["served_by"].append(
        {"provider": "anthropic", "provider_model_id": "claude-x", "free": "yes"}
    )
    with pytest.raises(build.Refusal, match="anthropic is blocked"):
        build.render(catalog, keys)


def test_refuses_an_unknown_provider(catalog, keys):
    m = first_routed(catalog)
    m["served_by"].append(
        {"provider": "mystery", "provider_model_id": "x", "free": "yes"}
    )
    with pytest.raises(build.Refusal, match="unknown provider mystery"):
        build.render(catalog, keys)


def test_refuses_a_non_free_openrouter_id(catalog, keys):
    m = first_routed(catalog)
    m["served_by"].append(
        {"provider": "openrouter", "provider_model_id": "vendor/model", "free": "yes"}
    )
    with pytest.raises(build.Refusal, match="not a ':free' variant"):
        build.render(catalog, keys)


def test_refuses_an_oversized_config(catalog, keys):
    m = first_routed(catalog)
    keys[catalog["providers"][m["served_by"][0]["provider"]]["key_env"]] = "k" * (
        build.MAX_CONFIG_BYTES + 1
    )
    with pytest.raises(build.Refusal, match="bytes, over"):
        build.render(catalog, keys)


def test_refuses_an_empty_tier(catalog, keys):
    for m in catalog["models"]:
        if m.get("tier") == "reasoning":
            m["tier"] = "complex"
    with pytest.raises(build.Refusal, match="tier reasoning has no models"):
        build.render(catalog, keys)


def test_refuses_a_routed_model_with_no_live_provider(catalog, keys):
    m = first_routed(catalog)
    for s in m["served_by"]:
        s["unavailable"] = "gone"
    with pytest.raises(build.Refusal, match="every provider is marked unavailable"):
        build.render(catalog, keys)


def test_refuses_a_missing_key(catalog):
    with pytest.raises(build.Refusal, match="is not set in Portkey's .env"):
        build.render(catalog, {})


def test_refuses_duplicate_ids(catalog, keys):
    catalog["models"].append(copy.deepcopy(first_routed(catalog)))
    with pytest.raises(build.Refusal, match="duplicate model id"):
        build.render(catalog, keys)


def test_outputs_are_deterministic_and_keys_only_reach_portkey(catalog, keys):
    a = build.render(copy.deepcopy(catalog), dict(keys))
    b = build.render(copy.deepcopy(catalog), dict(keys))
    assert a == b
    yaml = a["litellm/config.yaml"]
    for v in keys.values():
        assert v not in yaml
    assert "x-portkey-config: team-model-" in yaml
    assert "master_key: os.environ/LITELLM_MASTER_KEY" in yaml
    model_cfg = json.loads(next(t for p, t in a.items() if "/team-model-" in p))
    assert model_cfg["strategy"]["mode"] == "fallback"
    assert all(t["api_key"].startswith("key-for-") for t in model_cfg["targets"])


def test_check_passes_after_a_build_and_fails_after_a_hand_edit(
    catalog, keys, monkeypatch, tmp_path
):
    state = Path(os.environ["TEAM_STATE"])
    (state / "portkey").mkdir(parents=True)
    (state / "portkey" / ".env").write_text(
        "".join(f"{k}={v}\n" for k, v in keys.items())
    )
    monkeypatch.setattr(build, "CATALOG", CATALOG)
    assert build.main([]) == 0
    assert build.main(["--check"]) == 0
    assert oct((state / "litellm" / "config.yaml").stat().st_mode & 0o777) == "0o600"
    cfg = next((state / "portkey" / "configs").glob("team-model-*.json"))
    cfg.write_text(cfg.read_text() + "\n")
    with pytest.raises(build.Refusal, match="differs"):
        build.main(["--check"])
    build.main([])
    stale = state / "portkey" / "configs" / "team-model-stale.json"
    stale.write_text("{}")
    with pytest.raises(build.Refusal, match="stale"):
        build.main(["--check"])
    build.main([])
    assert not stale.exists()
    assert build.main(["--check"]) == 0


def test_policy_values_reach_both_proxies(catalog, keys):
    catalog["policy"]["portkey"].update(
        {"on_status_codes": [429, 503], "retry_attempts": 2, "retry_on_status_codes": [502], "default_timeout_ms": 5000}
    )
    catalog["policy"]["litellm"].update(
        {"routing_strategy": "least-busy", "num_retries": 3, "cooldown_time": 30,
         "context_window_fallbacks": {"simple": ["reasoning"]}, "drop_params": False}
    )
    for p in catalog["providers"].values():
        p.pop("request_timeout_ms", None)
    files = build.render(catalog, keys)
    model_cfg = json.loads(next(t for p, t in files.items() if "/team-model-" in p))
    assert model_cfg["strategy"] == {"mode": "fallback", "on_status_codes": [429, 503]}
    t = model_cfg["targets"][0]
    assert t["retry"] == {"attempts": 2, "on_status_codes": [502]}
    assert t["request_timeout"] == 5000
    yaml = files["litellm/config.yaml"]
    assert "routing_strategy: least-busy" in yaml
    assert "num_retries: 3" in yaml
    assert "cooldown_time: 30" in yaml
    assert "- tier-simple:\n    - tier-reasoning" in yaml
    assert "drop_params: false" in yaml


def test_loadbalance_spreads_by_weight_without_failover(catalog, keys):
    catalog["policy"]["portkey"]["mode"] = "loadbalance"
    m = next(m for m in catalog["models"] if m.get("tier") and len(m["served_by"]) > 1)
    m["served_by"][0]["weight"] = 3
    files = build.render(catalog, keys)
    cfg = json.loads(files[f"portkey/configs/team-model-{build.slug(m['id'])}.json"])
    assert cfg["strategy"] == {"mode": "loadbalance"}
    assert cfg["targets"][0]["weight"] == 3
    assert all(t["weight"] == 1 for t in cfg["targets"][1:])
    tier = json.loads(files[f"portkey/configs/team-tier-{m['tier']}.json"])
    assert tier["strategy"]["mode"] == "fallback"


@pytest.mark.parametrize(
    "section,key,value,match",
    [
        ("portkey", "mode", "conditional", "mode must be one of"),
        ("portkey", "on_status_codes", [], "non-empty list"),
        ("portkey", "on_status_codes", [200], "non-empty list"),
        ("portkey", "retry_attempts", -1, "integer >= 0"),
        ("portkey", "retry_attempts", True, "integer >= 0"),
        ("portkey", "default_timeout_ms", 10, "integer >= 1000"),
        ("litellm", "routing_strategy", "cost-based-routing", "routing_strategy"),
        ("litellm", "num_retries", "1", "integer >= 0"),
        ("litellm", "drop_params", 1, "true or false"),
        ("litellm", "session_pin_ttl_s", 0, "integer >= 1"),
        ("litellm", "context_window_fallbacks", {"simple": ["simple"]}, "context_window_fallbacks.simple"),
        ("litellm", "context_window_fallbacks", {"huge": ["simple"]}, "context_window_fallbacks.huge"),
    ],
)
def test_refuses_a_bad_policy_value(catalog, keys, section, key, value, match):
    catalog["policy"][section][key] = value
    with pytest.raises(build.Refusal, match=match):
        build.render(catalog, keys)


def test_refuses_a_missing_policy_and_a_bad_weight(catalog, keys):
    bad = copy.deepcopy(catalog)
    del bad["policy"]
    with pytest.raises(build.Refusal, match="policy"):
        build.render(bad, keys)
    first_routed(catalog)["served_by"][0]["weight"] = 0
    with pytest.raises(build.Refusal, match="weight"):
        build.render(catalog, keys)


def test_one_probe_per_provider_with_that_provider_alone(catalog, keys):
    files = build.render(catalog, keys)
    probes = {p: json.loads(t) for p, t in files.items() if "/team-probe-" in p}
    served = {s["provider"] for m in build.routed(catalog) for s in m["live"]}
    assert sorted(probes) == sorted(f"portkey/configs/team-probe-{p}.json" for p in served)
    for name, cfg in probes.items():
        provider = name.rsplit("team-probe-", 1)[1].removesuffix(".json")
        assert cfg["provider"] == catalog["providers"][provider]["portkey"]
        assert cfg["api_key"] == f"key-for-{provider}"
        assert "targets" not in cfg
