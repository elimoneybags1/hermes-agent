"""Command line: `team ledger init|list`, `team attach`, `team targets`."""

from __future__ import annotations

import argparse
import sys

from team import ledger, targets

LIST_COLUMNS = ("id", "target", "role", "attempt", "started", "ended", "outcome", "grader")
TARGET_COLUMNS = ("name", "path", "commit", "attached_at")


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


def main(argv: list[str] | None = None) -> int:
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

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
