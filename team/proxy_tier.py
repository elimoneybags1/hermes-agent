"""Which LiteLLM tier a Team task should run on.

Hermes's kanban runner opens every worker with "work kanban task <id>", so the
proxy's own classifier only ever sees five words. Team has the goal text, so it
asks the running proxy to classify that instead (/auto_router/test_routing: the
same heuristic, no model call) and dispatches the task on the tier group directly.
"""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "litellm" / "config.yaml"
ENV = Path.home() / "state" / "team" / "litellm" / ".env"
URL = "http://127.0.0.1:4000/auto_router/test_routing"
TIERS = ("SIMPLE", "MEDIUM", "COMPLEX", "REASONING")
# Coding work never starts below MEDIUM; SIMPLE is for Hermes's own side jobs.
FLOOR = "MEDIUM"


def group(tier: str) -> str:
    return f"tier-{tier.lower()}"


def _router_config() -> dict:
    cfg = yaml.safe_load(CONFIG.read_text())
    auto = next(m for m in cfg["model_list"] if m["model_name"] == "team-auto")
    return auto["litellm_params"]["complexity_router_config"]


def _key() -> str:
    for line in ENV.read_text().splitlines():
        if line.startswith("LITELLM_MASTER_KEY="):
            return line.split("=", 1)[1].strip()
    raise KeyError("LITELLM_MASTER_KEY")


def classify(text: str, timeout: float = 10) -> str:
    """The tier the proxy's classifier gives `text` (SIMPLE..REASONING)."""
    body = json.dumps({"prompt": text, "complexity_router_config": _router_config()}).encode()
    req = urllib.request.Request(URL, data=body, method="POST", headers={
        "Authorization": f"Bearer {_key()}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        tier = json.load(r)["routing_decision"]["tier"]
    if tier not in TIERS:
        raise ValueError(f"unknown tier {tier!r}")
    return tier


def for_task(text: str, attempt: int = 1, classify=classify) -> tuple[str, str]:
    """(tier group, why) for a task: its classified tier, at least FLOOR, one tier
    higher for each retry, capped at the top. Falls back to FLOOR if the proxy
    cannot classify; every tier is free, so the fallback is only a quality choice."""
    try:
        tier, why = classify(text), "classified"
    except Exception as e:
        tier, why = FLOOR, f"classifier unavailable ({type(e).__name__})"
    level = max(TIERS.index(tier), TIERS.index(FLOOR)) + max(attempt - 1, 0)
    chosen = TIERS[min(level, len(TIERS) - 1)]
    if attempt > 1:
        why += f", +{attempt - 1} for retry"
    return group(chosen), f"{why}: {tier} -> {chosen}"
