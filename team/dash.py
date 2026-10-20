"""Dashboard: a JSON-serializable snapshot of the ledger and roster."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from team import ledger
from team.roles import load_roles


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _pass_rate(passed: int, total: int) -> float | None:
    if total == 0:
        return None
    return passed / total


def _role_stats(conn, role_name: str) -> dict:
    """Stats for finished jobs of a specific role."""
    rows = conn.execute(
        "SELECT outcome FROM jobs WHERE role = ? AND ended IS NOT NULL", (role_name,)
    ).fetchall()
    total = len(rows)
    passed = sum(1 for r in rows if r["outcome"] == "pass")
    return {
        "jobs": total,
        "passed": passed,
        "pass_rate": _pass_rate(passed, total),
    }


def _role_working_on(conn, role_name: str) -> list[dict]:
    """Unfinished jobs for a specific role."""
    rows = conn.execute(
        "SELECT id, target, attempt, started FROM jobs WHERE role = ? AND ended IS NULL ORDER BY id",
        (role_name,),
    ).fetchall()
    return [
        {
            "job_id": r["id"],
            "target": r["target"],
            "attempt": r["attempt"],
            "started": r["started"],
        }
        for r in rows
    ]


def _base_stats(conn) -> dict:
    """Overall stats across all jobs (including unknown roles)."""
    rows = conn.execute("SELECT outcome FROM jobs WHERE ended IS NOT NULL").fetchall()
    total = len(rows)
    passed = sum(1 for r in rows if r["outcome"] == "pass")
    return {
        "jobs": total,
        "passed": passed,
        "pass_rate": _pass_rate(passed, total),
    }


def _recent_jobs(conn, limit: int = 20) -> list[dict]:
    """Last N jobs, newest first."""
    rows = conn.execute(
        "SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]


def _safe_connect() -> ledger.sqlite3.Connection | None:
    """Connect to ledger if it exists, otherwise return None."""
    path = ledger.db_path()
    if not path.exists():
        return None
    return ledger.connect(path)


def snapshot(conn: ledger.sqlite3.Connection | None, roles: list[dict]) -> dict:
    """Build the dashboard snapshot.

    Args:
        conn: Ledger connection, or None if no ledger exists.
        roles: List of role dicts from load_roles().

    Returns:
        JSON-serializable dict with the exact structure specified.
    """
    if conn is None:
        # Empty in-memory ledger
        base = {"jobs": 0, "passed": 0, "pass_rate": None}
        recent = []
        role_data = []
        for role in roles:
            role_data.append(
                {
                    "name": role["name"],
                    "title": role.get("title", ""),
                    "does": role.get("does", ""),
                    "graded_by": role.get("graded_by", ""),
                    "status": role.get("status", ""),
                    "runs_on": role.get("runs_on", {}),
                    "escalation": role.get("escalation"),
                    "budget": role.get("budget"),
                    "working_on": [],
                    "stats": {"jobs": 0, "passed": 0, "pass_rate": None},
                }
            )
        return {
            "generated_at": _now_iso(),
            "base": base,
            "roles": role_data,
            "recent": recent,
        }

    base = _base_stats(conn)
    recent = _recent_jobs(conn)

    role_data = []
    for role in roles:
        name = role["name"]
        role_data.append(
            {
                "name": name,
                "title": role.get("title", ""),
                "does": role.get("does", ""),
                "graded_by": role.get("graded_by", ""),
                "status": role.get("status", ""),
                "runs_on": role.get("runs_on", {}),
                "escalation": role.get("escalation"),
                "budget": role.get("budget"),
                "working_on": _role_working_on(conn, name),
                "stats": _role_stats(conn, name),
            }
        )

    return {
        "generated_at": _now_iso(),
        "base": base,
        "roles": role_data,
        "recent": recent,
    }


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for `team dash --json`."""
    import argparse
    import sys

    parser = argparse.ArgumentParser(prog="team dash")
    parser.add_argument("--json", action="store_true", help="print JSON snapshot")
    args = parser.parse_args(argv)

    roles = load_roles()
    conn = _safe_connect()
    try:
        data = snapshot(conn, roles)
    finally:
        if conn is not None:
            conn.close()

    if args.json:
        json.dump(data, sys.stdout, indent=2)
        print()
    else:
        # Short readable table
        for role in data["roles"]:
            sr = role["stats"]
            rate = f"{sr['pass_rate']:.2%}" if sr["pass_rate"] is not None else "—"
            wo = len(role["working_on"])
            print(f"{role['name']}\t{role['status']}\t{wo}\t{rate}")
        br = data["base"]
        base_rate = f"{br['pass_rate']:.2%}" if br["pass_rate"] is not None else "—"
        print(f"base\t{br['jobs']}\t{br['passed']}\t{base_rate}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())