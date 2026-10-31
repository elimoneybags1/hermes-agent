"""Review, then apply: Team's role files and settings, and the gateway catalog.

A change is reviewed first: the diff, and the checks Team itself would run
(the role loads and its Hermes profile passes the free-only audit; settings
parse; the catalog renders through build.py, which refuses anything not free).
Apply checks again, writes the file, syncs the role's profile or rebuilds the
gateway, and commits that one file, so every change is a commit that can be
reverted. A file that already has someone else's uncommitted changes is
refused: commit or discard those first, so an apply never sweeps them in.

Keys are not files in git: they go straight to the gateway dashboard's
``save_key``, which writes the right .env and never returns the value.
"""

from __future__ import annotations

import difflib
import hashlib
import importlib.util
import json
import re
import shutil
import subprocess
import tempfile
import threading
import tomllib
from pathlib import Path

from team import grade, profiles, roles, settings

SETTINGS = "team/orchestration.toml"
CATALOG = "integrations/portkey/catalog.json"
ROLE_FILE = re.compile(r"^team/roles/([a-z][a-z0-9_-]*)/(role\.toml|SOUL\.md|skills/[a-z0-9][a-z0-9_-]*/SKILL\.md)$")
MARK = "Team app:"
lock = threading.Lock()


class EditError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def editable(rel: str) -> re.Match | None:
    """The role a path belongs to (group 1), or None for settings; refuses anything else."""
    if rel == SETTINGS:
        return None
    m = ROLE_FILE.match(rel)
    if not m or m.group(1) == "_base":
        raise EditError(f"{rel} is not a file this app edits")
    return m


def _text(root: Path, rel: str) -> str:
    p = Path(root) / rel
    return p.read_text() if p.exists() else ""


def roster(root: Path, cat: dict | None) -> dict:
    """Every role with its files and what it runs on, and every setting with its value."""
    out = []
    for name in roles.names(root):
        base = Path(root) / "team" / "roles" / name
        files = [f"team/roles/{name}/role.toml"]
        if (base / "SOUL.md").exists():
            files.append(f"team/roles/{name}/SOUL.md")
        files += [f"team/roles/{name}/skills/{s}/SKILL.md" for s in roles.skills(root, name)
                  if (base / "skills" / s / "SKILL.md").exists()]
        raw = tomllib.loads((base / "role.toml").read_text())
        try:
            roles.load(root, name, cat)
            problem = None
        except roles.RoleError as e:
            problem = str(e)
        out.append({
            "name": name, "title": raw.get("title", name), "does": raw.get("does", ""),
            "status": raw.get("status"), "runs_on": raw.get("runs_on", {}),
            "escalation": raw.get("escalation"), "models": raw.get("models"),
            "tools": raw.get("tools", []), "budget": raw.get("budget", {}),
            "files": files, "problem": problem,
        })
    try:
        values = settings.load(root)
        problem = None
    except settings.SettingsError as e:
        values, problem = {}, str(e)
    return {"roles": out, "settings": {"fields": settings.fields(), "values": values,
                                       "path": SETTINGS, "problem": problem}}


def read(root: Path, rel: str) -> dict:
    editable(rel)
    text = _text(root, rel)
    return {"path": rel, "text": text, "base": digest(text)}


def settings_text(root: Path, values: dict) -> str:
    """orchestration.toml with these settings changed (each checked against its type)."""
    fields = {f["key"]: f for f in settings.fields()}
    data = settings._read(root)
    for key, value in values.items():
        if key not in fields:
            raise EditError(f"unknown setting: {key}")
        try:
            value = settings._coerce(key, settings.FIELDS[key][0], value)
        except settings.SettingsError as e:
            raise EditError(str(e)) from None
        section, name = key.split(".", 1)
        data.setdefault(section, {})[name] = value
    return settings._dump(data)


def diff(old: str, new: str, rel: str) -> str:
    return "".join(difflib.unified_diff(
        old.splitlines(keepends=True), new.splitlines(keepends=True), f"a/{rel}", f"b/{rel}"))


def check(root: Path, rel: str, text: str, cat: dict | None) -> list[str]:
    """Team's own checks on the changed file, run against a copy of the roles and settings."""
    m = editable(rel)
    if rel.endswith(".md"):
        return [] if text.strip() else [f"{rel} would be empty"]
    with tempfile.TemporaryDirectory() as tmp:
        sandbox = Path(tmp)
        shutil.copytree(Path(root) / "team" / "roles", sandbox / "team" / "roles")
        if (Path(root) / SETTINGS).exists():
            shutil.copy(Path(root) / SETTINGS, sandbox / SETTINGS)
        (sandbox / rel).write_text(text)
        try:
            if m is None:
                settings.load(sandbox)
                return []
            role = roles.load(sandbox, m.group(1), cat)
            if role["runs_on"]["harness"] == "claude":
                return []
            return profiles.audit(profiles.role_config(sandbox, role, cat))
        except (roles.RoleError, settings.SettingsError, tomllib.TOMLDecodeError,
                KeyError, TypeError, ValueError) as e:
            return [str(e)]


def review(root: Path, rel: str, text: str, cat: dict | None) -> dict:
    old = _text(root, rel)
    return {"path": rel, "diff": diff(old, text, rel), "problems": check(root, rel, text, cat),
            "base": digest(old), "dirty": dirty(root, rel)}


def dirty(root: Path, rel: str) -> bool:
    r = subprocess.run(["git", "-C", str(root), "status", "--porcelain", "--", rel],
                       capture_output=True, text=True, check=False)
    return bool(r.stdout.strip())


def _guard(root: Path, rel: str, base: str) -> None:
    if digest(_text(root, rel)) != base:
        raise EditError(f"{rel} changed on disk since you opened it; reload, then redo the edit", 409)
    if dirty(root, rel):
        raise EditError(
            f"{rel} has uncommitted changes from someone else (another session?); "
            "commit or discard them first so this apply does not sweep them in", 409)


def apply(root: Path, rel: str, text: str, base: str, cat: dict | None, sync=profiles.sync) -> dict:
    with lock:
        m = editable(rel)
        _guard(root, rel, base)
        problems = check(root, rel, text, cat)
        if problems:
            raise EditError("refused: " + "; ".join(problems))
        (Path(root) / rel).write_text(text)
        synced = []
        if m is not None:
            synced = sync(root, m.group(1), cat)
        sha = grade.commit_paths(root, f"{MARK} edit {rel}", rel)
        return {"path": rel, "commit": sha, "synced": synced, "base": digest(text)}


# --- the gateway: LiteLLM and Portkey, through the gateway dashboard's gates ---

_gw = None


def gateway(root: Path):
    """integrations/dashboard/server.py, loaded once: build.py's checks, keys, restarts, probes."""
    global _gw
    if _gw is None:
        spec = importlib.util.spec_from_file_location(
            "team_gateway_dashboard", Path(root) / "integrations" / "dashboard" / "server.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _gw = mod
    return _gw


def _gw_call(fn, *args, **kw):
    gw = _gw
    try:
        return fn(*args, **kw)
    except gw.Problem as e:
        raise EditError(str(e), getattr(e, "status", 400)) from None


def gateway_review(root: Path, catalog: dict) -> dict:
    gw = gateway(root)
    old = gw.catalog_text()
    files = _gw_call(gw.render, catalog)
    return {"path": CATALOG, "diff": diff(old, gw.dump_catalog(catalog), CATALOG),
            "problems": [], "summary": gw.summary(catalog, files), "dirty": dirty(root, CATALOG)}


def gateway_apply(root: Path, catalog: dict, base: str) -> dict:
    gw = gateway(root)
    with lock:
        if dirty(root, CATALOG):
            raise EditError(
                f"{CATALOG} has uncommitted changes from someone else; commit or discard them first", 409)
        result = _gw_call(gw.save_catalog, catalog, base)
        result["commit"] = grade.commit_paths(root, f"{MARK} edit {CATALOG}", CATALOG)
        return result


def history(root: Path, n: int = 20) -> list[dict]:
    r = subprocess.run(
        ["git", "-C", str(root), "log", f"-{n}", "--format=%H%x09%cI%x09%s", f"--grep={MARK}", "--fixed-strings"],
        capture_output=True, text=True, check=False)
    rows = []
    for line in r.stdout.splitlines():
        sha, when, subject = line.split("\t", 2)
        rows.append({"commit": sha, "at": when, "subject": subject,
                     "path": subject.split(" edit ", 1)[-1]})
    return rows


def undo(root: Path, sha: str, cat: dict | None, sync=profiles.sync) -> dict:
    """Revert one app commit (only its file), then sync or rebuild as an apply would."""
    entry = next((h for h in history(root, 200) if h["commit"] == sha), None)
    if entry is None:
        raise EditError("only a change made in this app can be undone here")
    rel = entry["path"]
    with lock:
        if dirty(root, rel):
            raise EditError(f"{rel} has uncommitted changes; commit or discard them first", 409)
        old = subprocess.run(["git", "-C", str(root), "show", f"{sha}^:{rel}"],
                             capture_output=True, text=True, check=False)
        if old.returncode != 0:
            raise EditError(f"cannot read {rel} before {sha[:10]}")
        if rel == CATALOG:
            gw = gateway(root)
            catalog = json.loads(old.stdout)
            _gw_call(gw.save_catalog, catalog, gw.digest(gw.catalog_text()))
        else:
            m = editable(rel)
            problems = check(root, rel, old.stdout, cat)
            if problems:
                raise EditError("refused: " + "; ".join(problems))
            (Path(root) / rel).write_text(old.stdout)
            if m is not None:
                sync(root, m.group(1), cat)
        return {"path": rel, "commit": grade.commit_paths(root, f"{MARK} undo {sha[:10]}, edit {rel}", rel)}
