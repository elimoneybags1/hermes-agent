"""Team deck: the control room Textual app."""

from __future__ import annotations

import asyncio
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    ListItem,
    ListView,
    Static,
    TabbedContent,
    TabPane,
)
from textual.worker import Worker, get_current_worker

from team import ledger
from team.dash import snapshot
from team.roles import load_roles


REFRESH_INTERVAL = 2.0


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class TeamDeck(App):
    """Team control room: boss on the left, deck on the right."""

    CSS = """
    Screen {
        layout: vertical;
    }
    #header {
        dock: top;
        height: 3;
        background: $surface;
        border-bottom: solid $primary;
    }
    #header-content {
        layout: horizontal;
        height: 100%;
        padding: 0 1;
    }
    #header-left {
        width: 1fr;
    }
    #header-right {
        width: auto;
        text-align: right;
        padding-right: 1;
    }
    #tabs {
        dock: top;
        height: 1;
    }
    #main {
        layout: vertical;
    }
    TabbedContent {
        height: 1fr;
    }
    TabPane {
        layout: vertical;
    }
    #pipeline-strip {
        dock: top;
        height: 3;
        background: $surface;
        border-bottom: solid $primary;
    }
    #pipeline-table {
        height: 1fr;
    }
    #inbox-list {
        height: 1fr;
    }
    #agents-table {
        height: 1fr;
    }
    #log-table {
        height: 1fr;
    }
    DataTable {
        height: 1fr;
    }
    .status-running {
        color: $success;
    }
    .status-stopped {
        color: $error;
    }
    .decision-question {
        text-style: bold;
    }
    .decision-recommendation {
        color: $warning;
    }
    #footer-line {
        dock: bottom;
        height: 1;
        background: $surface;
        border-top: solid $primary;
    }
    """

    BINDINGS = [
        Binding("1", "switch_tab('pipeline')", "Pipeline", show=True),
        Binding("2", "switch_tab('inbox')", "Inbox", show=True),
        Binding("3", "switch_tab('agents')", "Agents", show=True),
        Binding("4", "switch_tab('log')", "Log", show=True),
        Binding("g", "new_goal", "New Goal", show=True),
        Binding("q", "quit", "Quit", show=True),
    ]

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._snapshot_data: dict | None = None
        self._ledger_conn = None
        self._refresh_thread: threading.Thread | None = None
        self._stop_refresh = threading.Event()
        self._run_checker: Worker | None = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Container(id="header"):
            with Horizontal(id="header-content"):
                with Vertical(id="header-left"):
                    yield Static("", id="header-title")
                    yield Static("", id="header-stats")
                with Vertical(id="header-right"):
                    yield Static("", id="header-decisions")
        yield TabbedContent(
            TabPane(
                "Pipeline",
                Static("", id="pipeline-strip"),
                DataTable(id="pipeline-table"),
                id="pipeline-tab",
            ),
            TabPane(
                "Inbox",
                ListView(id="inbox-list"),
                id="inbox-tab",
            ),
            TabPane(
                "Agents",
                DataTable(id="agents-table"),
                id="agents-tab",
            ),
            TabPane(
                "Log",
                DataTable(id="log-table"),
                id="log-tab",
            ),
            id="tabs",
        )
        yield Footer()

    def on_mount(self) -> None:
        self._load_roles()
        self._start_refresh()
        self._start_run_checker()
        self._refresh_ui()

    def _load_roles(self) -> None:
        repo_root = Path(__file__).resolve().parent.parent
        self._roles = load_roles(repo_root)

    def _start_refresh(self) -> None:
        self._stop_refresh.clear()
        self._refresh_thread = threading.Thread(target=self._refresh_loop, daemon=True)
        self._refresh_thread.start()

    def _refresh_loop(self) -> None:
        while not self._stop_refresh.is_set():
            time.sleep(REFRESH_INTERVAL)
            if not self._stop_refresh.is_set():
                self.call_from_thread(self._refresh_ui)

    def _start_run_checker(self) -> None:
        """Start a worker to check if `team run` is alive."""
        self._run_checker = self.run_worker(self._check_team_run_loop, exclusive=True, group="run_checker")

    async def _check_team_run_loop(self) -> None:
        while True:
            await self._check_team_run()
            await asyncio.sleep(REFRESH_INTERVAL)

    async def _check_team_run(self) -> None:
        """Check if `team run` process is alive."""
        import psutil
        is_running = False
        for proc in psutil.process_iter(["cmdline"]):
            try:
                cmdline = proc.info.get("cmdline")
                if cmdline and "team" in cmdline and "run" in cmdline:
                    is_running = True
                    break
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        self.call_from_thread(self._update_run_status, is_running)

    def _update_run_status(self, is_running: bool) -> None:
        status_widget = self.query_one("#header-title", Static)
        status_text = "🟢 team run alive" if is_running else "🔴 team run stopped"
        status_widget.update(status_text)
        status_widget.set_class(is_running, "status-running")
        status_widget.set_class(not is_running, "status-stopped")

    def _get_ledger_conn(self):
        """Get a ledger connection."""
        db_path = ledger.db_path()
        if db_path.exists():
            return ledger.connect(db_path)
        return None

    def _refresh_ui(self) -> None:
        """Refresh the UI with latest data."""
        conn = self._get_ledger_conn()
        try:
            data = snapshot(conn, self._roles)
        finally:
            if conn is not None:
                conn.close()

        self._snapshot_data = data
        self._update_header(data)
        self._update_pipeline_tab(data)
        self._update_inbox_tab(data)
        self._update_agents_tab(data)
        self._update_log_tab(data)

    def _update_header(self, data: dict) -> None:
        """Update the header with summary stats."""
        base = data.get("base", {})
        roles = data.get("roles", [])

        # Count open goals per role stage
        pipeline_counts = self._count_pipeline_goals(roles)

        # Header title with run status
        title_widget = self.query_one("#header-title", Static)

        # Stats line: open goals / landed today, pass rate
        open_goals = sum(pipeline_counts.values())
        total_jobs = base.get("jobs", 0)
        passed_jobs = base.get("passed", 0)
        base_rate = f"{base['pass_rate']:.2%}" if base.get("pass_rate") is not None else "—"
        pass_rate_str = f"{passed_jobs}/{total_jobs} · base {base_rate}"

        stats_widget = self.query_one("#header-stats", Static)
        stats_widget.update(f"Goals: {open_goals} open  |  Pass rate: {pass_rate_str}")

        # Open decisions count
        decisions_widget = self.query_one("#header-decisions", Static)
        decisions_widget.update(f"Decisions: {self._open_decisions_count()} open")

    def _count_pipeline_goals(self, roles: list[dict]) -> dict[str, int]:
        """Count goals at each pipeline stage (simplified for now)."""
        # For now, just count by role
        counts = {}
        for role in roles:
            counts[role["name"]] = len(role.get("working_on", []))
        return counts

    def _open_decisions_count(self) -> int:
        conn = self._get_ledger_conn()
        if conn is None:
            return 0
        try:
            from team.decisions import list_open
            return len(list_open(conn))
        finally:
            conn.close()

    def _update_pipeline_tab(self, data: dict) -> None:
        """Update the Pipeline tab with role strip and goals table."""
        roles = data.get("roles", [])

        # Role strip
        strip_widget = self.query_one("#pipeline-strip", Static)
        if not strip_widget:
            return
        role_names = [r["name"] for r in roles]
        strip_text = " → ".join(role_names)
        strip_widget.update(f"Pipeline: {strip_text}")

        # Goals table
        table = self.query_one("#pipeline-table", DataTable)
        table.clear(columns=True)
        table.add_columns("ID", "Target", "Role", "Attempt", "Status", "Age", "Goal")

        conn = self._get_ledger_conn()
        if conn is not None:
            try:
                goals = conn.execute(
                    "SELECT * FROM goals WHERE finished IS NULL ORDER BY id"
                ).fetchall()
                for goal in goals:
                    created = goal["created"]
                    age = self._format_age(created)
                    goal_text = goal["goal"].split("\n")[0] if goal["goal"] else ""
                    table.add_row(
                        str(goal["id"]),
                        goal["target"],
                        goal["role"],
                        str(goal["attempt"]),
                        "open",
                        age,
                        goal_text[:80],
                    )
            finally:
                conn.close()

    def _update_inbox_tab(self, data: dict) -> None:
        """Update the Inbox tab with open decisions."""
        list_view = self.query_one("#inbox-list", ListView)
        list_view.clear()

        conn = self._get_ledger_conn()
        if conn is not None:
            try:
                from team.decisions import list_open
                decisions = list_open(conn)
                for d in decisions:
                    import json
                    options = json.loads(d["options"])
                    opt_str = ", ".join(f"{i+1}. {opt}" for i, opt in enumerate(options))
                    rec_idx = options.index(d["recommendation"]) + 1 if d["recommendation"] in options else 1
                    opt_str += f"  (rec: {rec_idx})"

                    item_text = (
                        f"[{d['id']}] {d['question']}\n"
                        f"  Context: {d['context']}\n"
                        f"  Options: {opt_str}"
                    )
                    list_view.append(ListItem(Static(item_text, classes="decision-item")))
            finally:
                conn.close()

    def _update_agents_tab(self, data: dict) -> None:
        """Update the Agents tab with role list."""
        table = self.query_one("#agents-table", DataTable)
        table.clear(columns=True)
        table.add_columns("Name", "Title", "Status", "Model", "Pass Rate")

        roles = data.get("roles", [])
        for role in roles:
            stats = role.get("stats", {})
            passed = stats.get("passed", 0)
            total = stats.get("jobs", 0)
            rate = f"{passed}/{total}" if total > 0 else "—"
            model = role.get("runs_on", {}).get("model", "—")
            table.add_row(
                role["name"],
                role.get("title", ""),
                role.get("status", ""),
                model,
                rate,
            )

    def _update_log_tab(self, data: dict) -> None:
        """Update the Log tab with recent activity."""
        table = self.query_one("#log-table", DataTable)
        table.clear(columns=True)
        table.add_columns("Time", "Target", "Event", "Detail")

        recent = data.get("recent", [])
        for job in recent:
            time_str = job.get("started", "")[:19].replace("T", " ")
            outcome = job.get("outcome") or "running"
            detail = job.get("grader_detail") or ""
            if detail:
                detail = detail[:60]
            table.add_row(time_str, job.get("target", ""), outcome, detail)

    def _format_age(self, created_iso: str) -> str:
        """Format age from ISO timestamp."""
        try:
            created = datetime.fromisoformat(created_iso.replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)
            delta = now - created
            if delta.days > 0:
                return f"{delta.days}d"
            hours = delta.seconds // 3600
            if hours > 0:
                return f"{hours}h"
            minutes = (delta.seconds % 3600) // 60
            return f"{minutes}m"
        except Exception:
            return "?"

    # Actions
    def action_switch_tab(self, tab_id: str) -> None:
        """Switch to a tab by id."""
        tabs = self.query_one("#tabs", TabbedContent)
        tabs.active = tab_id

    def action_new_goal(self) -> None:
        """Show new goal form."""
        self.push_screen(NewGoalScreen(self._roles))

    def action_quit(self) -> None:
        self._stop_refresh.set()
        self.exit()

    def on_unmount(self) -> None:
        self._stop_refresh.set()
        if self._run_checker:
            self._run_checker.cancel()


class NewGoalScreen(Screen):
    """Screen for creating a new goal."""

    CSS = """
    NewGoalScreen {
        align: center middle;
    }
    #goal-form {
        width: 80;
        height: auto;
        padding: 2;
        border: solid $primary;
        background: $surface;
    }
    #goal-form Label {
        margin-top: 1;
    }
    #goal-form Input {
        margin-bottom: 1;
    }
    #goal-form #buttons {
        margin-top: 1;
        layout: horizontal;
    }
    #goal-form Button {
        margin-right: 1;
    }
    """

    def __init__(self, roles: list[dict]) -> None:
        super().__init__()
        self._roles = roles

    def compose(self) -> ComposeResult:
        with Container(id="goal-form"):
            yield Label("New Goal")
            yield Input(placeholder="Target (from attached targets)", id="target-input")
            yield Input(placeholder="Goal text", id="goal-input")
            yield Input(placeholder="Role (default: implement)", id="role-input")
            with Horizontal(id="buttons"):
                yield Button("Create", variant="primary", id="create-btn")
                yield Button("Cancel", variant="default", id="cancel-btn")

    @on(Button.Pressed, "#create-btn")
    def create_goal(self) -> None:
        target = self.query_one("#target-input", Input).value
        goal_text = self.query_one("#goal-input", Input).value
        role = self.query_one("#role-input", Input).value or "implement"

        if not target or not goal_text:
            self.notify("Target and goal text are required", severity="error")
            return

        # Call team.run.start_goal - for now just notify
        self.notify(f"Would create goal: {goal_text} on {target} as {role}")
        self.app.pop_screen()

    @on(Button.Pressed, "#cancel-btn")
    def cancel(self) -> None:
        self.app.pop_screen()


def main() -> int:
    """Entry point for `team deck`."""
    app = TeamDeck()
    app.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())