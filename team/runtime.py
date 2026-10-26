"""What the Hermes runtime is doing right now, read from state on disk.

Everything here only reads: the per-role Hermes profiles under
`$TEAM_STATE/hermes/profiles/<role>/` (config, native `state.db` sessions,
logs, memories, skills), the optimizer's saved proposals, the ledger's model
calls, and the process table for a live `team run`. The web page (`team ui`,
Runtime view) and the deck (Runtime tab) both render `snapshot()`.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import yaml

from team import ledger, optimizer

SESSION_LIMIT = 10
CALLS_WINDOW_H = 24
LOG_FILES = ("agent.log", "errors.log")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    try:
        return datetime.fromtimestamp(float(ts), timezone.utc).isoformat(timespec="seconds")
    except (OverflowError, OSError, ValueError):
        return None


def profiles_dir() -> Path:
    return ledger.state_dir() / "hermes" / "profiles"


# --- team run process


def team_run(cmdlines: list[list[str]] | None = None) -> dict:
    """Is a `team run` loop alive? `cmdlines` can be injected for tests.

    A match is an argv token whose basename is `team` followed directly by
    `run` (`team run`, `uv run team run`, `.venv/bin/team run`). `uv run team ui`
    has both words but is not a match.
    """
    if cmdlines is None:
        cmdlines = _process_argvs()
    pids = []
    for argv in cmdlines:
        pid, tokens = argv[0], argv[1:]
        for i in range(len(tokens) - 1):
            if Path(tokens[i]).name == "team" and tokens[i + 1] == "run":
                pids.append(pid)
                break
    return {"alive": bool(pids), "pids": pids}


def _process_argvs() -> list[list[str]]:
    try:
        out = subprocess.run(
            ["ps", "-axo", "pid=,args="], capture_output=True, text=True, check=False, timeout=5
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    rows = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0].isdigit():
            rows.append([int(parts[0]), *parts[1:]])
    return rows


# --- Hermes profiles


def _read_sessions(db_path: Path, limit: int = SESSION_LIMIT) -> dict:
    """Newest sessions from a Hermes profile's native state.db, read-only."""
    if not db_path.is_file():
        return {"count": 0, "active": 0, "items": [], "error": None}
    uri = f"file:{db_path}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=1.0)
    except sqlite3.Error as e:
        return {"count": 0, "active": 0, "items": [], "error": str(e)}
    conn.row_factory = sqlite3.Row
    try:
        count = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        active = conn.execute("SELECT COUNT(*) FROM sessions WHERE ended_at IS NULL").fetchone()[0]
        rows = conn.execute(
            "SELECT * FROM sessions ORDER BY COALESCE(last_activity_at, started_at) DESC, started_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    except sqlite3.Error as e:
        return {"count": 0, "active": 0, "items": [], "error": str(e)}
    finally:
        conn.close()
    items = []
    for r in rows:
        d = dict(r)
        items.append(
            {
                "id": d.get("id"),
                "title": d.get("title"),
                "model": d.get("model"),
                "source": d.get("source"),
                "started_at": _iso(d.get("started_at")),
                "ended_at": _iso(d.get("ended_at")),
                "end_reason": d.get("end_reason"),
                "active": d.get("ended_at") is None,
                "message_count": d.get("message_count") or 0,
                "tool_call_count": d.get("tool_call_count") or 0,
                "api_call_count": d.get("api_call_count") or 0,
                "input_tokens": d.get("input_tokens") or 0,
                "output_tokens": d.get("output_tokens") or 0,
                "last_activity_at": _iso(d.get("last_activity_at")),
                "last_activity": d.get("last_activity_description"),
                "cwd": d.get("cwd"),
                "git_branch": d.get("git_branch"),
                "parent_session_id": d.get("parent_session_id"),
            }
        )
    return {"count": count, "active": active, "items": items, "error": None}


def _tail_line(path: Path, max_bytes: int = 4096) -> dict | None:
    """Last non-empty line of a log file plus its mtime, without reading it all."""
    if not path.is_file():
        return None
    try:
        size = path.stat().st_size
        with path.open("rb") as f:
            f.seek(max(0, size - max_bytes))
            chunk = f.read().decode("utf-8", "replace")
        lines = [ln for ln in chunk.splitlines() if ln.strip()]
        last = lines[-1] if lines else ""
        return {
            "path": str(path),
            "mtime": _iso(path.stat().st_mtime),
            "size": size,
            "last": last[-600:],
        }
    except OSError:
        return None


def _model_of(config_file: Path) -> dict:
    if not config_file.is_file():
        return {"provider": None, "default": None}
    try:
        cfg = yaml.safe_load(config_file.read_text()) or {}
    except (OSError, yaml.YAMLError):
        return {"provider": None, "default": None}
    model = cfg.get("model") or {}
    return {"provider": model.get("provider"), "default": model.get("default")}


def _count_files(path: Path) -> int:
    if not path.is_dir():
        return 0
    return sum(1 for p in path.rglob("*") if p.is_file())


def profile_status(home: Path, session_limit: int = SESSION_LIMIT) -> dict:
    """One Hermes profile: role, model, sessions, logs, what it has learned."""
    team_skills = sorted(p.name for p in (home / "skills" / "team").glob("*") if p.is_dir()) if (home / "skills" / "team").is_dir() else []
    return {
        "role": home.name,
        "home": str(home),
        "model": _model_of(home / "config.yaml"),
        "sessions": _read_sessions(home / "state.db", session_limit),
        "memories": _count_files(home / "memories"),
        "team_skills": team_skills,
        "logs": {name: _tail_line(home / "logs" / name) for name in LOG_FILES},
    }


def profiles(session_limit: int = SESSION_LIMIT) -> list[dict]:
    base = profiles_dir()
    if not base.is_dir():
        return []
    return [profile_status(p, session_limit) for p in sorted(base.iterdir()) if p.is_dir()]


# --- ledger calls


def calls_summary(conn: sqlite3.Connection | None, hours: int = CALLS_WINDOW_H) -> dict:
    """Model calls in the last `hours`, grouped by provider and model."""
    if conn is None:
        return {"window_h": hours, "providers": []}
    since = datetime.now(timezone.utc).timestamp() - hours * 3600
    since_iso = datetime.fromtimestamp(since, timezone.utc).isoformat(timespec="seconds")
    try:
        rows = conn.execute(
            "SELECT provider, model, COUNT(*) AS calls, SUM(ok) AS ok,"
            " COALESCE(SUM(tokens_in), 0) AS tokens_in, COALESCE(SUM(tokens_out), 0) AS tokens_out,"
            " COALESCE(SUM(api_calls), 0) AS api_calls, MAX(ts) AS last_ts"
            " FROM calls WHERE ts >= ? GROUP BY provider, model ORDER BY calls DESC",
            (since_iso,),
        ).fetchall()
    except sqlite3.Error:
        return {"window_h": hours, "providers": []}
    return {"window_h": hours, "providers": [dict(r) for r in rows]}


# --- optimizer proposals


def proposals_summary() -> list[dict]:
    """Saved recommendations, newest first, without their evidence payload."""
    try:
        items = optimizer.proposals()
    except (OSError, ValueError, KeyError):
        return []
    out = []
    for p in items:
        d = {k: v for k, v in p.items() if k != "evidence"}
        d["evidence_count"] = len(p.get("evidence") or [])
        out.append(d)
    out.sort(key=lambda p: (p.get("created_at") or "", p.get("id") or ""), reverse=True)
    return out


# --- the whole picture


def snapshot(conn: sqlite3.Connection | None = None, session_limit: int = SESSION_LIMIT) -> dict:
    return {
        "generated_at": _now_iso(),
        "state_dir": str(ledger.state_dir()),
        "run": team_run(),
        "profiles": profiles(session_limit),
        "calls": calls_summary(conn),
        "proposals": proposals_summary(),
    }


def fingerprint() -> str:
    """Changes whenever a profile's sessions, logs or the proposals change."""
    parts = []
    base = profiles_dir()
    if base.is_dir():
        for home in sorted(base.iterdir()):
            if not home.is_dir():
                continue
            for rel in ("state.db", "state.db-wal", "logs/agent.log", "logs/errors.log", "memories"):
                p = home / rel
                try:
                    st = p.stat()
                except OSError:
                    continue
                parts.append(f"{home.name}/{rel}:{st.st_mtime_ns}:{st.st_size}")
    props = ledger.state_dir() / "optimizer" / "proposals"
    if props.is_dir():
        try:
            parts.append("proposals:" + ",".join(sorted(p.name for p in props.iterdir())))
        except OSError:
            pass
    return "|".join(parts)


def main(argv: list[str] | None = None) -> int:
    """`python -m team.runtime [--json]`: print the runtime snapshot."""
    import argparse
    import sys

    parser = argparse.ArgumentParser(prog="team.runtime")
    parser.add_argument("--json", action="store_true", help="print JSON")
    args = parser.parse_args(argv)
    path = ledger.db_path()
    conn = ledger.connect(path) if path.exists() else None
    try:
        data = snapshot(conn)
    finally:
        if conn is not None:
            conn.close()
    if args.json:
        json.dump(data, sys.stdout, indent=2, default=str)
        print()
        return 0
    run = data["run"]
    print(f"team run: {'alive ' + ','.join(map(str, run['pids'])) if run['alive'] else 'stopped'}")
    for p in data["profiles"]:
        s = p["sessions"]
        print(f"{p['role']}\t{p['model']['provider'] or '-'}/{p['model']['default'] or '-'}\tsessions {s['count']} ({s['active']} active)\tmemories {p['memories']}\tteam skills {len(p['team_skills'])}")
    for prov in data["calls"]["providers"]:
        print(f"calls {data['calls']['window_h']}h\t{prov['provider']}\t{prov['model']}\t{prov['ok']}/{prov['calls']} ok\tin {prov['tokens_in']} out {prov['tokens_out']}")
    for pr in data["proposals"]:
        print(f"proposal\t{pr['status']}\t{pr['created_at']}\t{pr['subject'][:90]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
