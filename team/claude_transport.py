"""Subscription-only, tool-free Claude CLI transport. No agent policy lives here."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

SCRUB = (
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
    "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY",
    "CLAUDE_CODE_SIMPLE", "CLAUDE_CONFIG_DIR",
)


class TransportError(RuntimeError):
    pass


def clean_env(env=None):
    return {k: v for k, v in (os.environ if env is None else env).items() if k not in SCRUB}


def subscription_ready(cwd: Path, runner=subprocess.run, timeout_s=30):
    proc = runner(["claude", "auth", "status"], cwd=cwd, env=clean_env(),
                  capture_output=True, text=True, timeout=timeout_s)
    try:
        data = json.loads(proc.stdout)
    except (ValueError, TypeError):
        raise TransportError("Cannot verify Claude subscription authentication") from None
    if (proc.returncode or not isinstance(data, dict) or not data.get("loggedIn")
            or data.get("authMethod") != "claude.ai" or data.get("apiProvider") != "firstParty"):
        raise TransportError("Claude subscription login required; run `claude auth login`")


def request(cwd: Path, system: str, messages: list[dict], schema: dict,
            *, model=None, timeout_s=120, runner=subprocess.run):
    cmd = ["claude", "-p", "--output-format", "json", "--safe-mode",
           "--setting-sources", "", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
           "--tools", "", "--disable-slash-commands", "--no-chrome",
           "--no-session-persistence", "--max-turns", "3",
           "--system-prompt", system, "--json-schema", json.dumps(schema)]
    if model and model != "claude":
        cmd += ["--model", model]
    proc = runner(cmd, cwd=cwd, env=clean_env(), input=json.dumps(messages),
                  capture_output=True, text=True, timeout=timeout_s)
    if proc.returncode:
        raise TransportError((proc.stderr or proc.stdout).strip()[:2000])
    try:
        data = json.loads(proc.stdout)
    except (ValueError, TypeError):
        raise TransportError("Claude returned invalid JSON") from None
    if not isinstance(data, dict) or data.get("is_error"):
        raise TransportError(f"Claude failed: {str(data)[:2000]}")
    action = data.get("structured_output")
    if not isinstance(action, dict):
        raise TransportError("Claude returned no structured action")
    return action, data.get("usage") or {}
