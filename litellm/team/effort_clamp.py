"""Clamp ``reasoning_effort`` to what the chosen deployment accepts.

Hermes sends efforts from its own ladder (``none`` for auxiliary lanes with
thinking off, ``xhigh``/``max`` when asked). Groq's gpt-oss answers 400
"`reasoning_effort` must be one of `low`, `medium`, or `high`" to anything
else: on 2026-11-10, 111 of 237 calls to tier-simple/gpt-oss-120b failed this
way, each costing a fallback. A catalog row lists the efforts its model
accepts (``reasoning_efforts``); build.py writes it into the deployment's
model_info as ``team_reasoning_efforts``, and this hook moves an effort
outside that list to the nearest one on the ladder, after LiteLLM has picked
the deployment and before the request leaves. Rows without the list are
untouched.
"""

from __future__ import annotations

LADDER = ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")
INFO_KEY = "team_reasoning_efforts"


def clamp(effort: object, allowed: object) -> object:
    """The nearest allowed effort to ``effort``: up from below the list, down from above it."""
    if not isinstance(effort, str) or not isinstance(allowed, list) or not allowed or effort in allowed:
        return effort
    ranked = sorted((LADDER.index(a), a) for a in allowed if a in LADDER)
    if not ranked or effort not in LADDER:
        return effort
    at = LADDER.index(effort)
    below = [a for i, a in ranked if i < at]
    # Below the list: its lowest. Otherwise the nearest allowed value at or under the ask.
    return below[-1] if below else ranked[0][1]


def apply(kwargs: dict) -> dict:
    allowed = (kwargs.get("model_info") or {}).get(INFO_KEY)
    if not allowed:
        return kwargs
    if "reasoning_effort" in kwargs:
        kwargs["reasoning_effort"] = clamp(kwargs["reasoning_effort"], allowed)
    extra = kwargs.get("extra_body")
    if isinstance(extra, dict) and "reasoning_effort" in extra:
        extra["reasoning_effort"] = clamp(extra["reasoning_effort"], allowed)
    return kwargs


try:  # the proxy has litellm; Team's test runner (Hermes's environment) does not
    from litellm.integrations.custom_logger import CustomLogger
except ImportError:  # pragma: no cover
    CustomLogger = object  # type: ignore[assignment,misc]


class EffortClamp(CustomLogger):  # type: ignore[misc]
    async def async_pre_call_deployment_hook(self, kwargs, call_type):
        return apply(kwargs) if isinstance(kwargs, dict) else kwargs


clamp_hook = EffortClamp()
