"""Write every model call a Hermes profile makes into Team's ledger.

Hermes's ``post_api_request`` hook is observe-only, so this cannot refuse a
call; it records what happened, and marks a call that reached anything
outside the catalog as a violation so ``team run`` blocks the task. It never
raises out of the hook: a broken sink must not kill a worker.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from team import catalog, ledger

LOOPBACK_PREFIXES = ("http://127.0.0.1:", "http://localhost:")
ALLOWED_PROVIDERS = ("litellm", "subscription")


def _int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _usage(usage: Any) -> tuple[int | None, int | None]:
    if not isinstance(usage, dict):
        return None, None
    tokens_in = usage.get("prompt_tokens", usage.get("input_tokens"))
    tokens_out = usage.get("completion_tokens", usage.get("output_tokens"))
    return _int(tokens_in), _int(tokens_out)


def classify(
    provider: str | None,
    base_url: str | None,
    model: str | None,
    response_model: str | None,
    cat: dict | None,
) -> tuple[str | None, str | None]:
    """(violation kind, detail) for a call outside the catalog, or (None, None)."""
    provider = provider or ""
    base_url = base_url or ""
    if provider not in ALLOWED_PROVIDERS or not base_url.startswith(LOOPBACK_PREFIXES):
        return (
            "off-gateway",
            f"call went to provider {provider!r} at {base_url!r}, not the loopback gateway",
        )
    if provider == "subscription" or cat is None:
        return None, None
    answered = response_model or model or ""
    if answered.startswith("tier-"):
        return None, None
    ok, why = catalog.is_free(cat, answered)
    if ok:
        return None, None
    # LiteLLM may answer with the provider's own id (e.g. "z-ai/glm-5.3"); accept a served_by match.
    for m in cat.get("models", []):
        for s in m.get("served_by") or []:
            if s.get("provider_model_id") == answered:
                ok, why = catalog.is_free(cat, m["id"], s.get("provider"))
                return (None, None) if ok else ("non-catalog", why)
    return "non-catalog", why


def record(**kw: Any) -> None:
    """The hook body. Takes the keyword payload Hermes passes to ``post_api_request``."""
    try:
        conn = ledger.connect()
        ledger.init(conn)
    except (OSError, sqlite3.Error):
        return
    try:
        task_ref = os.environ.get("HERMES_KANBAN_TASK") or None
        profile = os.environ.get("HERMES_PROFILE") or None
        provider = kw.get("provider")
        base_url = kw.get("base_url")
        model = kw.get("model")
        response_model = kw.get("response_model")
        tokens_in, tokens_out = _usage(kw.get("usage"))
        duration = kw.get("api_duration")
        latency_ms = int(float(duration) * 1000) if duration is not None else None
        try:
            cat = catalog.load()
        except catalog.GateError:
            cat = None
        kind, detail = classify(provider, base_url, model, response_model, cat)
        job = ledger.open_job_for_task(conn, task_ref) if task_ref else None
        ledger.record_call(
            conn,
            job["id"] if job else None,
            provider or "unknown",
            model or "unknown",
            profile or "unknown",
            tokens_in,
            tokens_out,
            latency_ms,
            kind is None,
            detail,
            api_calls=_int(kw.get("api_call_count")),
            task_ref=task_ref,
            session_id=kw.get("session_id"),
            profile=profile,
            response_model=response_model,
            base_url=base_url,
        )
        if kind:
            ledger.record_violation(conn, kind, detail or kind, task_ref=task_ref)
    except Exception as e:  # the hook must never raise into the agent loop
        _log(f"ledger sink failed: {e}")
    finally:
        conn.close()


def _log(msg: str) -> None:
    try:
        path = ledger.state_dir() / "logs" / "plugin.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as f:
            f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {msg}\n")
    except OSError:
        pass
