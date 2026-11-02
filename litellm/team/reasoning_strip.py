"""Drop replayed reasoning from assistant turns before any provider sees them.

Hermes treats LiteLLM's loopback URL as a local server and replays every
assistant turn with both ``reasoning`` and ``reasoning_content`` (the same
text twice). Behind Portkey those bytes reach providers that reject them:
NVIDIA answers 400 ``duplicate field reasoning_content``, Mistral 422
``extra_forbidden``, Cohere 422 ``unknown field reasoning``. On 2026-11-02
that was 1,588 of the day's 2,452 requests. No catalog model needs the text
echoed back (the must-echo tier, DeepSeek and Kimi thinking, is not routed),
so the proxy removes the carriers once, for every caller, and the model's
own reasoning for the *current* turn is untouched: only history is shaped.

Measured before this hook (spike 2, 2026-11-02): the same failed worker
request replayed to ``laguna-xs-2.1`` and ``ministral-14b`` answered 400 /
null with the fields and 200 without them.
"""

from __future__ import annotations

CARRIERS = ("reasoning", "reasoning_content", "reasoning_details")


def strip_messages(messages: object) -> int:
    """Remove the reasoning carriers from every assistant message in place; returns how many were removed."""
    if not isinstance(messages, list):
        return 0
    removed = 0
    for m in messages:
        if isinstance(m, dict) and m.get("role") == "assistant":
            for key in CARRIERS:
                if key in m:
                    del m[key]
                    removed += 1
    return removed


try:  # the proxy has litellm; Team's test runner (Hermes's environment) does not
    from litellm.integrations.custom_logger import CustomLogger
except ImportError:  # pragma: no cover
    CustomLogger = object  # type: ignore[assignment,misc]


class ReasoningStrip(CustomLogger):  # type: ignore[misc]
    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
        if isinstance(data, dict):
            strip_messages(data.get("messages"))
        return data


strip = ReasoningStrip()
