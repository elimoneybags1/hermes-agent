"""Team's Hermes plugin: the ledger sink and the boss's ``team_*`` tools.

Hermes loads this through ``plugins.enabled: [team]`` in a profile's
config.yaml. The ledger hooks observe only; the worker hook (worker_tools.py)
refuses calls that would re-read what a worker already has.
"""

from __future__ import annotations

from pathlib import Path

_HERE = Path(__file__).resolve().parent


def _sink():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "team_ledger_sink", _HERE / "ledger_sink.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _on_post_api_request(**kwargs) -> None:
    _sink().record(**kwargs)


def _on_api_request_error(**kwargs) -> None:
    kwargs.setdefault("response_model", None)
    kwargs["error"] = str(
        kwargs.get("error") or kwargs.get("exception") or "api request error"
    )
    _sink().record(**kwargs)


def _module(name: str):
    import importlib.util

    spec = importlib.util.spec_from_file_location(f"team_{name}", _HERE / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def register(ctx) -> None:
    ctx.register_hook("post_api_request", _on_post_api_request)
    ctx.register_hook("api_request_error", _on_api_request_error)
    ctx.register_hook("pre_tool_call", _module("worker_tools").pre_tool_call)
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "team_boss_tools", _HERE / "boss_tools.py"
    )
    tools = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tools)
    tools.register(ctx)
