"""Launch the full forked Hermes AIAgent with persistent role profiles and Team overlays."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
import uuid
from pathlib import Path

import yaml

from team import claude_transport, ledger
from team.subscription_gateway import serve

ROOT = Path(__file__).resolve().parent.parent
FORK = ROOT / "vendor" / "hermes-agent"


def python_executable() -> Path:
    for path in (FORK / ".venv/bin/python", FORK / "venv/bin/python", Path.home() / ".hermes/hermes-agent/venv/bin/python"):
        if path.is_file():
            return path
    raise RuntimeError("Install the fork's Python dependencies before running Team")


def native_profile_root() -> Path:
    """Use upstream's root resolution, including isolated/custom HERMES_HOME."""
    import importlib.util
    name = '_team_hermes_constants'
    module = sys.modules.get('hermes_constants') or sys.modules.get(name)
    if module is None:
        spec = importlib.util.spec_from_file_location(name, FORK / 'hermes_constants.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        sys.modules[name] = module
    return module.get_default_hermes_root()


def worker_profile_root() -> Path:
    return Path(os.environ['TEAM_WORKER_HERMES_HOME']) if os.environ.get('TEAM_WORKER_HERMES_HOME') else native_profile_root()


def worker_environment(env=None) -> dict:
    result = fork_environment(env)
    result['HERMES_HOME'] = str(worker_profile_root())
    result.pop('HERMES_PROFILE', None)
    if result.get('TEAM_INFERENCE_URL'):
        # Subscription credentials are local transport tokens, not the worker's
        # provider credentials. Let the worker profile resolve its own route.
        for key in ('OPENAI_BASE_URL', 'OPENAI_API_KEY', 'TEAM_INFERENCE_URL', 'TEAM_INFERENCE_TOKEN'):
            result.pop(key, None)
    return result


def prepare_profile(role: str, root: Path = ROOT) -> Path:
    manifest = root / "roles" / role / "role.toml"
    if not manifest.is_file() or '/' in role or role in {'.', '..'}:
        raise ValueError("unknown Team role")
    if not (FORK / "run_agent.py").is_file():
        raise RuntimeError("Hermes checkout missing; run git submodule update --init vendor/hermes-agent")
    home = ledger.state_dir() / "hermes" / "profiles" / role
    home.mkdir(parents=True, exist_ok=True)
    # Seed once, not on every launch: learned skills, memories, and local tuning survive.
    soul = home / "SOUL.md"
    if not soul.exists():
        soul.write_text((FORK / "SOUL.md").read_text())
    if not (home / "skills").exists():
        shutil.copytree(FORK / "skills", home / "skills")
    skill_dir = root / "roles" / role / "skills"
    for source in skill_dir.glob("*/SKILL.md"):
        destination = home / "skills" / "team" / source.parent.name
        if not destination.exists():
            shutil.copytree(source.parent, destination)
    config_file = home / "config.yaml"
    if not config_file.exists():
        config = {
            "model": {"provider": "custom", "default": "opus", "api_mode": "chat_completions",
                      "base_url": "${TEAM_INFERENCE_URL}", "api_key": "${TEAM_INFERENCE_TOKEN}",
                      "streaming": False, "context_length": 160000},
            "fallback_providers": [], "fallback_model": None,
            "auxiliary": {"free_only": True},
            "memory": {"memory_enabled": True, "user_profile_enabled": True},
            "compression": {"enabled": True},
            "agent": {"coding_context": "focus" if role == "boss" else "auto"},
            "kanban": {"dispatch_in_gateway": False, "auto_decompose": False, "review_dispatch": False},
        }
        for task in ("compression", "session_search", "title_generation", "curator", "background_review", "kanban_decomposer", "triage_specifier"):
            config["auxiliary"][task] = {"provider": "custom", "model": "opus", "base_url": "${TEAM_INFERENCE_URL}",
                                         "api_key": "${TEAM_INFERENCE_TOKEN}", "fallback_chain": []}
        config["auxiliary"]["background_review"]["enabled"] = role not in {"boss", "optimizer"}
        config_file.write_text(yaml.safe_dump(config, sort_keys=False))
    else:
        config = yaml.safe_load(config_file.read_text()) or {}
        if "coding_context" in config:
            old = config.pop("coding_context")
            config.setdefault("agent", {}).setdefault("coding_context", "focus" if role == "boss" else old)
            config_file.write_text(yaml.safe_dump(config, sort_keys=False))
    if role == "boss":
        config = yaml.safe_load(config_file.read_text()) or {}
        search = config.setdefault("tools", {}).setdefault("tool_search", {})
        if isinstance(search, dict) and not search:
            # Native progressive disclosure: the full library stays installed,
            # but its catalog and management schemas load only on request.
            search.update(enabled="on", listing_max_tokens=1000,
                          defer=["skills_list", "skill_view", "skill_manage", "session_search", "todo_list"])
            config_file.write_text(yaml.safe_dump(config, sort_keys=False))
    return home


def runtime_env(home: Path, gateway: dict) -> dict:
    # Start with an execution environment, not the user's provider credentials.
    keep = {"PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LC_ALL", "TERM", "TMPDIR", "SSH_AUTH_SOCK"}
    env = {k: v for k, v in os.environ.items() if k in keep}
    env.update(HERMES_HOME=str(home), HERMES_PROFILE=home.name,
               HERMES_KANBAN_HOME=str(ledger.state_dir() / "kanban"),
               TEAM_STATE=str(ledger.state_dir()), TEAM_WORKER_HERMES_HOME=str(worker_profile_root()),
               TEAM_INFERENCE_URL=gateway["url"],
               TEAM_INFERENCE_TOKEN=gateway["token"], OPENAI_BASE_URL=gateway["url"],
               OPENAI_API_KEY=gateway["token"], PYTHONUNBUFFERED="1")
    return fork_environment(env)


def launch(role: str, *, root=ROOT, cwd=None, prompt=None, resume=None, model=None,
           max_turns=40, timeout_s=1800, runner=subprocess.run, capture=False, decision_file=None, result_file=None, initial_prompt=None):
    cwd = Path(cwd or root).resolve()
    home = prepare_profile(role, Path(root))
    claude_transport.subscription_ready(cwd, runner=runner)
    with serve(cwd, timeout_s=timeout_s, trace_path=home / "inference.jsonl") as gateway:
        payload = dict(role=role, root=str(root), cwd=str(cwd), prompt=prompt, resume=resume,
                       model=model or "opus", max_turns=max_turns, timeout_s=timeout_s, initial_prompt=initial_prompt,
                       decision_file=str(decision_file) if decision_file else None,
                       result_file=str(result_file) if result_file else None)
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", dir=home, delete=False) as f:
            json.dump(payload, f)
            payload_path = Path(f.name)
        try:
            cmd = [str(python_executable()), str(ROOT / "team/hermes_native.py"), str(payload_path)]
            kwargs = dict(cwd=cwd, env=runtime_env(home, gateway), text=True)
            if capture:
                kwargs.update(capture_output=True, timeout=timeout_s + 75, stdin=subprocess.DEVNULL)
            proc = runner(cmd, **kwargs)
            return proc, list(gateway["calls"])
        finally:
            payload_path.unlink(missing_ok=True)


def boss(root: Path, argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="team --")
    parser.add_argument("--model", default="opus")
    parser.add_argument("--resume")
    parser.add_argument("--max-turns", type=int, default=40)
    parser.add_argument("--timeout", type=int, default=1800)
    parser.add_argument("--prompt")
    args = parser.parse_args(argv)
    try:
        proc, _ = launch("boss", root=root, prompt=args.prompt, resume=args.resume,
                         model=args.model, max_turns=args.max_turns, timeout_s=args.timeout)
        return proc.returncode
    except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
        print(f"Cannot start Hermes-backed boss: {exc}", file=sys.stderr)
        return 1


def fork_environment(env=None) -> dict:
    """Use the checked-in fork for Team's normal Hermes CLI and dispatched workers."""
    result = dict(os.environ if env is None else env)
    if (FORK / "hermes").is_file():
        interpreter_dir = python_executable().parent
        result["PATH"] = os.pathsep.join([str(FORK), str(interpreter_dir), result.get("PATH", "")])
        result["HERMES_BIN"] = str(FORK / "hermes")
    return result
