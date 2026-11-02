"""The Team gateway: one Hermes process serves every profile's turns and loads code once.

``hermes chat --oneshot`` (the app's aide and boss lanes) and the ``team boss``
TUI do not run the agent themselves: they connect over a websocket to a shared
gateway under ``$HERMES_HOME`` that Hermes starts on demand with ``--idle-exit``.
While a TUI pane is attached it is never idle, so it keeps the ``team`` and
``plugins/team`` modules it imported when it started: on 2026-11-01 a gateway
from the night before ran the old ``team_propose`` (drafting with the lead for
minutes) and refused the aide's toolset, hours after both had changed in the
checkout. Workers and the lead are unaffected: ``team.hermes_run`` builds its
own agent in a fresh process.

``refresh`` is called before anything Team sends through the gateway. The
gateway is stale when the commit it booted from (``gateway_state.json``'s
``code_sha``, written by Hermes) is not the checkout's HEAD, or when a Team
source file is newer than the gateway's lock (an uncommitted edit). A stale
gateway is restarted with ``gateway restart --detach``; without ``--yes`` and
without a terminal Hermes refuses while any session is mid-turn, so a live
turn is never interrupted and the next call tries again.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from team import ledger

SOURCE_DIRS = ("team", "plugins/team")
SOURCE_SUFFIXES = (".py", ".toml", ".md", ".yaml", ".json")
RESTART_TIMEOUT_S = 180


def hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME") or ledger.state_dir() / "hermes")


def running(home: Path) -> dict | None:
    """The gateway's lock when its process is alive, else None."""
    lock = home / "gateway.lock"
    try:
        info = json.loads(lock.read_text())
        pid = int(info["pid"])
        os.kill(pid, 0)
    except (OSError, ValueError, KeyError, TypeError):
        return None
    info["started"] = lock.stat().st_mtime
    return info


def loaded_sha(home: Path) -> str | None:
    """The commit the gateway booted from, as Hermes records it."""
    try:
        sha = json.loads((home / "gateway_state.json").read_text()).get("code_sha")
    except (OSError, ValueError, AttributeError):
        return None
    return sha if isinstance(sha, str) and sha else None


def checkout_sha(root: Path) -> str | None:
    try:
        return subprocess.check_output(
            ["git", "-C", str(root), "rev-parse", "HEAD"], text=True, stderr=subprocess.DEVNULL
        ).strip() or None
    except (OSError, subprocess.CalledProcessError):
        return None


def newest_source(root: Path) -> float:
    """The newest mtime under Team's own source directories."""
    newest = 0.0
    for rel in SOURCE_DIRS:
        for p in (Path(root) / rel).rglob("*"):
            if "__pycache__" in p.parts or p.suffix not in SOURCE_SUFFIXES:
                continue
            try:
                newest = max(newest, p.stat().st_mtime)
            except OSError:
                continue
    return newest


def changed_since(root: Path, started: float, loaded: str | None, *, head: str | None = None) -> str | None:
    """Why a process that loaded Team's code at ``started`` (from commit ``loaded``) is behind the
    checkout, or None. Used for the gateway and for ``team run`` itself."""
    head = head or checkout_sha(root)
    if loaded and head and loaded != head:
        return f"started from {loaded[:10]}, checkout is at {head[:10]}"
    if newest_source(root) > started:
        return "a Team source file changed after it started"
    return None


def stale(root: Path, home: Path | None = None, *, head: str | None = None) -> str | None:
    """Why the running gateway does not match the checkout, or None (also when none runs)."""
    home = home or hermes_home()
    gw = running(home)
    if gw is None:
        return None
    why = changed_since(root, gw["started"], loaded_sha(home), head=head)
    return f"gateway: {why}" if why else None


def refresh(root: Path, home: Path | None = None, *, run=subprocess.run, head: str | None = None) -> str | None:
    """Restart a stale gateway so the next turn runs the checkout's code.

    Returns the reason it was stale when a restart was asked for, else None.
    Hermes refuses the restart while a session is live (no ``--yes``, no
    terminal); that is left to stand, and the caller's turn goes to the old
    gateway this once.
    """
    root = Path(root)
    why = stale(root, home, head=head)
    if why is None:
        return None
    try:
        run(
            [str(root / "team" / "bin" / "team-hermes"), "gateway", "restart", "--detach"],
            stdin=subprocess.DEVNULL, capture_output=True, text=True,
            timeout=RESTART_TIMEOUT_S, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass
    return why
