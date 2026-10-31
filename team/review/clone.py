"""Shallow, read-only clone of a repo for review. Auth is whatever git/gh is already logged in with; no token is read or stored."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any


class CloneError(RuntimeError):
    """Error during git clone."""


def clone_repo(url: str, dest: str | Path, *, runner: Any = subprocess.run, timeout: int = 300) -> Path:
    """shallow-clones url into dest via the git CLI and returns the resolved dest path."""
    dest = Path(dest)
    if dest.exists() and any(dest.iterdir()):
        raise CloneError(f'{dest} exists and is not empty')

    try:
        proc = runner(['git', 'clone', '--depth', '1', '--no-tags', '--single-branch', '--', url, str(dest)],
                      capture_output=True, text=True, timeout=timeout,
                      env={**os.environ, 'GIT_TERMINAL_PROMPT': '0'})
    except (OSError, subprocess.TimeoutExpired) as e:
        raise CloneError(f'git clone failed: {e}') from e
    if proc.returncode != 0:
        raise CloneError(f'git clone failed: {(proc.stderr or proc.stdout).strip()[:300]}')

    return dest.resolve()