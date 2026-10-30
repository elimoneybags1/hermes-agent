"""Spend against a provider's monthly allowance, from Portkey's request log.

A provider with an "allowance" in catalog.json (today: anthropic, the Max plan's
included API credit) is paid per token out of a fixed monthly credit. build.py
writes litellm/allowances.json with each such provider's budget, cycle day and
per-model prices; this module totals what Portkey's requests.jsonl says that
provider answered since the cycle started, at those prices.

The estimate errs high: every prompt token is priced as uncached input (cache
reads are really ~0.1x), and the long-prompt rate applies per request. It never
reads prompt text; requests.jsonl holds none.

Standard library only: used by the LiteLLM spend_guard callback (venv Python)
and by the dashboard (system python3).
"""

from __future__ import annotations

import datetime as dt
import json
import os
import threading
from pathlib import Path


def state_dir() -> Path:
    return Path(os.environ.get("TEAM_STATE") or Path.home() / "state" / "team")


def load_allowances(state: Path | None = None) -> dict:
    p = (state or state_dir()) / "litellm" / "allowances.json"
    return json.loads(p.read_text()) if p.exists() else {}


def cycle_start(cycle_day: int, now: dt.datetime | None = None) -> dt.datetime:
    """The most recent cycle_day at 00:00 UTC on or before now."""
    now = now or dt.datetime.now(dt.timezone.utc)  # noqa: UP017 - also runs on the system python3 (3.9), which has no dt.UTC
    start = now.replace(day=cycle_day, hour=0, minute=0, second=0, microsecond=0)
    if start > now:
        year, month = (now.year, now.month - 1) if now.month > 1 else (now.year - 1, 12)
        start = start.replace(year=year, month=month)
    return start


def cost(price: dict, prompt_tokens: int, completion_tokens: int) -> float:
    over = price.get("long_prompt_over")
    if over and prompt_tokens > over:
        i, o = price["long_input_per_mtok"], price["long_output_per_mtok"]
    else:
        i, o = price["input_per_mtok"], price["output_per_mtok"]
    return (prompt_tokens * i + completion_tokens * o) / 1_000_000


class Ledger:
    """Running totals per allowance provider; reads only the bytes appended since the last call."""

    def __init__(self, state: Path | None = None) -> None:
        self.state = state or state_dir()
        self.lock = threading.Lock()
        self.offset = 0
        self.inode = None
        self.rows: list[tuple[str, str, float]] = []  # (time, provider, usd)
        self.unpriced: dict[str, int] = {}

    def _read(self, allowances: dict) -> None:
        path = self.state / "portkey" / "requests.jsonl"
        if not path.exists():
            return
        st = path.stat()
        if self.inode != st.st_ino or st.st_size < self.offset:  # rotated or truncated
            self.offset, self.inode, self.rows, self.unpriced = 0, st.st_ino, [], {}
        with open(path, "rb") as f:
            f.seek(self.offset)
            data = f.read()
        end = data.rfind(b"\n") + 1  # a half-written last line waits for the next read
        self.offset += end
        slugs = {a["portkey"]: name for name, a in allowances.items()}
        for line in data[:end].splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            name = slugs.get(r.get("provider"))
            if not name:
                continue
            tokens_in, tokens_out = r.get("prompt_tokens") or 0, r.get("completion_tokens") or 0
            if not (tokens_in or tokens_out):
                continue
            price = allowances[name]["prices"].get(r.get("provider_model") or "")
            if price is None:
                # A model the catalog does not price: count it at the provider's highest price.
                self.unpriced[r.get("provider_model") or "?"] = self.unpriced.get(r.get("provider_model") or "?", 0) + 1
                price = max(allowances[name]["prices"].values(), key=lambda p: p["output_per_mtok"])
            self.rows.append((r.get("time") or "", name, cost(price, tokens_in, tokens_out)))

    def status(self, now: dt.datetime | None = None) -> dict:
        """Provider -> {spent_usd, monthly_usd, stop_at_usd, over, since, requests}."""
        allowances = load_allowances(self.state)
        with self.lock:
            self._read(allowances)
            out = {}
            for name, a in allowances.items():
                since = cycle_start(a["cycle_day"], now)
                iso = since.strftime("%Y-%m-%dT%H:%M:%S")
                mine = [usd for t, p, usd in self.rows if p == name and t[:19] >= iso]
                spent = round(sum(mine), 4)
                out[name] = {
                    "spent_usd": spent, "monthly_usd": a["monthly_usd"], "stop_at_usd": a["stop_at_usd"],
                    "over": spent >= a["stop_at_usd"], "since": iso + "Z", "requests": len(mine),
                    "configs": a["configs"], "unpriced": dict(self.unpriced),
                }
            return out

    def blocked_configs(self) -> set[str]:
        return {c for s in self.status().values() if s["over"] for c in s["configs"]}
