"""Record every call the proxy makes, successful or not, in $HOME/state/team/litellm/usage.db.

One row per attempt against a deployment, so a failure that fell back to another
model shows up as two rows. `session` is the caller's x-litellm-session-id (a
Team job, or "smoke-1" from a status check); LiteLLM forwards the same id to
Portkey as x-portkey-trace-id (forward_headers.py), so this table, Portkey's
requests.jsonl and Team's ledger line up on one id.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from litellm.integrations.custom_logger import CustomLogger

DB = Path(os.environ.get("TEAM_STATE_DIR") or Path.home() / "state" / "team") / "litellm" / "usage.db"
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
    tokens_in    INTEGER,
    tokens_out   INTEGER,
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
    return conn


def row(kwargs: dict, ok: bool) -> dict:
    slo = kwargs.get("standard_logging_object") or {}
    lp = kwargs.get("litellm_params") or {}
    meta = lp.get("metadata") or {}
    group = meta.get("deployment_model_name") or slo.get("model_group") or ""
    tier = group.removeprefix("tier-") if group.startswith("tier-") else None
    err = slo.get("error_information") or {}
    error = None if ok else (err.get("error_message") or slo.get("error_str") or "")[:500]
    limited = not ok and (
        str(err.get("error_code")) == "429"
        or err.get("error_class") == "RateLimitError"
        or "rate limit" in (error or "").lower()
    )
    headers = (kwargs.get("extra_headers") or lp.get("extra_headers") or {})
    return {
        "ts": datetime.now(UTC).isoformat(timespec="seconds"),
        "session": meta.get("session_id") or slo.get("session_id") or slo.get("trace_id"),
        "group": group,
        "tier": tier,
        "deployment": slo.get("model_id") or "",
        "config": headers.get("x-portkey-config"),
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
            cols = ", ".join(f'"{c}"' for c in r)
            with connect() as conn:
                conn.execute(f"INSERT INTO calls ({cols}) VALUES ({', '.join('?' * len(r))})", tuple(r.values()))
        except Exception as e:  # logging must never break a call
            log.warning("usage_log: could not record call: %s", e)

    async def async_log_success_event(self, kwargs, response_obj, start_time, end_time):
        self._write(kwargs, True)

    async def async_log_failure_event(self, kwargs, response_obj, start_time, end_time):
        self._write(kwargs, False)


logger = UsageLog()
