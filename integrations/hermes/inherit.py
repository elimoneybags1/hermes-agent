"""Bring a project profile up to date with the main (default) profile.

Main is the overall configuration; every project profile is main plus its own
additions. Three mechanisms carry main down, each matched to what Hermes can do:

  shared scalars   integrations/hermes/base/config.yaml, Hermes's managed layer:
                   locked on top of every profile, live, nothing to do here.
  shared lists     a list in the managed layer replaces the profile's list whole,
                   so a project could not add to it. Lists in INHERITED_LISTS are
                   instead unioned here: main's entries first, then the project's
                   own. skills.external_dirs must live here anyway, because Hermes
                   reads skills.* only from the profile's own config.yaml.
  personality      the profile's SOUL.md is main's SOUL.md followed by the
                   profile's SOUL.project.md. Edit SOUL.project.md; SOUL.md is
                   rebuilt. A SOUL.md edited by hand is saved aside, not lost.

Run by integrations/bin/hermes before Hermes starts:
    inherit.py <hermes_home> <profile>
"""

from __future__ import annotations

import hashlib
import io
import os
import sys
import time
from pathlib import Path

INHERITED_LISTS = (
    ("plugins", "enabled"),
    ("command_allowlist",),
)
PROJECT_SOUL = "SOUL.project.md"
SOUL_STAMP = ".soul-inherited"  # sha256 of the SOUL.md this step last wrote


def _yaml():
    from hermes_yaml import roundtrip_yaml
    return roundtrip_yaml()


def _get(cfg, path):
    node = cfg
    for key in path:
        if not isinstance(node, dict) or key not in node:
            return None
        node = node[key]
    return node


def _set(cfg, path, value):
    node = cfg
    for key in path[:-1]:
        if not isinstance(node.get(key), dict):
            node[key] = {}
        node = node[key]
    node[path[-1]] = value


def _union(first, second):
    out = []
    for item in list(first or []) + list(second or []):
        if item not in out:
            out.append(item)
    return out


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    try:
        os.chmod(tmp, path.stat().st_mode & 0o777)
    except FileNotFoundError:
        pass
    os.replace(tmp, path)


def merge_config(main_cfg: dict, child_cfg: dict, shared_skills: str) -> bool:
    """Union main's lists into the child's, in place. True when the child changed."""
    changed = False
    for path in INHERITED_LISTS:
        main_list = _get(main_cfg, path)
        if not main_list:
            continue
        current = _get(child_cfg, path)
        merged = _union(main_list, current)
        if list(current or []) != merged:
            _set(child_cfg, path, merged)
            changed = True
    skills = child_cfg.get("skills")
    if not isinstance(skills, dict):
        skills = {}
    dirs = [d for d in (skills.get("external_dirs") or []) if d != shared_skills]
    wanted = [shared_skills] + dirs
    if list(skills.get("external_dirs") or []) != wanted:
        skills["external_dirs"] = wanted
        child_cfg["skills"] = skills
        changed = True
    return changed


def compose_soul(main_soul: str, project_soul: str) -> str:
    main_soul = main_soul.rstrip()
    project_soul = project_soul.strip()
    if not project_soul:
        return main_soul + "\n"
    return f"{main_soul}\n\n{project_soul}\n"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sync_soul(home: Path, pdir: Path, log=print) -> bool:
    """Rebuild pdir/SOUL.md from main + SOUL.project.md. True when it was written."""
    main_path = home / "SOUL.md"
    if not main_path.is_file():
        return False
    wanted = compose_soul(main_path.read_text(encoding="utf-8"),
                          (pdir / PROJECT_SOUL).read_text(encoding="utf-8")
                          if (pdir / PROJECT_SOUL).is_file() else "")
    soul = pdir / "SOUL.md"
    stamp = pdir / SOUL_STAMP
    current = soul.read_text(encoding="utf-8") if soul.is_file() else None
    if current == wanted:
        if not stamp.is_file() or stamp.read_text().strip() != _sha(wanted):
            stamp.write_text(_sha(wanted) + "\n")
        return False
    if current is not None:
        last_written = stamp.read_text().strip() if stamp.is_file() else None
        if _sha(current) != last_written and current.strip() != main_path.read_text(encoding="utf-8").strip():
            # Edited by hand (or by an older Hermes): keep it rather than overwrite it.
            aside = pdir / f"SOUL.md.edited-{time.strftime('%Y%m%d-%H%M%S')}"
            aside.write_text(current, encoding="utf-8")
            log(f"hermes: {soul} was edited directly; saved it as {aside.name}. "
                f"Project additions belong in {PROJECT_SOUL}.")
    _atomic_write(soul, wanted)
    stamp.write_text(_sha(wanted) + "\n")
    return True


def inherit(home: Path, profile: str, log=print) -> None:
    pdir = home / "profiles" / profile
    child_path = pdir / "config.yaml"
    if profile == "default" or not child_path.is_file():
        return
    y = _yaml()
    main_cfg = {}
    if (home / "config.yaml").is_file():
        with open(home / "config.yaml", encoding="utf-8") as f:
            main_cfg = y.load(f) or {}
    with open(child_path, encoding="utf-8") as f:
        child_cfg = y.load(f) or {}
    if merge_config(main_cfg, child_cfg, str(home / "skills")):
        buf = io.StringIO()
        y.dump(child_cfg, buf)
        _atomic_write(child_path, buf.getvalue())
    sync_soul(home, pdir, log=log)


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # the checkout, for hermes_yaml
    inherit(Path(sys.argv[1]), sys.argv[2], log=lambda m: print(m, file=sys.stderr))
