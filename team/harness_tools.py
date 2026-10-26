"""Team-owned tools. File boundaries are enforced; terminal is trusted local code."""
from __future__ import annotations

import json
import shlex
import subprocess
import sys
from fnmatch import fnmatch
from pathlib import Path

from team.claude_transport import clean_env
from team.free import check_command
from team import hermes_boss

DESCRIPTIONS = {
    "read": 'Read UTF-8 file: {"path": "relative/path"}.',
    "list": 'List directory entries: {"path": "."}.',
    "write": 'Write UTF-8 file: {"path": "relative/path", "content": "..."}.',
    "exec": 'Run argv without shell: {"argv": ["git", "status"]}. Working directory is fixed.',
    "team": 'Call orchestrator: {"argv": ["goals"]}. Allowed: targets, roles, goals, decide list, ledger list, dash --json. Use kanban_create to dispatch.',
}


def tools_for(role: str, configured: list[str]) -> list[str]:
    names = []
    if role == "boss":
        if "read-only" in configured:
            names += ["read", "list"]
        if "orchestrator" in configured:
            names += ["team"]
        if "kanban" in configured:
            names += list(hermes_boss.schemas())
        return names
    if "file" in configured or "read-only" in configured:
        names += ["read", "list"]
    if "file" in configured:
        names += ["write"]
    if "terminal" in configured:
        names += ["exec"]
    return names


class ToolExecutor:
    def __init__(self, cwd: Path, allowed: list[str], *, allowed_files=None,
                 runner=subprocess.run, root=None):
        self.cwd = cwd.resolve()
        self.allowed = allowed
        self.allowed_files = allowed_files
        self.runner = runner
        self.root = root or Path(__file__).resolve().parent.parent

    def path(self, value):
        if not isinstance(value, str):
            raise ValueError("path must be a string")
        path = (self.cwd / value).resolve()
        if not path.is_relative_to(self.cwd):
            raise ValueError("path is outside the worktree")
        rel = path.relative_to(self.cwd)
        if any(p in {".git", ".env", ".ssh", ".aws"} or p.startswith(".env.") for p in rel.parts):
            raise ValueError("repository metadata and credential files are not file-tool inputs")
        return path

    def execute(self, name, args, timeout_s):
        if name not in self.allowed:
            raise ValueError(f"tool not allowed: {name}")
        if not isinstance(args, dict):
            raise ValueError("tool arguments must be an object")
        if name in hermes_boss.schemas():
            return hermes_boss.execute(self.root, name, args, runner=self.runner, timeout_s=timeout_s)
        if name in {"read", "list", "write"}:
            path = self.path(args.get("path"))
            if name == "read":
                with path.open() as f:
                    return f.read(24000)
            if name == "list":
                return "\n".join(sorted(p.name + ("/" if p.is_dir() else "") for p in path.iterdir()))[:24000]
            content = args.get("content")
            if not isinstance(content, str):
                raise ValueError("content must be a string")
            rel = path.relative_to(self.cwd).as_posix()
            if self.allowed_files is not None and not any(fnmatch(rel, p) for p in self.allowed_files):
                raise ValueError("write outside task's allowed files")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
            return f"wrote {rel}"
        argv = args.get("argv")
        if not isinstance(argv, list) or not argv or not all(isinstance(s, str) and s and '\0' not in s for s in argv):
            raise ValueError("argv must be a nonempty array of strings")
        cwd = self.cwd
        if name == "exec":
            reason = check_command(shlex.join(argv))
            if reason:
                raise ValueError(reason)
            if Path(argv[0]).name in {"claude", "codex", "crew"}:
                raise ValueError("agent launches belong to Team's orchestrator")
        if name == "team":
            if not (argv in (["targets"], ["roles"], ["dash", "--json"])
                    or argv[:1] == ["goals"]
                    or argv[:2] in (["decide", "list"], ["ledger", "list"])):
                raise ValueError("boss may only inspect Team or dispatch goals")
            argv = [sys.executable, "-m", "team", *argv]
            cwd = self.root
        proc = self.runner(argv, cwd=cwd, env=clean_env(), stdin=subprocess.DEVNULL,
                           capture_output=True, text=True, timeout=timeout_s)
        return json.dumps({"exit_code": proc.returncode,
                           "stdout": proc.stdout[-16000:], "stderr": proc.stderr[-8000:]})
