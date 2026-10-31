"""Tool policy for an isolated Team worker (installed before its first turn)."""

from pathlib import Path
from team import claude_roles


def guard(request, names, tool_name, args):
    if tool_name not in names:
        return {
            "action": "block",
            "message": "This tool is outside this worker's scope.",
        }
    if tool_name not in ("write_file", "patch"):
        return None
    if tool_name == "patch" and args.get("mode") == "patch":
        return {
            "action": "block",
            "message": "Use single-file replace patches so file ownership can be checked.",
        }
    target = args.get("path") or args.get("file_path")
    if not target:
        return {"action": "block", "message": "An explicit file path is required."}
    decision = request.get("decision_file")
    if decision and target and Path(target).resolve() == Path(decision).resolve():
        return None
    reason = claude_roles.edit_gate(
        {
            "tool_name": "Write",
            "tool_input": {"file_path": target},
            "cwd": str(Path.cwd()),
        },
        {"TEAM_ALLOWED_FILES": ":".join(request.get("allowed_files") or [])},
    )
    return {"action": "block", "message": reason} if reason else None


def install_guard(request, names):
    from hermes_cli.plugins import (
        PluginContext,
        PluginManifest,
        discover_plugins,
        get_plugin_manager,
    )

    discover_plugins()
    ctx = PluginContext(PluginManifest(name="team-worker-guard"), get_plugin_manager())
    ctx.register_hook(
        "pre_tool_call",
        lambda tool_name, args, **kw: guard(request, names, tool_name, args),
    )

    observed = {}

    def response(response_model=None, **kw):
        observed["response_model"] = response_model
        observed["api_error_status"] = None

    def error(status_code=None, **kw):
        observed["api_error_status"] = status_code

    def sent(request=None, **kw):
        observed["tools"] = [
            t.get("function", {}).get("name")
            for t in (request or {}).get("body", {}).get("tools", [])
        ]

    ctx.register_hook("post_api_request", response)
    ctx.register_hook("api_request_error", error)
    ctx.register_hook("pre_api_request", sent)
    return observed
