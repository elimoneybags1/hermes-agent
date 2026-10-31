"""Hermes profiles for Team roles, generated from ``team/roles/``.

A role runs as the Hermes profile of the same name under Team's Hermes home
(``$TEAM_STATE/hermes/profiles/<role>``). ``sync`` makes the profile exist
and overwrites the files Team versions: ``config.yaml`` (the locked base in
``team/roles/_base/config.yaml`` plus the role's overrides), ``SOUL.md``, and
the role's skills. Everything the profile learned on its own (memories, other
skills, sessions) is left alone, so versioned files reach every worker on
every launch without masking what it learned.

``audit`` is the lock: it lists every way a config could reach a paid model.
``team gate audit`` runs it against the real profiles on every tick.
"""

from __future__ import annotations

import copy
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import hermes_yaml as yaml

from team import catalog, kanban, roles
from team.ledger import state_dir

Runner = Callable[..., subprocess.CompletedProcess[Any]]

ALLOWED_PROVIDERS = ("litellm", "team-subscription")
# Secrets a profile's .env may hold: both are loopback tokens, never a provider key.
ALLOWED_ENV = ("SUBSCRIPTION_TOKEN", "LITELLM_MASTER_KEY")
AUX_TASKS = (
    "vision",
    "compression",
    "skills_hub",
    "approval",
    "review",
    "mcp",
    "title_generation",
    "memory_query_rewrite",
    "tts_audio_tags",
    "voice_chat",
    "triage_specifier",
    "kanban_decomposer",
    "profile_describer",
    "goal_judge",
    "curator",
    "monitor",
    "background_review",
    "moa_reference",
    "moa_aggregator",
)
LEARNING_TOOLSETS = ("skills", "memory", "session_search", "todo")


class ProfileError(RuntimeError):
    """A profile that cannot be created or synced."""


def hermes_home() -> Path:
    return state_dir() / "hermes"


def profile_dir(role: str) -> Path:
    return hermes_home() / "profiles" / role


def base_config(root: Path) -> dict:
    path = Path(root) / "team" / "roles" / "_base" / "config.yaml"
    data = yaml.safe_load(path.read_text())
    if not isinstance(data, dict):
        raise ProfileError(f"{path} is not a mapping")
    return data


def role_config(root: Path, role: dict, cat: dict | None = None) -> dict:
    """The base lock with this role's overrides applied."""
    cfg = copy.deepcopy(base_config(root))
    harness = role.get("runs_on", {}).get("harness", "hermes")
    if harness == "hermes":
        spec = roles.candidate(role, 1)
        if spec:
            choice = catalog.resolve(spec, cat) if cat is not None else None
            cfg["model"] = {
                "provider": "litellm",
                "default": choice.model if choice else spec,
            }
    else:
        model = role.get("runs_on", {}).get("model") or "claude-opus-5-5"
        cfg["providers"]["team-subscription"] = {
            "base_url": cfg["providers"]["litellm"]["base_url"],
            "api_key": "${LITELLM_MASTER_KEY}",
            "key_env": "LITELLM_MASTER_KEY",
            "api_mode": "chat_completions",
            "model": model,
            "models": [model],
            "session_affinity_header": "x-litellm-session-id",
        }
        cfg["model"] = {"provider": "team-subscription", "default": model}
    tools = list(role.get("tools") or [])
    cfg.setdefault("platform_toolsets", {})["cli"] = sorted(
        set(tools) | (set(LEARNING_TOOLSETS) if harness == "hermes" else set())
    )
    cfg.setdefault("agent", {})["max_turns"] = int(role["budget"]["max_turns"])
    _merge(cfg, role.get("hermes") or {})
    return cfg


def _merge(into: dict, extra: dict) -> None:
    """A role's ``[hermes]`` table over the config; the lock audit still runs on the result."""
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(into.get(key), dict):
            _merge(into[key], value)
        else:
            into[key] = copy.deepcopy(value)


# --- the lock ----------------------------------------------------------------


def audit(cfg: dict) -> list[str]:
    """Every way this config could reach a paid or unpinned model; empty is clean."""
    out: list[str] = []
    model = cfg.get("model") if isinstance(cfg.get("model"), dict) else {}
    provider = model.get("provider")
    if not provider:
        out.append("model.provider is missing")
    elif provider == "auto" or provider not in ALLOWED_PROVIDERS:
        out.append(f"model.provider is {provider!r}, not one of {ALLOWED_PROVIDERS}")
    if cfg.get("fallback_providers"):
        out.append("fallback_providers is not empty")
    for name, entry in (cfg.get("providers") or {}).items():
        if name not in ALLOWED_PROVIDERS:
            out.append(f"providers.{name} is not one of {ALLOWED_PROVIDERS}")
        key = entry.get("api_key") if isinstance(entry, dict) else None
        if key and not (str(key).startswith("${") and str(key).endswith("}")):
            out.append(
                f"providers.{name} holds an api_key value; use ${{VAR}} or key_env"
            )
        if isinstance(entry, dict) and not str(entry.get("base_url", "")).startswith(
            "http://127.0.0.1"
        ):
            out.append(f"providers.{name}.base_url is not loopback")
    aux = cfg.get("auxiliary") or {}
    if aux.get("free_only") is not True:
        out.append("auxiliary.free_only is not true")
    for task in AUX_TASKS:
        entry = aux.get(task)
        if not isinstance(entry, dict):
            out.append(f"auxiliary.{task} is missing")
            continue
        p = entry.get("provider")
        if p not in ALLOWED_PROVIDERS:
            out.append(
                f"auxiliary.{task}.provider is {p!r}, not one of {ALLOWED_PROVIDERS}"
            )
        if not entry.get("model"):
            out.append(f"auxiliary.{task}.model is empty")
    if (
        isinstance(aux.get("background_review"), dict)
        and aux["background_review"].get("enabled") is not False
    ):
        out.append("auxiliary.background_review.enabled is not false")
    if (cfg.get("curator") or {}).get("enabled") is not False:
        out.append("curator.enabled is not false")
    if (cfg.get("moa") or {}).get("active_preset"):
        out.append("moa.active_preset is not empty")
    deleg = cfg.get("delegation") or {}
    if deleg.get("provider") not in ALLOWED_PROVIDERS:
        out.append(
            f"delegation.provider is {deleg.get('provider')!r}, not one of {ALLOWED_PROVIDERS}"
        )
    if deleg.get("fallback_providers"):
        out.append("delegation.fallback_providers is not empty")
    if (cfg.get("cron") or {}).get("model_provider") not in ALLOWED_PROVIDERS:
        out.append("cron.model_provider is not pinned")
    kb = cfg.get("kanban") or {}
    for key in ("dispatch_in_gateway", "review_dispatch", "auto_decompose"):
        if kb.get(key) is not False:
            out.append(f"kanban.{key} is not false")
    if "team" not in ((cfg.get("plugins") or {}).get("enabled") or []):
        out.append("plugins.enabled does not include team")
    return out


def audit_env(path: Path) -> list[str]:
    """Names in a profile ``.env`` that no Team profile may hold."""
    path = Path(path)
    if not path.exists():
        return []
    bad = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name = line.split("=", 1)[0].strip()
        if name not in ALLOWED_ENV:
            bad.append(f".env holds {name}, which no Team profile may have")
    return sorted(bad)


def audit_profiles(root: Path) -> dict[str, list[str]]:
    """Findings per profile under Team's Hermes home, the root home included."""
    findings: dict[str, list[str]] = {}
    homes = {"default": hermes_home()}
    pdir = hermes_home() / "profiles"
    if pdir.is_dir():
        for d in sorted(pdir.iterdir()):
            if d.is_dir() and (d / "config.yaml").exists():
                homes[d.name] = d
    for name, home in homes.items():
        cfg_path = home / "config.yaml"
        if not cfg_path.exists():
            findings[name] = ["config.yaml is missing"]
            continue
        try:
            cfg = yaml.safe_load(cfg_path.read_text()) or {}
        except Exception as e:
            findings[name] = [f"config.yaml unreadable: {e}"]
            continue
        found = audit(cfg) + audit_env(home / ".env")
        if found:
            findings[name] = found
    return findings


# --- sync ---------------------------------------------------------------------


def _write(path: Path, text: str) -> bool:
    """Write when different; True when something changed."""
    if path.exists() and path.read_text() == text:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return True


def write_root_config(root: Path) -> bool:
    """The root home's config.yaml is the base lock itself (the kanban dispatcher runs there)."""
    cfg = base_config(root)
    cfg.setdefault("platform_toolsets", {})["cli"] = sorted(
        set(LEARNING_TOOLSETS) | {"kanban"}
    )
    changed = _write(hermes_home() / "config.yaml", yaml.safe_dump(cfg))
    return write_env(hermes_home()) or changed


def litellm_master_key() -> str | None:
    """The LiteLLM master key from ``$TEAM_STATE/litellm/.env``, or None."""
    path = state_dir() / "litellm" / ".env"
    if not path.exists():
        return None
    for line in path.read_text().splitlines():
        if line.startswith("LITELLM_MASTER_KEY="):
            return line.split("=", 1)[1].strip().strip("'\"") or None
    return None


def write_env(home: Path) -> bool:
    """A ``.env`` holding only the loopback secrets Team allows; Hermes loads it on start."""
    key = litellm_master_key()
    if key is None:
        return False
    path = home / ".env"
    kept: list[str] = []
    if path.exists():
        for line in path.read_text().splitlines():
            name = line.split("=", 1)[0]
            if name in ALLOWED_ENV and name != "LITELLM_MASTER_KEY":
                kept.append(line)
    text = "\n".join([*kept, f"LITELLM_MASTER_KEY={key}"]) + "\n"
    changed = _write(path, text)
    path.chmod(0o600)
    return changed


def sync(
    root: Path, role_name: str, cat: dict | None = None, runner: Runner = subprocess.run
) -> list[str]:
    """Make the role's profile match its files; returns what changed."""
    root = Path(root)
    role = roles.load(root, role_name, cat)
    if role["runs_on"]["harness"] == "claude":
        return []  # runs through Claude Code, not a Hermes profile
    changes: list[str] = []
    if write_root_config(root):
        changes.append("root config.yaml")
    pdir = profile_dir(role_name)
    if not pdir.exists():
        result = runner(
            [
                str(kanban.wrapper()),
                "profile",
                "create",
                role_name,
                "--clone-from",
                "default",
                "--no-alias",
                "--description",
                role["does"],
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0 or not pdir.exists():
            raise ProfileError(
                f"hermes profile create {role_name} failed: {(result.stderr or result.stdout)[-800:]}"
            )
        changes.append(f"created profile {role_name}")
    cfg = role_config(root, role, cat)
    if role["runs_on"]["harness"] == "hermes-subscription":
        from integrations.subscription.setup import install_profile

        install_profile(pdir)
    if _write(pdir / "config.yaml", yaml.safe_dump(cfg)):
        changes.append("config.yaml")
    if write_env(pdir):
        changes.append(".env")
    soul = roles.soul(root, role_name)
    if soul is not None and _write(pdir / "SOUL.md", soul):
        changes.append("SOUL.md")
    for skill in role["skills"]:
        src = root / "team" / "roles" / role_name / "skills" / skill
        dst = pdir / "skills" / skill
        if dst.exists() and _same_tree(src, dst):
            continue
        if dst.exists():
            shutil.rmtree(dst)
        shutil.copytree(src, dst)
        changes.append(f"skill {skill}")
    return changes


def _same_tree(a: Path, b: Path) -> bool:
    files_a = {p.relative_to(a): p.read_bytes() for p in a.rglob("*") if p.is_file()}
    files_b = {p.relative_to(b): p.read_bytes() for p in b.rglob("*") if p.is_file()}
    return files_a == files_b


def sync_all(
    root: Path, cat: dict | None = None, runner: Runner = subprocess.run
) -> dict[str, list[str]]:
    return {name: sync(root, name, cat, runner) for name in roles.names(root)}
