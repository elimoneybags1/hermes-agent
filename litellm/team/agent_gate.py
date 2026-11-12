"""Send Hermes's agent loop only to models that can, and measurably do, work it.

On 2026-11-12 a tool-carrying, 55k-token Hermes turn reached gemini-2.5-flash-lite
through tier-simple and was answered with prose about being unable to act. This
filter runs on every call to a tier group (``tier-*``) that carries tools:

1. Capability: keep only deployments whose catalog row says ``tools: true``
   (``model_info.supports_function_calling``). Prompt size is already handled by
   LiteLLM's pre-call checks. If none remain, the tier is skipped and LiteLLM's
   tier fallback takes the request.
2. Track record: from ``usage.db`` (``had_tools = 1`` rows in the window), drop a
   model with at least ``min_samples`` tool requests whose success rate (rate
   limits excluded; LiteLLM's cooldown handles those) is under ``min_ok_pct``,
   or that answered under ``min_fresh_tool_pct`` of its fresh asks with a tool
   call (it talks instead of acting). If the record would drop every capable
   model, the capable ones are kept: the record narrows, never empties.

A model picked by name (``claude-opus-5-5``) is never filtered: that is a
deliberate choice. Thresholds come from ``policy.agent_gate`` in catalog.json;
build.py writes them to ``$TEAM_STATE/litellm/agent_gate.json``. The filter fails
open: any error returns the deployments unchanged.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

try:  # the proxy has litellm; Team's test runner (Hermes's environment) does not
    from litellm.integrations.custom_logger import CustomLogger
except ImportError:  # pragma: no cover
    CustomLogger = object  # type: ignore[assignment,misc]

STATE = Path(os.environ.get("TEAM_STATE") or Path.home() / "state" / "team") / "litellm"
CACHE_S = 60
log = logging.getLogger("team.agent_gate")


def slug(deployment: dict) -> str:
    return str((deployment.get("model_info") or {}).get("id") or "").rsplit("/", 1)[-1]


def load_policy(path: Path) -> dict:
    return json.loads(path.read_text())


def records(db: Path, window_hours: int) -> dict[str, dict[str, int]]:
    """Model slug -> counts over tool-carrying calls in the window."""
    since = (datetime.now(UTC) - timedelta(hours=window_hours)).isoformat(timespec="seconds")
    out: dict[str, dict[str, int]] = {}
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2) as conn:
        columns = {r[1] for r in conn.execute("PRAGMA table_info(calls)")}
        if not {"had_tools", "fresh_ask", "tool_call"} <= columns:
            return out  # usage_log adds them on its first write; no record yet
        rows = conn.execute(
            "SELECT deployment, count(*), sum(ok), sum(rate_limited), "
            "sum(coalesce(fresh_ask,0) * ok), sum(coalesce(fresh_ask,0) * ok * coalesce(tool_call,0)) "
            "FROM calls WHERE had_tools = 1 AND ts > ? GROUP BY deployment", (since,)).fetchall()
    for dep, n, ok, limited, fresh, fresh_tool in rows:
        r = out.setdefault(str(dep or "").rsplit("/", 1)[-1],
                           {"n": 0, "ok": 0, "limited": 0, "fresh": 0, "fresh_tool": 0})
        for k, v in zip(("n", "ok", "limited", "fresh", "fresh_tool"), (n, ok, limited, fresh, fresh_tool)):
            r[k] += v or 0
    return out


def verdict(rec: dict[str, int] | None, pol: dict) -> str | None:
    """Why this model's record rules it out, or None when it passes (or has too few samples)."""
    if not rec:
        return None
    tried = rec["n"] - rec["limited"]
    if tried >= pol["min_samples"] and 100 * rec["ok"] < pol["min_ok_pct"] * tried:
        return f"ok {rec['ok']}/{tried} tool requests, under {pol['min_ok_pct']}%"
    if rec["fresh"] >= pol["min_samples"] and 100 * rec["fresh_tool"] < pol["min_fresh_tool_pct"] * rec["fresh"]:
        return f"tool call on {rec['fresh_tool']}/{rec['fresh']} fresh asks, under {pol['min_fresh_tool_pct']}%"
    return None


def gate(group: str, deployments: list, has_tools: bool, recs: dict, pol: dict) -> tuple[list, list[str]]:
    """The deployments a tool-carrying tier call may use, and one note per dropped model."""
    if not has_tools or not group.startswith("tier-"):
        return deployments, []
    notes = []
    capable = []
    for d in deployments:
        if (d.get("model_info") or {}).get("supports_function_calling") is True:
            capable.append(d)
        else:
            notes.append(f"{slug(d)}: not tool-capable in the catalog")
    proven = []
    for d in capable:
        why = verdict(recs.get(slug(d)), pol)
        if why:
            notes.append(f"{slug(d)}: {why}")
        else:
            proven.append(d)
    if capable and not proven:
        notes.append("record would drop every capable model; keeping them")
        proven = capable
    return proven, notes


class AgentGate(CustomLogger):  # type: ignore[misc]
    def __init__(self, state: Path = STATE) -> None:
        super().__init__()
        self.state = state
        self._cache: tuple[float, dict, dict] | None = None

    def _data(self) -> tuple[dict, dict]:
        now = time.time()
        if self._cache is None or now - self._cache[0] > CACHE_S:
            pol = load_policy(self.state / "agent_gate.json")
            db = self.state / "usage.db"
            recs = records(db, pol["window_hours"]) if db.exists() else {}
            self._cache = (now, pol, recs)
        return self._cache[1], self._cache[2]

    async def async_filter_deployments(self, model, healthy_deployments, messages,
                                       request_kwargs=None, parent_otel_span=None):
        try:
            has_tools = bool((request_kwargs or {}).get("tools"))
            if not has_tools or not str(model).startswith("tier-"):
                return healthy_deployments
            pol, recs = self._data()
            kept, notes = gate(str(model), list(healthy_deployments), has_tools, recs, pol)
            if notes:
                log.warning("agent_gate: %s: %s", model, "; ".join(notes))
            return kept
        except Exception as e:  # never break a call over routing advice
            log.warning("agent_gate: skipped: %s", e)
            return healthy_deployments


gate_hook = AgentGate()
