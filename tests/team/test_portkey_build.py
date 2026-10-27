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
    assert sorted(tiers) == sorted(f"portkey/configs/team-tier-{t}.json" for t in catalog["tiers"])
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
    catalog["providers"]["anthropic"] = {"portkey": "anthropic", "key_env": "ANTHROPIC_API_KEY"}
    m = first_routed(catalog)
    m["served_by"].append({"provider": "anthropic", "provider_model_id": "claude-x", "free": "yes"})
    with pytest.raises(build.Refusal, match="anthropic is blocked"):
        build.render(catalog, keys)


def test_refuses_an_unknown_provider(catalog, keys):
    m = first_routed(catalog)
    m["served_by"].append({"provider": "mystery", "provider_model_id": "x", "free": "yes"})
    with pytest.raises(build.Refusal, match="unknown provider mystery"):
        build.render(catalog, keys)


def test_refuses_a_non_free_openrouter_id(catalog, keys):
    m = first_routed(catalog)
    m["served_by"].append({"provider": "openrouter", "provider_model_id": "vendor/model", "free": "yes"})
    with pytest.raises(build.Refusal, match="not a ':free' variant"):
        build.render(catalog, keys)


def test_refuses_an_oversized_config(catalog, keys):
    m = first_routed(catalog)
    keys[catalog["providers"][m["served_by"][0]["provider"]]["key_env"]] = "k" * (build.MAX_CONFIG_BYTES + 1)
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


def test_check_passes_after_a_build_and_fails_after_a_hand_edit(catalog, keys, monkeypatch, tmp_path):
    state = Path(os.environ["TEAM_STATE"])
    (state / "portkey").mkdir(parents=True)
    (state / "portkey" / ".env").write_text("".join(f"{k}={v}\n" for k, v in keys.items()))
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
