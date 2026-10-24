"""Command line: `team ledger init|list`, `team attach`, `team targets`, `team roles`, `team dash`, `team ui`, `team board`, `team free-check`,
`team goal`, `team run`, `team decide`."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

from team import ledger, targets, roles, dash
from team.free import check_command
from team import decisions

LIST_COLUMNS = ("id", "target", "role", "attempt", "started", "ended", "outcome", "grader")
TARGET_COLUMNS = ("name", "path", "commit", "attached_at")
REPO = Path(__file__).resolve().parent.parent

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


def _ui(args: argparse.Namespace) -> int:
    from team import ui

    return ui.main(port=args.port, open_browser=not args.no_open)


def board_argv(port: int) -> list[str]:
    """The Hermes web dashboard on Team's own kanban home, as the free-only worker profile."""
    return ["hermes", "-p", "team-worker", "dashboard", "--isolated", "--port", str(port), "--no-open"]


def _board(args: argparse.Namespace) -> int:
    hermes = shutil.which("hermes")
    if hermes is None:
        print("hermes not found on PATH", file=sys.stderr)
        return 1
    os.environ["HERMES_KANBAN_HOME"] = str(ledger.state_dir() / "kanban")
    url = f"http://127.0.0.1:{args.port}/kanban?profile=team-worker"
    print(f"Team board: {url}  (keep the profile on team-worker: it holds only free models)")
    os.execvp(hermes, board_argv(args.port))


def boss_argv(repo: Path, extra: list[str]) -> list[str]:
    """Build the argv for the boss agent."""
    plugin_dir = repo / "plugin"
    return ["claude", "--plugin-dir", str(plugin_dir), "--agent", "boss", *extra]


def _free_check(args: argparse.Namespace) -> int:
    """Check a hermes command from a Claude Code PreToolUse hook payload."""
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError:
        # Not a JSON payload we judge - exit 0
        return 0
    
    # Check it's a Bash tool call
    if payload.get("tool_name") != "Bash":
        return 0
    
    tool_input = payload.get("tool_input", {})
    command = tool_input.get("command")
    if not command:
        return 0
    
    reason = check_command(command)
    if reason is None:
        return 0
    
    print(reason, file=sys.stderr)
    return 2


def _decide_list(args: argparse.Namespace) -> int:
    path = ledger.db_path()
    if not path.exists():
        print(f"no ledger at {path}; run `team ledger init`", file=sys.stderr)
        return 1
    conn = ledger.connect(path)
    rows = decisions.list_open(conn, target=args.target)
    conn.close()
    if not rows:
        print("no open decisions")
        return 0
    for r in rows:
        opts = ", ".join(json.loads(r["options"]))
        print(f"[{r['id']}] {r['target']} — {r['question']}")
        print(f"    options: {opts}")
        print(f"    recommendation: {r['recommendation']}")
        if r["goal_id"] is not None:
            print(f"    goal_id: {r['goal_id']}")
        if r["task_id"] is not None:
            print(f"    task_id: {r['task_id']}")
        print(f"    source: {r['source']}")
    return 0


def _decide_answer(args: argparse.Namespace) -> int:
    path = ledger.db_path()
    if not path.exists():
        print(f"no ledger at {path}; run `team ledger init`", file=sys.stderr)
        return 1
    conn = ledger.connect(path)
    try:
        decisions.answer(conn, args.id, args.answer)
    except LookupError as e:
        print(f"decision not found: {e}", file=sys.stderr)
        conn.close()
        return 1
    except ValueError as e:
        print(f"cannot answer: {e}", file=sys.stderr)
        conn.close()
        return 1
    conn.close()
    print(f"answered decision {args.id}")
    return 0


def _decide_dismiss(args: argparse.Namespace) -> int:
    path = ledger.db_path()
    if not path.exists():
        print(f"no ledger at {path}; run `team ledger init`", file=sys.stderr)
        return 1
    conn = ledger.connect(path)
    try:
        decisions.dismiss(conn, args.id)
    except LookupError as e:
        print(f"decision not found: {e}", file=sys.stderr)
        conn.close()
        return 1
    except ValueError as e:
        print(f"cannot dismiss: {e}", file=sys.stderr)
        conn.close()
        return 1
    conn.close()
    print(f"dismissed decision {args.id}")
    return 0


def _goal(args: argparse.Namespace) -> int:
    from team import grade, kanban, run
    from team.agents import AgentError

    conn = ledger.connect(ledger.db_path())
    ledger.init(conn)
    try:
        goal = run.start_goal(REPO, conn, args.target, args.goal,
                              role=args.role, check=args.check)
    except (run.RunError, grade.GradeError, kanban.KanbanError, AgentError) as e:
        print(f"cannot start goal: {e} {getattr(e, 'stderr', '')}".rstrip(), file=sys.stderr)
        return 1
    finally:
        conn.close()
    print(f"goal {goal['id']}: task {goal['current_task']} on {goal['branch']}"
          f" in {goal['worktree']}; check: {goal['check_cmd']}")
    return 0


def _run(args: argparse.Namespace) -> int:
    from team import run

    conn = ledger.connect(ledger.db_path())
    ledger.init(conn)
    try:
        run.run_loop(REPO, conn, once=args.once, interval=args.interval)
    except KeyboardInterrupt:
        return 130
    finally:
        conn.close()
    return 0


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
    ui_parser = commands.add_parser("ui", help="agent dashboard in the browser")
    ui_parser.add_argument("--port", type=int, default=7341)
    ui_parser.add_argument("--no-open", action="store_true", help="do not open the browser")
    ui_parser.set_defaults(func=_ui)
    board_parser = commands.add_parser("board", help="Team's kanban board (Hermes dashboard) in the browser")
    board_parser.add_argument("--port", type=int, default=9129)
    board_parser.set_defaults(func=_board)
    commands.add_parser("free-check", help="check hermes command for free model compliance").set_defaults(func=_free_check)

    dec = commands.add_parser("decide", help="list, answer, or dismiss open decisions")
    dec_sub = dec.add_subparsers(dest="action", required=True)
    dec_list = dec_sub.add_parser("list", help="list open decisions")
    dec_list.add_argument("--target", help="filter by target name")
    dec_list.set_defaults(func=_decide_list)
    dec_ans = dec_sub.add_parser("answer", help="answer a decision")
    dec_ans.add_argument("id", type=int, help="decision id")
    dec_ans.add_argument("answer", help="answer text")
    dec_ans.set_defaults(func=_decide_answer)
    dec_dis = dec_sub.add_parser("dismiss", help="dismiss a decision")
    dec_dis.add_argument("id", type=int, help="decision id")
    dec_dis.set_defaults(func=_decide_dismiss)

    goal_parser = commands.add_parser("goal", help="hand Team a goal on a target (docs/RUN.md)")
    goal_parser.add_argument("target")
    goal_parser.add_argument("goal")
    goal_parser.add_argument("--role", default="implement")
    goal_parser.add_argument("--check", help="set the target's check first")
    goal_parser.set_defaults(func=_goal)

    run_parser = commands.add_parser("run", help="dispatch, grade, land, escalate: the autonomous loop")
    run_parser.add_argument("--once", action="store_true", help="one tick, then exit")
    run_parser.add_argument("--interval", type=int, help="seconds between ticks (default: settings)")
    run_parser.set_defaults(func=_run)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
