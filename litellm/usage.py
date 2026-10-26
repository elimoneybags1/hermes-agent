"""Report what the proxy has used, against each provider's documented free quota.

    usage.py              today (UTC) by provider and model, plus quota use
    usage.py --days 7     the last 7 days
    usage.py --session S  every call of one Team job

Runs on plain python3; reads ~/state/team/litellm/usage.db and free-models.json.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

DB = Path.home() / "state" / "team" / "litellm" / "usage.db"
CATALOG = Path(__file__).resolve().parent / "free-models.json"
WINDOW = {"minute": timedelta(minutes=1), "day": timedelta(days=1), "month": timedelta(days=30)}


def since(delta: timedelta) -> str:
    return (datetime.now(timezone.utc) - delta).isoformat(timespec="seconds")


def table(rows: list[tuple], head: tuple) -> None:
    rows = [tuple("" if v is None else str(v) for v in r) for r in rows]
    widths = [max(len(h), *(len(r[i]) for r in rows)) if rows else len(h) for i, h in enumerate(head)]
    for r in (head, *rows):
        print("  ".join(v.ljust(w) for v, w in zip(r, widths)))


def report(conn: sqlite3.Connection, days: int) -> None:
    start = since(timedelta(days=days)) if days > 1 else datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00+00:00")
    print(f"Calls since {start}\n")
    table(conn.execute("""
        SELECT provider, model, COUNT(*), SUM(ok), SUM(rate_limited),
               COALESCE(SUM(tokens_in), 0), COALESCE(SUM(tokens_out), 0),
               CAST(AVG(CASE WHEN ok THEN latency_ms END) AS INTEGER)
        FROM calls WHERE ts >= ? GROUP BY provider, model ORDER BY provider, COUNT(*) DESC
    """, (start,)).fetchall(), ("provider", "model", "calls", "ok", "429s", "tok_in", "tok_out", "avg_ms"))

    print("\nBy tier")
    table(conn.execute("""
        SELECT COALESCE(tier, '-'), COUNT(*), SUM(ok), COUNT(DISTINCT session)
        FROM calls WHERE ts >= ? GROUP BY tier ORDER BY tier
    """, (start,)).fetchall(), ("tier", "calls", "ok", "sessions"))

    print("\nFree quota (documented limits only)")
    providers = json.loads(CATALOG.read_text())["providers"]
    rows = []
    for name, p in providers.items():
        q = p.get("quota")
        if not q:
            continue
        window_start = since(WINDOW[q["window"]])
        key = "model" if q["per"] == "model" else "provider"
        for who, calls, tokens in conn.execute(f"""
            SELECT {key}, COUNT(*), COALESCE(SUM(tokens_in), 0) + COALESCE(SUM(tokens_out), 0)
            FROM calls WHERE provider = ? AND ts >= ? GROUP BY {key}
        """, (name, window_start)).fetchall():
            used = f"{calls}/{q['requests']} req"
            if q.get("tokens"):
                used += f", {tokens}/{q['tokens']} tok"
            rows.append((name, who, f"per {q['window']}", used))
    table(rows, ("provider", "scope", "window", "used")) if rows else print("  (none used yet)")
    print("\nNo published numbers for OpenRouter (daily free cap), Gemini (AI Studio), Mistral (admin console), Z.ai.")


def session(conn: sqlite3.Connection, sid: str) -> None:
    table(conn.execute("""
        SELECT ts, tier, deployment, tokens_in, tokens_out, latency_ms, ok, rate_limited, SUBSTR(error, 1, 60)
        FROM calls WHERE session = ? ORDER BY id
    """, (sid,)).fetchall(), ("ts", "tier", "deployment", "in", "out", "ms", "ok", "429", "error"))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=1)
    ap.add_argument("--session")
    args = ap.parse_args()
    if not DB.exists():
        print(f"no usage recorded yet ({DB} does not exist)")
        return
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    session(conn, args.session) if args.session else report(conn, args.days)


if __name__ == "__main__":
    main()
