"""Stop the proxy from serving the moment any credit is spent.

Every routed model is free (build.py refuses anything else), so this is the second
wall: the OpenRouter key holds credit, and if its usage ever rises above the
baseline recorded the first time the proxy ran, the guard writes TRIPPED and
refuses every request until a person looks.

Reset after checking what spent it:
    rm ~/state/team/litellm/TRIPPED ~/state/team/litellm/openrouter-baseline.json
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

import httpx
from fastapi import HTTPException
from litellm.integrations.custom_logger import CustomLogger

STATE = Path.home() / "state" / "team" / "litellm"
TRIPPED = STATE / "TRIPPED"
BASELINE = STATE / "openrouter-baseline.json"
CHECK_EVERY_S = 120
log = logging.getLogger("team.spend_guard")


def _openrouter_usage() -> float:
    r = httpx.get(
        "https://openrouter.ai/api/v1/key",
        headers={"Authorization": f"Bearer {os.environ['OPENROUTER_API_KEY']}"},
        timeout=10,
    )
    r.raise_for_status()
    return float(r.json()["data"]["usage"])


class SpendGuard(CustomLogger):
    def __init__(self) -> None:
        super().__init__()
        self._checked = 0.0
        self.check()

    def check(self) -> None:
        self._checked = time.time()
        try:
            usage = _openrouter_usage()
        except Exception as e:  # an outage must not block free providers
            log.warning("spend_guard: could not read OpenRouter usage: %s", e)
            return
        if not BASELINE.exists():
            BASELINE.write_text(json.dumps({"usage": usage, "recorded": time.strftime("%Y-%m-%dT%H:%M:%S%z")}))
            return
        base = json.loads(BASELINE.read_text())["usage"]
        if usage > base + 1e-9 and not TRIPPED.exists():
            TRIPPED.write_text(json.dumps({
                "why": "OpenRouter credit usage rose above the baseline",
                "baseline": base, "usage": usage, "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            }))
            log.error("spend_guard: TRIPPED: OpenRouter usage %s > baseline %s", usage, base)

    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
        if time.time() - self._checked > CHECK_EVERY_S:
            self.check()
        if TRIPPED.exists():
            raise HTTPException(status_code=503, detail=f"spend_guard tripped: {TRIPPED.read_text()}")
        return data


guard = SpendGuard()
