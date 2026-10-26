"""The real models behind a worker session that ran through Team's LiteLLM proxy.

Hermes records such a session as model `team-auto`; the proxy's usage log
(litellm/usage_log.py) knows which free model served each call, keyed by the
kanban task id the worker sent as x-litellm-session-id.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

DB = Path.home() / "state" / "team" / "litellm" / "usage.db"


def calls_for_task(task_id: str, db: Path | None = None) -> list[dict]:
    """One ledger row per (provider, model, ok) the task used; [] if none recorded."""
    db = db or DB
    if not task_id or not db.exists():
        return []
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        rows = conn.execute("""
            SELECT deployment, ok, COUNT(*), SUM(tokens_in), SUM(tokens_out),
                   CAST(SUM(latency_ms) AS INTEGER), MAX(error)
            FROM calls WHERE session = ? GROUP BY deployment, ok ORDER BY MIN(id)
        """, (task_id,)).fetchall()
    finally:
        conn.close()
    out = []
    for deployment, ok, n, tin, tout, ms, error in rows:
        provider, _, model = (deployment or "unknown:unknown").partition(":")
        out.append({
            "provider": provider, "model": model, "tokens_in": tin, "tokens_out": tout,
            "latency_ms": ms, "ok": bool(ok), "error": error, "api_calls": n,
        })
    return out
