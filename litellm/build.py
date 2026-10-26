"""Generate config.yaml from free-models.json, refusing anything that is not free.

    build.py           write config.yaml
    build.py --check   exit 1 unless config.yaml is exactly what free-models.json generates

Run with LiteLLM's own interpreter (it has PyYAML): run.sh does `build.py --check`
before starting the proxy, so a hand-edited or stale config never serves.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
CATALOG = HERE / "free-models.json"
CONFIG = HERE / "config.yaml"

FREE = {"yes", "allowance"}
TIERS = ("SIMPLE", "MEDIUM", "COMPLEX", "REASONING")
PREFIX = {
    "openrouter": "openrouter",
    "nvidia": "nvidia_nim",
    "gemini": "gemini",
    "mistral": "mistral",
    "groq": "groq",
    "zai": "zai",
    "cohere": "cohere_chat",
    "cerebras": "cerebras",
}
# When a whole tier is rate-limited or failing, try these groups next. All are free.
FALLBACKS = {
    "REASONING": ["COMPLEX", "MEDIUM"],
    "COMPLEX": ["REASONING", "MEDIUM"],
    "MEDIUM": ["COMPLEX", "SIMPLE"],
    "SIMPLE": ["MEDIUM"],
}


def group(tier: str) -> str:
    return f"tier-{tier.lower()}"


def refuse(msg: str) -> None:
    sys.exit(f"build.py: refusing: {msg}")


def deployments(catalog: dict) -> list[dict]:
    blocked = catalog["blocked_providers"]
    providers = catalog["providers"]
    out = []
    for m in catalog["models"]:
        if m.get("tier") is None:
            continue
        name = f"{m['provider']}:{m['id']}"
        if m["provider"] in blocked:
            refuse(f"{name}: provider is blocked ({blocked[m['provider']]})")
        if m["provider"] not in providers or m["provider"] not in PREFIX:
            refuse(f"{name}: unknown provider")
        if m.get("free") not in FREE:
            refuse(f"{name}: free is {m.get('free')!r}")
        if m["tier"] not in TIERS:
            refuse(f"{name}: unknown tier {m['tier']!r}")
        if m["provider"] == "openrouter" and not m["id"].endswith(":free"):
            refuse(f"{name}: OpenRouter models must be ':free' variants")
        p = providers[m["provider"]]
        params = {
            "model": f"{PREFIX[m['provider']]}/{m['id']}",
            "api_key": f"os.environ/{p['key']}",
            # A provider having a bad stretch moves down its tiers without editing every model.
            "order": int(m["order"]) + int(p.get("order_offset", 0)),
            # A hung free endpoint (seen on NVIDIA) must fail over, not stall a worker.
            "timeout": 90,
            "stream_timeout": 45,
        }
        if p.get("api_base"):
            params["api_base"] = p["api_base"]
        info = {"id": name}
        if m.get("max_input") or m.get("context"):
            info["max_input_tokens"] = m.get("max_input") or m["context"]
        if m.get("max_output"):
            info["max_output_tokens"] = m["max_output"]
        # Free deployments cost nothing; keeps LiteLLM's spend log honest.
        info["input_cost_per_token"] = 0
        info["output_cost_per_token"] = 0
        # New models are missing from LiteLLM's built-in map, and its pre-call checks then
        # drop them from tool-calling requests; declare what the catalog knows.
        if m.get("tools") is not None:
            info["supports_function_calling"] = info["supports_tool_choice"] = bool(m["tools"])
        if m.get("vision") is not None:
            info["supports_vision"] = bool(m["vision"])
        if m.get("reasoning") is not None:
            info["supports_reasoning"] = bool(m["reasoning"])
        out.append({"model_name": group(m["tier"]), "litellm_params": params, "model_info": info})
    for tier in TIERS:
        if not any(d["model_name"] == group(tier) for d in out):
            refuse(f"tier {tier} has no models")
    return out


def config(catalog: dict) -> dict:
    return {
        "model_list": deployments(catalog) + [{
            "model_name": "team-auto",
            "litellm_params": {
                "model": "auto_router/complexity_router",
                "complexity_router_default_model": group("MEDIUM"),
                "complexity_router_config": {
                    "tiers": {t: group(t) for t in TIERS},
                    # Heuristic scorer: no model call to classify, so routing spends no quota.
                    "classifier_type": "heuristic",
                    # Pick on each new instruction; keep that model through its tool loop.
                    # Needs the x-litellm-session-id header from the client.
                    "classification_mode": "user_turn",
                    "session_affinity_ttl_seconds": 3600,
                    "route_housekeeping_to_cheapest_tier": True,
                    # Hermes compresses its own context; don't do it twice.
                    "context_compaction": False,
                    "return_raw_model_name": True,
                },
            },
        }],
        "router_settings": {
            # Lowest 'order' first; a deployment that 429s or fails rests, then the next order serves.
            "enable_pre_call_checks": True,
            # A session (Team job, by x-litellm-session-id) keeps one model within its tier
            # unless that model fails or cools down.
            "optional_pre_call_checks": ["session_affinity"],
            # Bounded: retries x timeout x fallbacks once held a worker for 9.5 minutes.
            "num_retries": 1,
            "max_fallbacks": 2,
            "allowed_fails": 1,
            "cooldown_time": 300,
            "fallbacks": [{group(t): [group(f) for f in FALLBACKS[t]]} for t in TIERS],
            "context_window_fallbacks": [],
        },
        "litellm_settings": {
            "drop_params": True,
            "telemetry": False,
            "callbacks": ["spend_guard.guard", "usage_log.logger"],
        },
        "general_settings": {"master_key": "os.environ/LITELLM_MASTER_KEY"},
    }


def render(catalog: dict) -> str:
    head = "# GENERATED by build.py from free-models.json. Edit that file, then run build.py.\n"
    return head + yaml.safe_dump(config(catalog), sort_keys=False, width=120)


def main() -> None:
    text = render(json.loads(CATALOG.read_text()))
    if "--check" in sys.argv[1:]:
        if not CONFIG.exists() or CONFIG.read_text() != text:
            refuse("config.yaml is not what free-models.json generates; run build.py")
        return
    CONFIG.write_text(text)
    print(f"wrote {CONFIG}")


if __name__ == "__main__":
    main()
