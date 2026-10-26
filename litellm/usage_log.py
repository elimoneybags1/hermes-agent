"""Record every call the proxy makes, successful or not, in ~/state/team/litellm/usage.db.

One row per attempt against a real model, so a 429 that failed over to another
model shows up as two rows. Team's ledger joins on `session` (the job's
x-litellm-session-id); usage.py reports use against each provider's free quota.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from litellm.integrations.custom_logger import CustomLogger

DB = Path.home() / "state" / "team" / "litellm" / "usage.db"
log = logging.getLogger("team.usage_log")

SCHEMA = """
CREATE TABLE IF NOT EXISTS calls (
    id          INTEGER PRIMARY KEY,
    ts          TEXT NOT NULL,      -- UTC ISO seconds, when the call finished
    session     TEXT,               -- x-litellm-session-id (Team job), if sent
    tier        TEXT,               -- SIMPLE | MEDIUM | COMPLEX | REASONING, if auto-routed
    cause       TEXT,               -- why that tier: heuristic_scorer, session pin, ...
    deployment  TEXT,               -- provider:model id from free-models.json
    provider    TEXT,
    model       TEXT,
    tokens_in   INTEGER,
    tokens_out  INTEGER,
    latency_ms  INTEGER,
    ok          INTEGER NOT NULL CHECK (ok IN (0, 1)),
    rate_limited INTEGER NOT NULL DEFAULT 0 CHECK (rate_limited IN (0, 1)),
    error       TEXT
);
CREATE INDEX IF NOT EXISTS calls_ts ON calls(ts);
CREATE INDEX IF NOT EXISTS calls_session ON calls(session);
"""


def connect() -> sqlite3.Connection:
    DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB, timeout=5)
    conn.executescript(SCHEMA)
    return conn


def row(kwargs: dict, ok: bool) -> dict:
    slo = kwargs.get("standard_logging_object") or {}
    meta = slo.get("metadata") or {}
    decision = meta.get("routing_decision") or {}
    # A fallback attempt carries no routing decision; its deployment's group ("tier-medium") says the tier.
    group = ((kwargs.get("litellm_params") or {}).get("metadata") or {}).get("deployment_model_name") or ""
    tier = decision.get("tier") or (group.removeprefix("tier-").upper() if group.startswith("tier-") else None)
    err = slo.get("error_information") or {}
    deployment = slo.get("model_id") or ""
    error = None if ok else (err.get("error_message") or slo.get("error_str") or "")[:500]
    limited = not ok and (
        str(err.get("error_code")) == "429"
        or err.get("error_class") == "RateLimitError"
        or "rate limit" in (error or "").lower()
    )
    return {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "session": slo.get("session_id") or slo.get("trace_id"),
        "tier": tier,
        "cause": decision.get("cause") or ("fallback" if tier else None),
        "deployment": deployment,
        "provider": deployment.split(":", 1)[0] if ":" in deployment else slo.get("custom_llm_provider"),
        "model": slo.get("model"),
        "tokens_in": slo.get("prompt_tokens"),
        "tokens_out": slo.get("completion_tokens"),
        "latency_ms": int((slo.get("response_time") or 0) * 1000),
        "ok": int(ok),
        "rate_limited": int(limited),
        "error": error,
    }


class UsageLog(CustomLogger):
    def _write(self, kwargs: dict, ok: bool) -> None:
        try:
            r = row(kwargs, ok)
            with connect() as conn:
                conn.execute(
                    f"INSERT INTO calls ({', '.join(r)}) VALUES ({', '.join('?' * len(r))})",
                    tuple(r.values()),
                )
        except Exception as e:  # logging must never break a call
            log.warning("usage_log: could not record call: %s", e)

    async def async_log_success_event(self, kwargs, response_obj, start_time, end_time):
        self._write(kwargs, True)

    async def async_log_failure_event(self, kwargs, response_obj, start_time, end_time):
        self._write(kwargs, False)


logger = UsageLog()
