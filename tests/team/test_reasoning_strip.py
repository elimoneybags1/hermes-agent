"""Reasoning carriers never reach a provider: the LiteLLM proxy hook strips them."""

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
