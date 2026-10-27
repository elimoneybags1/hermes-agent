import json

import pytest

from team import catalog

CATALOG = {
    "gateway": {"port": 8787},
    "litellm": {"port": 4000},
    "blocked_providers": {"anthropic": "paid key; Claude only via subscription"},
    "providers": {
        "openrouter": {"portkey": "openrouter", "key_env": "OPENROUTER_API_KEY"},
        "nvidia": {"portkey": "openai", "key_env": "NVIDIA_API_KEY"},
    },
    "tiers": ["simple", "medium", "complex", "reasoning"],
    "models": [
        {
            "id": "z-ai/glm-5.3",
            "tier": "reasoning",
            "order": 1,
            "served_by": [
                {"provider": "nvidia", "free": "yes", "why": "developer program, $0"},
                {"provider": "openrouter", "free": "yes", "why": ":free listing"},
            ],
        },
        {
            "id": "small-free",
            "tier": "simple",
            "order": 1,
            "served_by": [
                {
                    "provider": "openrouter",
                    "free": "allowance",
                    "why": "daily allowance",
                }
            ],
        },
        {
            "id": "gone",
            "tier": "medium",
            "order": 1,
            "unavailable": "provider retired it",
            "served_by": [{"provider": "openrouter", "free": "yes", "why": "x"}],
        },
        {
            "id": "paid-thing",
            "tier": "complex",
            "order": 1,
            "served_by": [
                {"provider": "openrouter", "free": "no", "why": "costs money"}
            ],
        },
        {
            "id": "claude-x",
            "tier": "complex",
            "order": 2,
            "served_by": [{"provider": "anthropic", "free": "no", "why": "paid"}],
        },
        {
            "id": "old-shape",
            "tier": "simple",
            "order": 2,
            "provider": "openrouter",
            "free": "yes",
            "why": "legacy",
        },
    ],
}


def test_resolve_tier_and_model_specs():
    c = catalog.resolve("tier-reasoning", CATALOG)
    assert (
        c.provider == "litellm"
        and c.model == "tier-reasoning"
        and "LiteLLM picks" in c.why
    )
    c = catalog.resolve("nvidia:z-ai/glm-5.3", CATALOG)
    assert (c.spec, c.provider, c.model) == (
        "nvidia:z-ai/glm-5.3",
        "litellm",
        "z-ai/glm-5.3",
    )
    assert c.as_dict()["why"].startswith("nvidia:")
    assert catalog.resolve("openrouter:small-free", CATALOG).model == "small-free"
    assert catalog.resolve("openrouter:old-shape", CATALOG).model == "old-shape"
    assert catalog.tiers(CATALOG) == ["simple", "medium", "complex", "reasoning"]


@pytest.mark.parametrize(
    "spec, message",
    [
        ("", "empty"),
        ("tier-ultra", "unknown tier"),
        ("tier-medium", "no available models"),
        ("glm", "must be tier-<t> or <provider>:<id>"),
        ("openrouter:nope", "not in the catalog"),
        ("openrouter:gone", "unavailable"),
        ("openrouter:paid-thing", "not free"),
        ("anthropic:claude-x", "blocked"),
        ("groq:z-ai/glm-5.3", "not served by 'groq'"),
    ],
)
def test_resolve_refuses(spec, message):
    with pytest.raises(catalog.GateError, match=message):
        catalog.resolve(spec, CATALOG)


def test_is_free_checks_every_provider_of_a_model():
    bad = json.loads(json.dumps(CATALOG))
    bad["models"][0]["served_by"][1]["provider"] = "mystery"
    ok, why = catalog.is_free(bad, "z-ai/glm-5.3")
    assert not ok and "mystery" in why
    ok, why = catalog.is_free(bad, "z-ai/glm-5.3", "nvidia")
    assert ok and why == "developer program, $0"


def test_load_reads_file_and_fails_closed(tmp_path):
    path = tmp_path / "catalog.json"
    with pytest.raises(catalog.GateError, match="unreadable"):
        catalog.load(path)
    path.write_text("{bad json")
    with pytest.raises(catalog.GateError, match="unreadable"):
        catalog.load(path)
    path.write_text(json.dumps({"tiers": [], "models": []}))
    with pytest.raises(catalog.GateError, match="providers"):
        catalog.load(path)
    path.write_text(json.dumps(CATALOG))
    assert catalog.load(path)["tiers"] == CATALOG["tiers"]
    assert catalog.default_path().name == "catalog.json"
    assert catalog.default_path().parts[-3:-1] == ("integrations", "portkey")
