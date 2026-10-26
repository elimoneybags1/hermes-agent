"""Hermes-derived boss surface, with Team's grading/target policy at the boundary."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
from pathlib import Path

from team import ledger, roles, targets
from team.claude_transport import clean_env

VENDOR = Path(__file__).resolve().parent / "vendor" / "hermes-kanban"
NATIVE = {"kanban_list", "kanban_show", "kanban_comment", "kanban_attachments"}


def schemas() -> dict:
    specs = json.loads((VENDOR / "schemas.json").read_text())
    for name, spec in specs.items():
        props = spec["parameters"]["properties"]
        props["board"] = {"type": "string", "description": "Required attached Team target name. Uses Team's kanban home only."}
        spec["parameters"]["required"] = list(dict.fromkeys([*spec["parameters"].get("required", []), "board"]))
        spec["parameters"]["additionalProperties"] = False
    create = specs["kanban_create"]
    props = create["parameters"]["properties"]
    create["parameters"]["properties"] = {k: copy.deepcopy(props[k]) for k in ("title", "body", "assignee", "parents", "board")}
    create["parameters"]["properties"]["check"] = {"type": "string", "description": "Optional verified check command; otherwise Team target's configured check."}
    create["parameters"]["properties"]["parents"]["description"] = (
        "Existing Team task IDs on this board. Parents must already have passed and landed in Team; "
        "otherwise creation is refused. Pending DAG dispatch is not yet supported by Team's grader.")
    create["parameters"]["required"] = ["title", "body", "assignee", "board"]
    create["description"] += " Team adapter: creates a graded goal and isolated worktree. Assignee must be a discovered active Team role profile."
    specs["team_roster"] = {"name": "team_roster", "description": "Discover actual Hermes profiles matched to Team roles, instructions, tools, skills, and attached targets before routing work.",
                            "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}
    return specs


def installed_source() -> Path:
    return Path(os.environ.get("TEAM_HERMES_SOURCE", str(Path(__file__).resolve().parent.parent / "vendor/hermes-agent"))).expanduser().resolve()


def verify_source(source: Path) -> None:
    provenance = json.loads((VENDOR / "provenance.json").read_text())
    for relative in ("tools/kanban_tools.py", "hermes_cli/kanban_db.py"):
        path = source / relative
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != provenance["files"][relative]:
            raise ValueError(f"Hermes source drift at {relative}; review upstream changes before refreshing Team's pinned integration")


def bridge_env() -> dict:
    from team.hermes_runtime import worker_environment, worker_profile_root
    env = worker_environment(clean_env())
    for key in list(env):
        if key.startswith("HERMES_") or key in {"PYTHONPATH", "PYTHONHOME"}:
            env.pop(key)
    env.update(HERMES_HOME=str(worker_profile_root() / "profiles/team-worker"),
               HERMES_PROFILE="team-boss", HERMES_KANBAN_HOME=str(ledger.state_dir() / "kanban"))
    return env


def validate(name: str, args: dict) -> None:
    schema = schemas()[name]["parameters"]
    if not isinstance(args, dict) or set(args) - set(schema["properties"]):
        raise ValueError("unknown tool arguments")
    if set(schema.get("required", [])) - set(args):
        raise ValueError("missing required tool arguments")
    for key, value in args.items():
        prop = schema["properties"][key]
        expected = {"string": str, "integer": int, "boolean": bool, "array": list}[prop["type"]]
        if type(value) is not expected:
            raise ValueError(f"invalid type for {key}")
        if "enum" in prop and value not in prop["enum"]:
            raise ValueError(f"invalid value for {key}")
        if expected is list and not all(isinstance(v, str) and v for v in value):
            raise ValueError(f"invalid items for {key}")
    if name != "team_roster":
        board = args["board"]
        if not targets.NAME_RE.fullmatch(board) or board in {".", ".."} or targets.get(board) is None:
            raise ValueError("board must be an attached Team target")


def roster(root: Path, runner, timeout_s: float) -> dict:
    proc = runner(["hermes", "profile", "list"], env=bridge_env(), capture_output=True,
                  text=True, timeout=timeout_s)
    if proc.returncode:
        raise ValueError(f"Cannot discover Hermes profiles: {proc.stderr[-1000:]}")
    profiles = {line.strip().lstrip("◆*").strip().split()[0]
                for line in proc.stdout.splitlines() if line.strip().lstrip("◆*").strip()}
    result = []
    for role in roles.load_roles(root):
        profile = "team-" + role["name"]
        result.append({**role, "profile": profile, "available": profile in profiles,
                       "skills": sorted(p.parent.name for p in (root / "roles" / role["name"]).glob("skills/*/SKILL.md"))})
    return {"roles": result, "targets": targets.list_targets()}


def execute(root: Path, name: str, args: dict, *, runner=subprocess.run, timeout_s=120) -> str:
    validate(name, args)
    if name == "team_roster":
        return json.dumps(roster(root, runner, timeout_s))
    source = installed_source()
    verify_source(source)
    if name == "kanban_create":
        from team.run import start_goal
        available = roster(root, runner, timeout_s)["roles"]
        role = next((r for r in available if r["profile"] == args["assignee"] and r["available"] and r.get("status") == "active" and r["name"] != "boss"), None)
        if role is None:
            raise ValueError("assignee must be an available active Team worker from team_roster")
        conn = ledger.connect()
        try:
            ledger.init(conn)
            for parent in args.get("parents", []):
                row = conn.execute("SELECT outcome FROM goals WHERE target=? AND current_task=?", (args["board"], parent)).fetchone()
                if row is None or row["outcome"] != "pass":
                    raise ValueError("parent has not passed and landed in Team; wait before dispatching dependent work")
            result = start_goal(root, conn, args["board"], args["title"] + "\n\n" + args["body"],
                                role=role["name"], check=args.get("check"), runner=runner,
                                parents=tuple(args.get("parents", [])))
            return json.dumps({"task_id": result["current_task"], "goal_id": result["id"], "board": args["board"]})
        finally:
            conn.close()
    if name not in NATIVE:
        raise ValueError("unexposed Hermes tool")
    from team.hermes_runtime import python_executable
    proc = runner([str(python_executable()), str(Path(__file__).with_name("hermes_bridge.py")), str(source)],
                  cwd=source, env=bridge_env(), input=json.dumps({"name": name, "arguments": args}),
                  capture_output=True, text=True, timeout=timeout_s)
    if proc.returncode:
        raise ValueError(f"Hermes tool failed: {proc.stderr[-2000:]}")
    data = json.loads(proc.stdout)
    output = json.dumps(data)
    if len(output) > 100000:
        return json.dumps({"error": "Tool response too large; request a smaller board listing or inspect a narrower task."})
    return output
