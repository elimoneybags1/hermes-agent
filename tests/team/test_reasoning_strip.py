"""Reasoning carriers never reach a provider: the proxy hook and the Hermes middleware."""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _load(path: Path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


proxy_hook = _load(ROOT / "litellm" / "team" / "reasoning_strip.py")
middleware = _load(ROOT / "plugins" / "team" / "request_shape.py")


def _messages():
    return [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "u"},
        {"role": "assistant", "content": "", "reasoning": "r", "reasoning_content": "r",
         "tool_calls": [{"id": "1", "type": "function", "function": {"name": "f", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "1", "content": "ok"},
        {"role": "assistant", "content": "done", "reasoning_details": [{"type": "x"}]},
        {"role": "user", "content": "next", "reasoning": "a user key is not an assistant carrier"},
    ]


def test_proxy_hook_strips_assistant_carriers_in_place():
    msgs = _messages()
    assert proxy_hook.strip_messages(msgs) == 3
    assert all(k not in m for m in msgs if m["role"] == "assistant" for k in proxy_hook.CARRIERS)
    assert msgs[2]["tool_calls"][0]["id"] == "1"  # everything else untouched
    assert msgs[5]["reasoning"] == "a user key is not an assistant carrier"
    assert proxy_hook.strip_messages(msgs) == 0
    assert proxy_hook.strip_messages(None) == 0


def test_middleware_returns_a_shaped_copy_only_when_something_changed():
    request = {"model": "tier-medium", "messages": _messages()}
    result = middleware.llm_request(request=request, original_request=request, provider="litellm")
    assert result["source"] == "team" and "2 assistant turns" in result["reason"]
    shaped = result["request"]
    assert shaped is not request and request["messages"][2]["reasoning"] == "r"  # the input is not mutated
    assert all(k not in m for m in shaped["messages"] if m["role"] == "assistant" for k in middleware.CARRIERS)
    assert shaped["model"] == "tier-medium"
    assert middleware.llm_request(request={"messages": [{"role": "user", "content": "u"}]}) is None
    assert middleware.llm_request(request="not a dict") is None
