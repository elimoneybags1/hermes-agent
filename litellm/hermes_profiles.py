"""Point Team's Hermes worker profiles at the LiteLLM proxy, or back.

    hermes_profiles.py           team-* profiles call team-auto through 127.0.0.1:4000
    hermes_profiles.py --undo    restore each profile's config from its .pre-litellm backup
    hermes_profiles.py --status  show where each profile points

Each worker sends x-litellm-session-id = its kanban task id (HERMES_KANBAN_TASK,
set by the kanban runner), so the proxy pins one model per task and usage.db rows
join to Team's jobs. Run with Team's interpreter (.venv/bin/python): needs PyYAML.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import yaml

PROFILES = Path.home() / ".hermes" / "profiles"
PROXY_ENV = Path.home() / "state" / "team" / "litellm" / ".env"
PROVIDER = {
    "name": "team-litellm",
    "base_url": "http://127.0.0.1:4000/v1",
    "api_key": "${TEAM_LITELLM_KEY}",
    "api_mode": "chat_completions",
    "extra_headers": {"x-litellm-session-id": "${HERMES_KANBAN_TASK}"},
    # Hermes compresses at half of this, so prompts stay under ~32K tokens instead of
    # growing to ~64K (goal 18 reached 62K uncompressed); well inside every model's window.
    "context_length": 64000,
}
# One big read is resent on every later turn (goal 18: an 18K-char JSON file); read in ranges.
FILE_READ_MAX_CHARS = 20000
MODEL = "team-auto"
# Hermes's own side jobs run on the cheapest tier, through the same provider so they
# carry the task's session header. Team dispatches the main work on a tier (-m).
AUXILIARY = ("compression", "session_search", "title_generation", "curator", "background_review")
SIDE_MODEL = "tier-simple"


def profiles() -> list[Path]:
    return sorted(p for p in PROFILES.glob("team-*") if (p / "config.yaml").exists())


def master_key() -> str:
    for line in PROXY_ENV.read_text().splitlines():
        if line.startswith("LITELLM_MASTER_KEY="):
            return line.split("=", 1)[1].strip()
    sys.exit(f"no LITELLM_MASTER_KEY in {PROXY_ENV}")


def set_env(path: Path, key: str, value: str) -> None:
    lines = path.read_text().splitlines() if path.exists() else []
    lines = [l for l in lines if not l.startswith(f"{key}=")] + [f"{key}={value}"]
    path.write_text("\n".join(lines) + "\n")
    path.chmod(0o600)


def connect(p: Path, key: str) -> None:
    cfg = p / "config.yaml"
    backup = p / "config.yaml.pre-litellm"
    if not backup.exists():
        shutil.copy2(cfg, backup)
    data = yaml.safe_load(cfg.read_text()) or {}
    others = [e for e in data.get("custom_providers") or [] if e.get("name") != PROVIDER["name"]]
    data["custom_providers"] = others + [dict(PROVIDER)]
    data["model"] = {"provider": PROVIDER["name"], "default": MODEL}
    data["fallback_providers"] = []
    data["fallback_model"] = None
    data["file_read_max_chars"] = FILE_READ_MAX_CHARS
    aux = data.setdefault("auxiliary", {})
    aux.pop("openrouter_model", None)
    for task in AUXILIARY:
        aux.setdefault(task, {}).update(provider=PROVIDER["name"], model=SIDE_MODEL, fallback_chain=[])
    cfg.write_text(yaml.safe_dump(data, sort_keys=False))
    set_env(p / ".env", "TEAM_LITELLM_KEY", key)


def undo(p: Path) -> None:
    backup = p / "config.yaml.pre-litellm"
    if backup.exists():
        shutil.copy2(backup, p / "config.yaml")
        backup.unlink()


def status(p: Path) -> str:
    m = (yaml.safe_load((p / "config.yaml").read_text()) or {}).get("model") or {}
    return f"{p.name:16} provider={m.get('provider')} model={m.get('default')}"


def main() -> None:
    found = profiles()
    if not found:
        sys.exit(f"no team-* profiles under {PROFILES}")
    if "--status" in sys.argv:
        pass
    elif "--undo" in sys.argv:
        for p in found:
            undo(p)
    else:
        key = master_key()
        for p in found:
            connect(p, key)
    for p in found:
        print(status(p))


if __name__ == "__main__":
    main()
