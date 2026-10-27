"""The locked base profile: nothing in it can reach a paid model."""

from pathlib import Path

import hermes_yaml as yaml

from team import profiles

BASE = Path(__file__).resolve().parents[2] / "team" / "roles" / "_base" / "config.yaml"

AUX_TASKS = (
    "vision",
    "compression",
    "skills_hub",
    "approval",
    "review",
    "mcp",
    "title_generation",
    "memory_query_rewrite",
    "tts_audio_tags",
    "voice_chat",
    "triage_specifier",
    "kanban_decomposer",
    "profile_describer",
    "goal_judge",
    "curator",
    "monitor",
    "background_review",
    "moa_reference",
    "moa_aggregator",
)


def test_base_config_is_locked():
    cfg = yaml.safe_load(BASE.read_text())
    assert profiles.audit(cfg) == []
    assert cfg["model"] == {"provider": "litellm", "default": "tier-medium"}
    assert set(cfg["providers"]) == {"litellm"}
    lite = cfg["providers"]["litellm"]
    assert (
        lite["base_url"] == "http://127.0.0.1:4000/v1"
        and lite["key_env"] == "LITELLM_MASTER_KEY"
    )
    assert "api_key" not in lite
    assert lite["session_affinity_header"] == "x-litellm-session-id"
    assert cfg["fallback_providers"] == []
    assert cfg["plugins"]["enabled"] == ["team"]
    assert cfg["auxiliary"]["free_only"] is True
    for task in AUX_TASKS:
        assert cfg["auxiliary"][task]["provider"] == "litellm", task
        assert cfg["auxiliary"][task]["model"].startswith("tier-"), task
    assert cfg["auxiliary"]["background_review"]["enabled"] is False
    assert cfg["curator"]["enabled"] is False and cfg["moa"]["active_preset"] == ""
    assert cfg["delegation"]["fallback_providers"] == []
    assert cfg["kanban"]["dispatch_in_gateway"] is False
    assert cfg["kanban"]["review_dispatch"] is False
    assert cfg["kanban"]["auto_decompose"] is False


def test_audit_catches_every_way_out():
    cfg = yaml.safe_load(BASE.read_text())
    cfg["model"]["provider"] = "auto"
    cfg["fallback_providers"] = [{"provider": "anthropic", "model": "claude"}]
    cfg["providers"]["openrouter"] = {
        "base_url": "https://openrouter.ai/api/v1",
        "api_key": "sk-or-x",
    }
    cfg["auxiliary"]["vision"]["provider"] = "auto"
    cfg["auxiliary"]["free_only"] = False
    cfg["auxiliary"]["background_review"]["enabled"] = True
    cfg["curator"]["enabled"] = True
    cfg["moa"]["active_preset"] = "default"
    cfg["delegation"]["provider"] = ""
    cfg["kanban"]["review_dispatch"] = True
    del cfg["auxiliary"]["goal_judge"]
    findings = profiles.audit(cfg)
    joined = "\n".join(findings)
    for needle in (
        "model.provider is 'auto'",
        "fallback_providers is not empty",
        "providers.openrouter is not one of",
        "providers.openrouter holds an api_key",
        "auxiliary.vision.provider is 'auto'",
        "auxiliary.free_only is not true",
        "auxiliary.background_review.enabled is not false",
        "curator.enabled is not false",
        "moa.active_preset is not empty",
        "delegation.provider is ''",
        "kanban.review_dispatch is not false",
        "auxiliary.goal_judge is missing",
    ):
        assert needle in joined, needle
    assert profiles.audit({}) and "model.provider is missing" in "\n".join(
        profiles.audit({})
    )


def test_audit_env_file_allows_only_loopback_secrets(tmp_path):
    env = tmp_path / ".env"
    env.write_text("SUBSCRIPTION_TOKEN=abc\nLITELLM_MASTER_KEY=sk-team\n# comment\n\n")
    assert profiles.audit_env(env) == []
    env.write_text(
        "OPENROUTER_API_KEY=sk-or\nANTHROPIC_API_KEY=sk-ant\nSUBSCRIPTION_TOKEN=x\n"
    )
    assert profiles.audit_env(env) == [
        ".env holds ANTHROPIC_API_KEY, which no Team profile may have",
        ".env holds OPENROUTER_API_KEY, which no Team profile may have",
    ]
    assert profiles.audit_env(tmp_path / "missing.env") == []
