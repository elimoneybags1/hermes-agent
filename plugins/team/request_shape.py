"""``llm_request`` middleware: a Team profile never replays reasoning text.

Hermes decides which reasoning keys to echo on assistant turns from the
route; for a loopback URL (LiteLLM) it picks ``reasoning`` and
``reasoning_content``, the same text twice on every turn of every call. The
proxy strips them anyway (litellm/team/reasoning_strip.py), so sending them
only costs bytes: in one dumped worker request 8 of 20 messages carried the
text. Dropping them here keeps the ledger's input counts honest too.
"""

from __future__ import annotations

CARRIERS = ("reasoning", "reasoning_content", "reasoning_details")


def shape(request: dict) -> tuple[dict, int]:
    """Return a copy of the provider kwargs without reasoning carriers on assistant turns, and the count removed."""
    messages = request.get("messages")
    if not isinstance(messages, list):
        return request, 0
    removed = 0
    out = []
    for m in messages:
        if isinstance(m, dict) and m.get("role") == "assistant" and any(k in m for k in CARRIERS):
            m = {k: v for k, v in m.items() if k not in CARRIERS}
            removed += 1
        out.append(m)
    if not removed:
        return request, 0
    return {**request, "messages": out}, removed


def llm_request(**kwargs) -> dict | None:
    request = kwargs.get("request")
    if not isinstance(request, dict):
        return None
    shaped, removed = shape(request)
    if not removed:
        return None
    return {"request": shaped, "source": "team", "reason": f"dropped reasoning from {removed} assistant turns"}
