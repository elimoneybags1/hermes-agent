"""Isolated Team workers using Hermes through LiteLLM and Portkey."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

from team import claude_roles, claude_run, profiles, roles

LEAD_MODEL = "claude-sonnet-5-5"
RESCUE_MODEL = "claude-opus-5-5"


def run(
    worktree,
    brief,
    *,
    max_turns=60,
    timeout_s=1800,
    runner=subprocess.run,
    role=None,
    root=None,
    model=None,
    json_schema=None,
    allowed_files=None,
    check=None,
    decision_file=None,
) -> dict[str, Any]:
    root = Path(root or Path(__file__).resolve().parents[1])
    spec = roles.load(root, role)
    selected = model or spec.get("runs_on" if role == "lead" else "escalation", {}).get(
        "model"
    )
    selected = selected or (LEAD_MODEL if role == "lead" else RESCUE_MODEL)
    session = "team-" + uuid.uuid4().hex
    identity = {"session_id": session, "model": selected}
    request = dict(
        session_id=session,
        brief=brief,
        root=str(root),
        role=role,
        model=selected,
        max_turns=max_turns,
        timeout_s=timeout_s,
        schema=json_schema,
        allowed_files=allowed_files,
        check=check,
        decision_file=str(decision_file or ""),
    )
    env = claude_run.clean_env()
    # A nested planner must not inherit the boss's profile or kanban ownership.
    for key in list(env):
        if key.startswith("HERMES_KANBAN_") or key == "HERMES_PROFILE":
            env.pop(key)
    env["PYTHONPATH"] = os.pathsep.join(
        filter(None, [str(Path(__file__).resolve().parents[1]), env.get("PYTHONPATH")])
    )
    if decision_file:
        env["TEAM_DECISION_FILE"] = str(decision_file)
    start = time.monotonic()
    try:
        proc = runner(
            [sys.executable, "-P", "-m", "team.hermes_run", json.dumps(request)],
            cwd=worktree,
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired:
        return claude_run._result(
            False,
            f"timeout after {timeout_s}s",
            int((time.monotonic() - start) * 1000),
            **identity,
        )
    duration = int((time.monotonic() - start) * 1000)
    if proc.returncode:
        return claude_run._result(
            False, (proc.stderr or proc.stdout)[-2000:], duration, **identity
        )
    try:
        data = json.loads(proc.stdout)
    except ValueError:
        return claude_run._result(
            False, "worker output is not valid JSON", duration, **identity
        )
    return claude_run._result(
        not data.get("is_error", False),
        data.get("result"),
        duration,
        session_id=data.get("session_id") or session,
        usage=data.get("usage"),
        structured=data.get("structured_output"),
        num_turns=data.get("num_turns"),
        api_error_status=data.get("api_error_status"),
        model=selected,
        response_model=data.get("response_model"),
        tools=data.get("tools"),
    )


def execute(request: dict, home: Path) -> dict:
    # Set the isolated home before importing Hermes's config/provider modules.
    os.environ["HERMES_HOME"] = str(home)
    os.environ["HERMES_PROFILE"] = request["role"]
    os.environ["TERMINAL_CWD"] = str(Path.cwd())
    import hermes_yaml as yaml
    from integrations.subscription.setup import install_profile

    root = Path(request["root"])
    spec = roles.load(root, request["role"])
    spec["runs_on"] = {"harness": "hermes-subscription", "model": request["model"]}
    cfg = profiles.role_config(root, spec)
    cfg["memory"] = {"memory_enabled": False, "user_profile_enabled": False}
    cfg["mcp_servers"] = {}
    (home / "config.yaml").write_text(yaml.safe_dump(cfg))
    key = profiles.litellm_master_key()
    if not key:
        raise RuntimeError("LiteLLM master key is missing; run the gateway setup")
    profiles.write_env(home)
    install_profile(home)
    from toolsets import create_custom_toolset
    from run_agent import AIAgent

    readonly = request["role"] == "lead"
    names = (
        ["read_file", "search_files"]
        if readonly
        else [
            "read_file",
            "search_files",
            "write_file",
            "patch",
            "terminal",
            "process_manage",
        ]
    )
    create_custom_toolset("team-worker", "Team's bounded worker tools", names)
    soul = roles.soul(root, request["role"]) or ""
    if not readonly:
        soul += (
            "\n"
            + claude_roles.BUILD_RULES
            + "\nReturn a summary when finished; Team performs the review handoff."
        )
    for skill in spec["skills"]:
        soul += (
            "\n"
            + (
                roles.roles_dir(root) / request["role"] / "skills" / skill / "SKILL.md"
            ).read_text()
        )
    if request.get("schema"):
        soul += "\nReturn only JSON matching this schema:\n" + json.dumps(
            request["schema"]
        )
    if request.get("check"):
        soul += "\nRequired check: " + request["check"]
    if request.get("decision_file"):
        soul += "\nDecision file: " + request["decision_file"]
    from team.worker_guard import install_guard

    observed = install_guard(request, names)
    session = request["session_id"]
    agent = AIAgent(
        model=request["model"],
        provider="team-subscription",
        requested_provider="team-subscription",
        api_mode="chat_completions",
        base_url="http://127.0.0.1:4000/v1",
        api_key=key,
        enabled_toolsets=["team-worker"],
        max_iterations=request["max_turns"],
        run_budget_seconds=request["timeout_s"],
        session_id=session,
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
        skip_background_review=True,
        ephemeral_system_prompt=soul,
        platform="cli",
        cwd=str(Path.cwd()),
    )
    try:
        result = agent.run_conversation(request["brief"])
        # Same stop/check loop as the previous worker, with history preserved.
        for _ in range(3):
            if (
                readonly
                or result.get("failed")
                or (
                    request.get("decision_file")
                    and Path(request["decision_file"]).exists()
                )
            ):
                break
            env = {
                "TEAM_CHECK": request.get("check") or "",
                "TEAM_DECISION_FILE": request.get("decision_file") or "",
            }
            reason = claude_roles.stop_gate(
                {"cwd": str(Path.cwd()), "session_id": session}, env
            )
            if not reason:
                break
            result = agent.run_conversation(
                reason, conversation_history=result["messages"]
            )
        return {
            "is_error": bool(result.get("failed") or result.get("interrupted")),
            "result": result.get("final_response"),
            "session_id": session,
            "num_turns": result.get("api_calls"),
            "usage": {
                "input_tokens": result.get("prompt_tokens"),
                "output_tokens": result.get("completion_tokens"),
            },
            "response_model": observed.get("response_model"),
            "api_error_status": observed.get("api_error_status"),
            "tools": observed.get("tools"),
        }
    finally:
        agent.close()


def main():
    request = json.loads(sys.argv[1])
    from hermes_constants import get_default_hermes_root

    # Under Team's profiles/, not the system temp dir: Hermes finds its committed
    # dependency environment from the home's data root, and a bare temp home has none.
    profiles_dir = get_default_hermes_root() / "profiles"
    profiles_dir.mkdir(parents=True, exist_ok=True)
    try:
        with (
            tempfile.TemporaryDirectory(prefix="team-worker-", dir=profiles_dir) as temp,
            contextlib.redirect_stdout(sys.stderr),
        ):
            result = execute(request, Path(temp))
    except Exception as exc:
        logging.getLogger(__name__).exception("Isolated Team worker failed")
        result = {
            "is_error": True,
            "result": str(exc),
            "api_error_status": getattr(exc, "status_code", None),
        }
    print(json.dumps(result))


if __name__ == "__main__":
    main()
