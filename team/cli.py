"""Command line: `team ledger init|list`, `team attach`, `team targets`, `team roles`, `team dash`."""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

from team import ledger, targets, roles, dash

LIST_COLUMNS = ("id", "target", "role", "attempt", "started", "ended", "outcome", "grader")
TARGET_COLUMNS = ("name", "path", "commit", "attached_at")
ROLE_COLUMNS = ("name", "status", "runs_on.harness", "runs_on.model", "title")


def _init(args: argparse.Namespace) -> int:
    path = ledger.db_path()
    conn = ledger.connect(path)
    ledger.init(conn)
    conn.close()
    print(f"ledger ready at {path}")
    return 0


def _list(args: argparse.Namespace) -> int:
    path = ledger.db_path()
    if not path.exists():
        print(f"no ledger at {path}; run `team ledger init`", file=sys.stderr)
        return 1
    conn = ledger.connect(path)
    jobs = ledger.list_jobs(conn, target=args.target, role=args.role, limit=args.limit)
    conn.close()
    print("\t".join(LIST_COLUMNS))
    for job in jobs:
        print("\t".join("" if job[c] is None else str(job[c]) for c in LIST_COLUMNS))
    return 0


def _attach(args: argparse.Namespace) -> int:
    try:
        target = targets.attach(args.repo, name=args.name)
    except targets.TargetError as e:
        print(f"cannot attach: {e}", file=sys.stderr)
        return 1
    print(f"attached {target['name']} at {target['path']} ({target['commit'][:12]})")
    return 0


def _targets(args: argparse.Namespace) -> int:
    print("\t".join(TARGET_COLUMNS))
    for target in targets.list_targets():
        print("\t".join(str(target[c]) for c in TARGET_COLUMNS))
    return 0


def _roles(args: argparse.Namespace) -> int:
    role_list = roles.load_roles()
    print("\t".join(ROLE_COLUMNS))
    for role in role_list:
        print(
            "\t".join(
                [
                    role.get("name", ""),
                    role.get("status", ""),
                    role.get("runs_on", {}).get("harness", ""),
                    role.get("runs_on", {}).get("model", ""),
                    role.get("title", ""),
                ]
            )
        )
    return 0


def _dash(args: argparse.Namespace) -> int:
    return dash.main(argv=["--json"] if args.json else [])


def boss_argv(repo: Path, extra: list[str]) -> list[str]:
    """Build the argv for the boss agent."""
    plugin_dir = repo / "plugin"
    return ["claude", "--plugin-dir", str(plugin_dir), "--agent", "boss", *extra]


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]

    # Start the boss ONLY when argv is empty, or when argv[0] == "--"
    if not argv or argv[0] == "--":
        # No subcommand: start the boss agent
        # Find the `--` separator to split extra args
        try:
            separator_idx = argv.index("--")
            extra = argv[separator_idx + 1 :]
        except ValueError:
            extra = argv if argv else []

        repo = Path(__file__).resolve().parent.parent
        os.chdir(repo)
        claude_path = shutil.which("claude")
        if claude_path is None:
            print("claude not found on PATH", file=sys.stderr)
            return 1
        boss_argv_list = boss_argv(repo, extra)
        os.execvp(claude_path, boss_argv_list)

    # Has a subcommand: parse normally
    parser = argparse.ArgumentParser(prog="team")
    commands = parser.add_subparsers(dest="command", required=True)

    led = commands.add_parser("ledger", help="the outcomes ledger")
    led_commands = led.add_subparsers(dest="action", required=True)
    led_commands.add_parser("init", help="create the ledger tables").set_defaults(func=_init)
    lst = led_commands.add_parser("list", help="list jobs, newest first")
    lst.add_argument("--target")
    lst.add_argument("--role")
    lst.add_argument("--limit", type=int)
    lst.set_defaults(func=_list)

    att = commands.add_parser("attach", help="register a git repo as a target")
    att.add_argument("repo", help="path to the target repo")
    att.add_argument("--name", help="target name (default: repo directory, lowercased)")
    att.set_defaults(func=_attach)
    commands.add_parser("targets", help="list attached targets").set_defaults(func=_targets)
    commands.add_parser("roles", help="list roles").set_defaults(func=_roles)
    dash_parser = commands.add_parser("dash", help="dashboard snapshot")
    dash_parser.add_argument("--json", action="store_true", help="print JSON snapshot")
    dash_parser.set_defaults(func=_dash)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
