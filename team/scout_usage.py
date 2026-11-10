"""Attribute tool-free scout calls without counting cache hits as model requests."""
from __future__ import annotations

import time

from team import ledger
from team.review.backend import ScoutError


def tracked_call(target, role, model, prompt, call):
    started = time.monotonic()
    result, error = {}, None
    try:
        result = call(model, prompt)
        return result
    except ScoutError as exc:
        result = {"usage": exc.usage, "model": exc.response_model, "gateway_session_id": exc.gateway_session_id}
        error = str(exc)
        raise
    finally:
        usage = result.get("usage") or {}
        details = usage.get("prompt_tokens_details") or {}
        with ledger.connect() as conn:
            ledger.init(conn)
            ledger.record_call(
                conn, None, "litellm", model, role,
                usage.get("prompt_tokens"), usage.get("completion_tokens"),
                int((time.monotonic() - started) * 1000), bool(result.get("text")),
                error=error, response_model=result.get("model"), target=target,
                gateway_session_id=result.get("gateway_session_id"),
                cache_read_tokens=details.get("cached_tokens"),
            )
