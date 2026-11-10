"""Record every call the proxy makes, successful or not, in $HOME/state/team/litellm/usage.db.

One row per attempt against a deployment, so a failure that fell back to another
model shows up as two rows. `session` is the caller's x-litellm-session-id (a
Team job, or "smoke-1" from a status check); LiteLLM forwards the same id to
Portkey as x-portkey-trace-id (forward_headers.py), so this table, Portkey's
requests.jsonl and Team's ledger line up on one id.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from litellm.constants import RETURN_RAW_MODEL_NAME_METADATA_KEY
from litellm.integrations.custom_logger import CustomLogger

DB = Path(os.environ.get("TEAM_STATE") or Path.home() / "state" / "team") / "litellm" / "usage.db"
log = logging.getLogger("team.usage_log")

SCHEMA = """
CREATE TABLE IF NOT EXISTS calls (
    id           INTEGER PRIMARY KEY,
    ts           TEXT NOT NULL,      -- UTC ISO seconds, when the call finished
    session      TEXT,               -- x-litellm-session-id, if sent
    "group"      TEXT,               -- model_name the caller asked for: tier-medium, or a catalog id
    tier         TEXT,               -- simple | medium | complex | reasoning when called by tier
    deployment   TEXT,               -- model_info.id: <slug> or tier-<t>/<slug>
    config       TEXT,               -- x-portkey-config sent to Portkey: team-model-<slug>
    model        TEXT,               -- catalog id (LiteLLM's view; Portkey's log names the provider)
    response_model TEXT,
    response_id TEXT,
    routed_group TEXT,
    route_json   TEXT,               -- the auto router's decision (tier, cause, score, signals), tier-auto calls only
    tokens_in    INTEGER,
    tokens_out   INTEGER,
    tokens_total INTEGER,
    cache_read_tokens INTEGER,
    cache_write_tokens INTEGER,
    reasoning_tokens INTEGER,
    usage_reported INTEGER NOT NULL DEFAULT 0,
    usage_json TEXT,
    latency_ms   INTEGER,
    ok           INTEGER NOT NULL CHECK (ok IN (0, 1)),
    rate_limited INTEGER NOT NULL DEFAULT 0 CHECK (rate_limited IN (0, 1)),
    error        TEXT
);
CREATE INDEX IF NOT EXISTS calls_ts ON calls(ts);
CREATE INDEX IF NOT EXISTS calls_session ON calls(session);
"""


def connect() -> sqlite3.Connection:
    DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB, timeout=5)
    conn.executescript(SCHEMA)
    columns = {r[1] for r in conn.execute("PRAGMA table_info(calls)")}
    with conn:
        for name, kind in {"tokens_total": "INTEGER", "cache_read_tokens": "INTEGER",
                           "cache_write_tokens": "INTEGER", "reasoning_tokens": "INTEGER",
                           "usage_reported": "INTEGER NOT NULL DEFAULT 0", "usage_json": "TEXT",
                           "response_model": "TEXT", "response_id": "TEXT", "routed_group": "TEXT",
                           "route_json": "TEXT"}.items():
            if name not in columns:
                conn.execute(f"ALTER TABLE calls ADD COLUMN {name} {kind}")
    return conn


def usage_fields(response, slo: dict, ok: bool) -> dict:
    usage = response.get("usage") if isinstance(response, dict) else getattr(response, "usage", None)
    if hasattr(usage, "model_dump"):
        usage = usage.model_dump()
    usage = usage if isinstance(usage, dict) else {}
    prompt = usage.get("prompt_tokens_details") or {}
    completion = usage.get("completion_tokens_details") or {}
    incoming = usage.get("prompt_tokens")
    if incoming is None and usage.get("input_tokens") is not None:
        incoming = sum(usage.get(k) or 0 for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
    if incoming is None and ok:
        incoming = slo.get("prompt_tokens")
    outgoing = usage.get("completion_tokens", usage.get("output_tokens"))
    if outgoing is None and ok:
        outgoing = slo.get("completion_tokens")
    return {
        "tokens_in": incoming,
        "tokens_out": outgoing,
        "tokens_total": usage.get("total_tokens", incoming + outgoing if incoming is not None and outgoing is not None else None),
        "cache_read_tokens": prompt.get("cached_tokens", usage.get("cache_read_input_tokens")),
        "cache_write_tokens": prompt.get("cache_write_tokens", usage.get("cache_creation_input_tokens")),
        "reasoning_tokens": completion.get("reasoning_tokens", usage.get("reasoning_tokens")),
        "usage_reported": int(bool(usage)),
        "usage_json": json.dumps(usage, separators=(",", ":")) if usage else None,
    }


def route_json(slo: dict) -> str | None:
    """The complexity router's decision for this call, as compact JSON; None for a call by name or tier."""
    decision = (slo.get("metadata") or {}).get("routing_decision")
    if not isinstance(decision, dict):
        return None
    return json.dumps(dict(decision), separators=(",", ":"), default=str)


def row(kwargs: dict, ok: bool, response=None) -> dict:
    slo = kwargs.get("standard_logging_object") or {}
    lp = kwargs.get("litellm_params") or {}
    meta = lp.get("metadata") or {}
    routed_group = meta.get("deployment_model_name") or slo.get("model_group") or ""
    group = meta.get("team_requested_model") or routed_group
    tier = group.removeprefix("tier-") if group.startswith("tier-") else None
    err = slo.get("error_information") or {}
    deployment = slo.get("model_id") or ""
    error = None if ok else (err.get("error_message") or slo.get("error_str") or "")[:500]
    limited = not ok and (
        str(err.get("error_code")) == "429"
        or err.get("error_class") == "RateLimitError"
        or "rate limit" in (error or "").lower()
    )
    return {
        "ts": datetime.now(UTC).isoformat(timespec="seconds"),
        "session": meta.get("session_id") or slo.get("session_id") or slo.get("trace_id"),
        "group": group,
        "tier": tier,
        "deployment": deployment,
        # build.py names the Portkey config after the deployment slug: <slug> or tier-<t>/<slug>.
        "config": f"team-model-{deployment.rsplit('/', 1)[-1]}" if deployment else None,
        "model": slo.get("model") or kwargs.get("model"),
        "response_model": response.get("model") if isinstance(response, dict) else getattr(response, "model", None),
        "response_id": response.get("id") if isinstance(response, dict) else getattr(response, "id", None),
        "routed_group": routed_group,
        "route_json": route_json(slo),
        **usage_fields(response, slo, ok),
        "latency_ms": int((slo.get("response_time") or 0) * 1000),
        "ok": int(ok),
        "rate_limited": int(limited),
        "error": error,
    }


class UsageLog(CustomLogger):
    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
        return {**data, "metadata": {**(data.get("metadata") or {}),
                    RETURN_RAW_MODEL_NAME_METADATA_KEY: True, "team_requested_model": data.get("model")}}

    def _write(self, kwargs: dict, ok: bool, response=None) -> None:
        try:
            r = row(kwargs, ok, response)
            if not r["group"] and not r["deployment"]:
                # Not a model call: a request refused before routing, such as Hermes's
                # pricing probe of GET /v1/models without a key (agent/usage_pricing.py).
                return
            cols = ", ".join(f'"{c}"' for c in r)
            with connect() as conn:
                conn.execute(f"INSERT INTO calls ({cols}) VALUES ({', '.join('?' * len(r))})", tuple(r.values()))
        except Exception as e:  # logging must never break a call
            log.warning("usage_log: could not record call: %s", e)

    async def async_log_success_event(self, kwargs, response_obj, start_time, end_time):
        self._write(kwargs, True, response_obj)

    async def async_log_failure_event(self, kwargs, response_obj, start_time, end_time):
        self._write(kwargs, False, response_obj)


logger = UsageLog()
