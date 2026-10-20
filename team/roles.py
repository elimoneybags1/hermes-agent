"""Roles: the roster of workers Team knows about."""

from __future__ import annotations

import tomllib
from pathlib import Path


def load_roles(root: Path | None = None) -> list[dict]:
    """Parse every roles/<name>/role.toml, sorted by name.

    Args:
        root: Repository root. Defaults to the parent of this file's directory.

    Returns:
        List of role dicts with keys from role.toml, plus 'name' as the directory name.
    """
    if root is None:
        root = Path(__file__).resolve().parent.parent

    roles_dir = root / "roles"
    if not roles_dir.is_dir():
        return []

    roles = []
    for role_dir in sorted(roles_dir.iterdir()):
        if not role_dir.is_dir():
            continue
        role_file = role_dir / "role.toml"
        if not role_file.exists():
            continue
        with role_file.open("rb") as f:
            data = tomllib.load(f)
        data["name"] = role_dir.name
        roles.append(data)

    return roles