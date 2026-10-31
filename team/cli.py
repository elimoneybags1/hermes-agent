"""``team``: the command line for Team.

team attach <path> [--name N] [--check CMD]   register a repo as a target
team targets                                   list targets
team roles list|sync [ROLE]                    the roster; sync profiles
team goal <target> "<goal>" [--no-lead] [--role R] [--check CMD]
team run [--once] [--interval S]               the loop
team boss                                      talk to the boss
team app [--port N]                            the boss chat and live chart, in a browser
team plans [show <id>|approve <id>|dismiss <id>]  the boss's proposals
team ledger [--target T] [--all] [--json]      jobs and goals
team decide list|answer <id> "<text>"|dismiss <id>
team gate audit                                every profile against the lock
team settings get|set <key> <value>
team hook stop|pretool                         Claude Code hooks (stdin JSON)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from team import (
    boss,
    catalog,
    claude_roles,
    grade,
    kanban,
    ledger,
    profiles,
    roles,
    run,
    settings,
    targets,
)

ROOT = Path(__file__).resolve().parent.parent


def _conn():
    conn = ledger.connect()
    ledger.init(conn)
    return conn


def _print(obj) -> None:
    print(json.dumps(obj, indent=2, default=str))


# --- commands -----------------------------------------------------------------


def cmd_attach(args) -> int:
    target = targets.attach(args.path, name=args.name, check=args.check)
    kanban.ensure_board(target["name"], f"Team target {target['name']}")
    print(f"attached {target['name']} at {target['path']} (board {target['board']})")
    return 0


def cmd_targets(args) -> int:
    rows = targets.list_targets()
    if args.json:
        _print(rows)
        return 0
    for t in rows:
        flag = "" if t.get("enabled", True) else " (disabled)"
        print(
            f"{t['name']:<16} {t['path']}  check: {t.get('check') or '(default)'}{flag}"
        )
    return 0


def cmd_roles(args) -> int:
    if args.roles_cmd == "list":
        cat = _catalog_or_none()
        for name in roles.names(ROOT):
            role = roles.load(ROOT, name, cat)
            cands = ", ".join(role["models"]["candidates"]) or "-"
            prof = "synced" if profiles.profile_dir(name).exists() else "no profile"
            print(
                f"{name:<12} {role['status']:<8} {role['runs_on']['harness']:<20} {cands}  [{prof}]"
            )
        return 0
    cat = _catalog_or_none()
    names = [args.role] if args.role else roles.names(ROOT)
    for name in names:
        changes = profiles.sync(ROOT, name, cat)
        print(f"{name}: " + (", ".join(changes) if changes else "up to date"))
    return 0


def _catalog_or_none() -> dict | None:
    try:
        return catalog.load()
    except catalog.GateError as e:
        print(f"warning: {e}", file=sys.stderr)
        return None


def cmd_goal(args) -> int:
    conn = _conn()
    if settings.load(ROOT)["lead.enabled"] and not args.no_lead:
        for g in run.plan_goal(ROOT, conn, args.target, args.goal, check=args.check):
            state = f"task {g['current_task']}" if g["current_task"] else "waits"
            print(f"goal {g['id']}: {state} on {g['branch']} ({g['role']}): {g['goal'].splitlines()[0][:60]}")
        return 0
    goal = run.start_goal(
        ROOT, conn, args.target, args.goal, role=args.role, check=args.check
    )
    print(
        f"goal {goal['id']}: task {goal['current_task']} on {goal['branch']} ({goal['role']})"
    )
    return 0


def cmd_run(args) -> int:
    conn = _conn()
    try:
        run.run_loop(ROOT, conn, once=args.once, interval=args.interval)
    except KeyboardInterrupt:
        print("team run: stopped", file=sys.stderr)
    return 0


def cmd_ledger(args) -> int:
    conn = _conn()
    jobs = ledger.list_jobs(conn, target=args.target, limit=None if args.all else 30)
    goals = ledger.list_goals(conn, target=args.target, all_goals=args.all)
    if args.json:
        _print({"jobs": jobs, "goals": goals})
        return 0
    passes, total = ledger.pass_rate(conn, args.target)
    print(f"pass {passes}/{total}")
    rates = ledger.order_rates(conn, args.target)
    print(
        "implement: "
        + " · ".join(f"{name} {p}/{n}" for name, (p, n) in rates.items())
    )
    for g in goals:
        state = (
            g["outcome"] or f"attempt {g['attempt']} {g['current_task'] or ''}".rstrip()
        )
        print(
            f"goal {g['id']:<4} {g['target']:<10} {g['role']:<10} {state:<28} {g['goal'].splitlines()[0][:50]}"
        )
    for j in jobs:
        mc = (j.get("model_choice") or {}).get("model", "")
        print(
            f"job {j['id']:<5} {j['target']:<10} {j['role']:<10} #{j['attempt']} {j['outcome'] or 'open':<8} {mc:<24} {j['task_ref'] or ''}"
        )
    return 0


def cmd_decide(args) -> int:
    conn = _conn()
    if args.decide_cmd == "list":
        for d in ledger.list_decisions(conn, args.target):
            opts = "  ".join(f"{i + 1}) {o}" for i, o in enumerate(d["options"]))
            print(
                f"[{d['id']}] {d['target']}: {d['question']}\n    {d['context'][:300]}\n    {opts}  (recommended: {d['recommendation']})"
            )
        return 0
    if args.decide_cmd == "answer":
        ledger.answer_decision(conn, args.id, args.text)
        print(f"decision {args.id} answered")
        return 0
    ledger.dismiss_decision(conn, args.id)
    print(f"decision {args.id} dismissed")
    return 0


def cmd_boss(args) -> int:
    """The boss's conversation, started outside any repo so no AGENTS.md loads into it."""
    import subprocess

    home = ledger.state_dir() / "boss"
    home.mkdir(parents=True, exist_ok=True)
    return subprocess.call([str(kanban.wrapper()), "-p", "boss", *args.rest], cwd=home)


def cmd_app(args) -> int:
    """Team's own app: the boss chat beside the live chart, and the Team, LiteLLM, Portkey editors."""
    from team.app import server

    server.serve(ROOT, args.port)
    return 0


def cmd_plans(args) -> int:
    conn = _conn()
    if args.plans_cmd in (None, "list"):
        for p in boss.proposals()[:20]:
            print(f"[{p['id']}] {p['status']:<9} {p['headline']}")
        return 0
    p = next((x for x in boss.proposals() if x["id"] == args.id), None)
    if p is None:
        print(f"no proposal {args.id}", file=sys.stderr)
        return 1
    if args.plans_cmd == "show":
        c = boss.card(p)
        print(f"{c['headline']}\n\n{c['summary']}\n")
        for g in c["goals"]:
            after = f" (after {', '.join(map(str, g['after']))})" if g["after"] else ""
            print(f"{g['n']}. {g['goal']}{after}\n   {g['plan']}")
            for part in g["parts"]:
                print(
                    f"   - {part['title']}: {part['role']} on {part['model']}, "
                    f"tools {', '.join(part['toolsets'])}, owns {', '.join(part['owned_paths'])}, "
                    f"{part['steps']} steps, reads {', '.join(part['context']) or 'nothing'}"
                )
        return 0
    if p["status"] != "waiting":
        print(f"proposal {p['id']} is {p['status']}", file=sys.stderr)
        return 1
    try:
        boss.settle(conn, p["decision_id"], "approve" if args.plans_cmd == "approve" else None)
    except boss.BossError as e:
        print(str(e), file=sys.stderr)
        return 1
    print(f"proposal {p['id']} " + ("approved; `team run` releases it" if args.plans_cmd == "approve" else "dismissed"))
    return 0


def cmd_gate(args) -> int:
    findings = profiles.audit_profiles(ROOT)
    if not findings:
        print("gate audit: clean")
        return 0
    for name, found in findings.items():
        for f in found:
            print(f"{name}: {f}")
    return 1


def cmd_settings(args) -> int:
    if args.settings_cmd == "get":
        cfg = settings.load(ROOT)
        for key, value in cfg.items():
            if args.key in (None, key):
                print(f"{key} = {value!r}")
        return 0
    value: object = args.value
    kind = settings.FIELDS[args.key][0] if args.key in settings.FIELDS else str
    if kind is bool:
        value = args.value.lower() in ("1", "true", "yes", "on")
    elif kind is int:
        value = int(args.value)
    sha = settings.set_value(ROOT, args.key, value)
    print(f"{args.key} = {value!r} ({sha[:10]})")
    return 0


def cmd_hook(args) -> int:
    """Claude Code hook: read the payload on stdin; exit 2 with the reason to refuse."""
    import os

    try:
        payload = json.load(sys.stdin)
    except ValueError:
        payload = {}
    env = dict(os.environ)
    if args.hook_cmd == "stop":
        reason = claude_roles.stop_gate(
            payload, env, check_timeout_s=settings.load(ROOT)["grading.check_timeout_s"]
        )
    else:
        reason = claude_roles.edit_gate(payload, env)
    if reason:
        print(reason, file=sys.stderr)
        return 2
    return 0


# --- parser -------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="team", description="An agent team you point at a repo."
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("attach", help="register a git repo as a target")
    a.add_argument("path")
    a.add_argument("--name")
    a.add_argument("--check", help='the command that decides "done" for this target')
    a.set_defaults(func=cmd_attach)

    t = sub.add_parser("targets", help="list targets")
    t.add_argument("--json", action="store_true")
    t.set_defaults(func=cmd_targets)

    r = sub.add_parser("roles", help="the roster and its Hermes profiles")
    rs = r.add_subparsers(dest="roles_cmd", required=True)
    rs.add_parser("list")
    sync = rs.add_parser("sync", help="make each role's profile match its files")
    sync.add_argument("role", nargs="?")
    r.set_defaults(func=cmd_roles)

    g = sub.add_parser("goal", help="hand Team a goal on a target")
    g.add_argument("target")
    g.add_argument("goal")
    g.add_argument("--role", default="implement", help="with --no-lead")
    g.add_argument("--check")
    g.add_argument(
        "--no-lead",
        action="store_true",
        help="skip the lead: one task with a plain brief (the base rate to compare against)",
    )
    g.set_defaults(func=cmd_goal)

    rn = sub.add_parser("run", help="dispatch, grade, land, escalate: the loop")
    rn.add_argument("--once", action="store_true")
    rn.add_argument("--interval", type=int)
    rn.set_defaults(func=cmd_run)

    lg = sub.add_parser("ledger", help="jobs and goals")
    lg.add_argument("--target")
    lg.add_argument("--all", action="store_true")
    lg.add_argument("--json", action="store_true")
    lg.set_defaults(func=cmd_ledger)

    d = sub.add_parser("decide", help="Eli's inbox")
    ds = d.add_subparsers(dest="decide_cmd", required=True)
    dl = ds.add_parser("list")
    dl.add_argument("--target")
    da = ds.add_parser("answer")
    da.add_argument("id", type=int)
    da.add_argument("text")
    dd = ds.add_parser("dismiss")
    dd.add_argument("id", type=int)
    d.set_defaults(func=cmd_decide)

    b = sub.add_parser("boss", help="talk to the boss (extra args go to hermes)")
    b.add_argument("rest", nargs=argparse.REMAINDER)
    b.set_defaults(func=cmd_boss)

    ap = sub.add_parser("app", help="the boss chat and live chart in a browser (127.0.0.1)")
    ap.add_argument("--port", type=int, default=8792)
    ap.set_defaults(func=cmd_app)

    pl = sub.add_parser("plans", help="the boss's proposals: one line each")
    pls = pl.add_subparsers(dest="plans_cmd")
    pls.add_parser("list")
    for verb in ("show", "approve", "dismiss"):
        pls.add_parser(verb).add_argument("id", type=int)
    pl.set_defaults(func=cmd_plans)

    ga = sub.add_parser("gate", help="the free-only lock")
    gas = ga.add_subparsers(dest="gate_cmd", required=True)
    gas.add_parser("audit")
    ga.set_defaults(func=cmd_gate)

    st = sub.add_parser("settings", help="orchestration settings")
    sts = st.add_subparsers(dest="settings_cmd", required=True)
    sg = sts.add_parser("get")
    sg.add_argument("key", nargs="?")
    ss = sts.add_parser("set")
    ss.add_argument("key")
    ss.add_argument("value")
    st.set_defaults(func=cmd_settings)

    h = sub.add_parser("hook", help="Claude Code hooks for build mode")
    hs = h.add_subparsers(dest="hook_cmd", required=True)
    hs.add_parser("stop")
    hs.add_parser("pretool")
    h.set_defaults(func=cmd_hook)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except (
        targets.TargetError,
        roles.RoleError,
        catalog.GateError,
        settings.SettingsError,
        profiles.ProfileError,
        run.RunError,
        grade.GradeError,
        kanban.KanbanError,
        LookupError,
    ) as e:
        print(f"team: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
