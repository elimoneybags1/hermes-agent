"""Per-target overlays on roles: what a role knows and may do on one repo.

A role is the shared roster's file set (``team/roles/<role>/``). An overlay for a
target lives outside every repo at ``$TEAM_STATE/targets/<target>/overlay/<role>/``
and may hold:

- ``SOUL.md``: appended to the role's instructions under a heading for the target;
- ``skills/<name>/SKILL.md``: skills added to the role for this target;
- ``role.toml``: a fragment that only **narrows**: ``tools`` (a subset), ``models.candidates``
  (a subset, in the overlay's order), ``models.pinned`` (one of them), ``budget`` (not above
  the role's), and a ``[hermes]`` table merged over the role's.

An overlay can never widen a role: a tool, model or budget the roster does not grant is
refused. The overlay directory of a target is its own git repository, so every change is a
commit (``commit``), and the profile a worker runs on is ``<role>--<target>``, synced by
``team roles sync`` like any other.
"""

from __future__ import annotations

import copy
import subprocess
import tomllib
from pathlib import Path

from team.ledger import state_dir


class OverlayError(ValueError):
    """An overlay that widens a role, or a fragment that cannot be read."""


def overlay_dir(target: str) -> Path:
    return state_dir() / "targets" / target / "overlay"


def role_dir(target: str, role: str) -> Path:
    return overlay_dir(target) / role


def roles(target: str) -> list[str]:
    """Roles this target overlays: directories with a role.toml, a SOUL.md or skills."""
    d = overlay_dir(target)
    if not d.is_dir():
        return []
    out = []
    for p in sorted(d.iterdir()):
        if p.is_dir() and ((p / "role.toml").exists() or (p / "SOUL.md").exists() or (p / "skills").is_dir()):
            out.append(p.name)
    return out


def exists(target: str, role: str) -> bool:
    return role in roles(target)


def profile_name(role: str, target: str) -> str:
    return f"{role}--{target}"


def fragment(target: str, role: str) -> dict:
    path = role_dir(target, role) / "role.toml"
    if not path.exists():
        return {}
    try:
        with path.open("rb") as f:
            return tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise OverlayError(f"{path}: {e}") from e


def soul(target: str, role: str) -> str | None:
    path = role_dir(target, role) / "SOUL.md"
    return path.read_text() if path.exists() else None


def skills(target: str, role: str) -> dict[str, Path]:
    """Skill name -> directory, for the overlay's skills."""
    d = role_dir(target, role) / "skills"
    if not d.is_dir():
        return {}
    return {p.name: p for p in sorted(d.iterdir()) if (p / "SKILL.md").exists()}


NARROWING_KEYS = {"tools", "models", "budget", "hermes"}


def apply(role: dict, target: str) -> dict:
    """The role with the target's overlay applied; refuses anything that widens it.

    ``role`` is what ``roles.load`` built (tools, models, budget, skills, soul resolved);
    the result carries ``overlay`` (the target) and ``profile`` (``<role>--<target>``).
    """
    name = role["name"]
    frag = fragment(target, name)
    unknown = set(frag) - NARROWING_KEYS
    if unknown:
        raise OverlayError(f"overlay {target}/{name}: role.toml may only narrow {sorted(NARROWING_KEYS)}, not {sorted(unknown)}")
    out = copy.deepcopy(role)
    if "tools" in frag:
        tools = [str(t) for t in frag["tools"]]
        extra = [t for t in tools if t not in role.get("tools", [])]
        if extra:
            raise OverlayError(f"overlay {target}/{name}: tools {extra} are not the role's to give")
        out["tools"] = tools
    models = frag.get("models") or {}
    if "candidates" in models:
        cands = [str(c) for c in models["candidates"]]
        extra = [c for c in cands if c not in role["models"]["candidates"]]
        if extra or not cands:
            raise OverlayError(f"overlay {target}/{name}: candidates {extra or '[]'} are not among the role's {role['models']['candidates']}")
        out["models"]["candidates"] = cands
    if models.get("pinned"):
        if models["pinned"] not in out["models"]["candidates"]:
            raise OverlayError(f"overlay {target}/{name}: pinned {models['pinned']!r} is not a candidate")
        out["models"]["pinned"] = models["pinned"]
    budget = frag.get("budget") or {}
    for key in ("max_turns", "run_budget_s"):
        if key in budget:
            v = budget[key]
            if isinstance(v, bool) or not isinstance(v, int) or v < 1 or v > role["budget"][key]:
                raise OverlayError(f"overlay {target}/{name}: budget.{key} must be a whole number from 1 to the role's {role['budget'][key]}")
            out["budget"][key] = v
    if frag.get("hermes"):
        if not isinstance(frag["hermes"], dict):
            raise OverlayError(f"overlay {target}/{name}: [hermes] must be a table")
        forbidden = {"model", "providers", "plugins", "fallback_providers", "auxiliary"} & set(frag["hermes"])
        if forbidden:
            raise OverlayError(f"overlay {target}/{name}: [hermes] may not set {sorted(forbidden)}; the lock owns them")
        merged = copy.deepcopy(out.get("hermes") or {})
        _merge(merged, frag["hermes"])
        out["hermes"] = merged
    extra_skills = skills(target, name)
    out["skills"] = list(dict.fromkeys([*role["skills"], *extra_skills]))
    out["skill_dirs"] = {**(role.get("skill_dirs") or {}), **extra_skills}
    base_soul = role.get("soul")
    more = soul(target, name)
    if more:
        out["soul"] = (base_soul or "").rstrip() + f"\n\n# For target {target}\n\n" + more.strip() + "\n"
    out["overlay"] = target
    out["profile"] = profile_name(name, target)
    return out


def _merge(into: dict, extra: dict) -> None:
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(into.get(key), dict):
            _merge(into[key], value)
        else:
            into[key] = copy.deepcopy(value)


def commit(target: str, message: str) -> str | None:
    """Commit every change in the target's overlay repository; None when nothing changed."""
    d = overlay_dir(target)
    d.mkdir(parents=True, exist_ok=True)
    if not (d / ".git").exists():
        _git(d, "init", "-q", "-b", "main")
        _git(d, "config", "user.name", "team")
        _git(d, "config", "user.email", "team@local")
    _git(d, "add", "-A")
    if not _git(d, "status", "--porcelain").strip():
        return None
    _git(d, "commit", "-q", "-m", message)
    return _git(d, "rev-parse", "HEAD").strip()


def _git(d: Path, *args: str) -> str:
    r = subprocess.run(["git", "-C", str(d), *args], capture_output=True, text=True, check=False)
    if r.returncode != 0:
        raise OverlayError(r.stderr.strip() or f"git {' '.join(args)} failed")
    return r.stdout
