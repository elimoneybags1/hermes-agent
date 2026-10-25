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
from textual.events import Key
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
        Binding("f1", "switch_tab('pipeline')", "Pipeline", show=True),
        Binding("f2", "switch_tab('inbox')", "Inbox", show=True),
        Binding("f3", "switch_tab('agents')", "Agents", show=True),
        Binding("f4", "switch_tab('log')", "Log", show=True),
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
        with TabbedContent(id="tabs"):
            with TabPane("Pipeline", id="pipeline-tab"):
                yield Static("", id="pipeline-strip")
                yield DataTable(id="pipeline-table")
            with TabPane("Inbox", id="inbox-tab"):
                yield ListView(id="inbox-list")
            with TabPane("Agents", id="agents-tab"):
                yield DataTable(id="agents-table")
            with TabPane("Log", id="log-tab"):
                yield DataTable(id="log-table")
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
        self._update_run_status(is_running)

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
                    item = ListItem(Static(item_text, classes="decision-item"))
                    item.decision_id = d["id"]  # Store decision ID on the item
                    item.options = options  # Store options for answering
                    list_view.append(item)
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

    # Key event handling
    def on_key(self, event: Key) -> None:
        """Handle key events for tab-specific actions."""
        tabs = self.query_one("#tabs", TabbedContent)
        active_tab = tabs.active
        
        if active_tab == "inbox-tab":
            self._handle_inbox_keys(event)
        elif active_tab == "pipeline-tab":
            self._handle_pipeline_keys(event)
        elif active_tab == "agents-tab":
            self._handle_agents_keys(event)

    def _handle_inbox_keys(self, event: Key) -> None:
        """Handle key presses in the Inbox tab."""
        list_view = self.query_one("#inbox-list", ListView)
        if not list_view.children:
            return
        
        highlighted = list_view.highlighted_child
        if highlighted is None:
            return
        
        decision_id = getattr(highlighted, "decision_id", None)
        if decision_id is None:
            return
        
        options = getattr(highlighted, "options", [])
        
        # 1-9: answer with that option
        if event.key.isdigit() and event.key != "0":
            idx = int(event.key) - 1
            if 0 <= idx < len(options):
                self._answer_decision(decision_id, options[idx])
                event.prevent_default()
                return
        
        # a: free answer
        if event.key == "a":
            self._free_answer_decision(decision_id)
            event.prevent_default()
            return
        
        # d: dismiss with y/n confirm
        if event.key == "d":
            self._dismiss_decision(decision_id)
            event.prevent_default()
            return

    def _handle_pipeline_keys(self, event: Key) -> None:
        """Handle key presses in the Pipeline tab."""
        if event.key == "enter":
            table = self.query_one("#pipeline-table", DataTable)
            if table.cursor_row is not None:
                row_key = table.get_row_at(table.cursor_row)
                if row_key:
                    goal_id = int(row_key[0])  # First column is ID
                    self._show_goal_detail(goal_id)
                    event.prevent_default()
            return
        
        if event.key == "escape":
            # Go back from detail view (if we're in one)
            self._go_back_from_detail()
            event.prevent_default()
            return

    def _handle_agents_keys(self, event: Key) -> None:
        """Handle key presses in the Agents tab."""
        table = self.query_one("#agents-table", DataTable)
        if table.cursor_row is None:
            return
        
        row_key = table.get_row_at(table.cursor_row)
        if not row_key:
            return
        
        role_name = row_key[0]  # First column is Name
        
        # c: open chat in new herdr pane
        if event.key == "c":
            self._open_agent_chat(role_name)
            event.prevent_default()
            return
        
        # e: open team ui in browser at agent overview
        if event.key == "e":
            self._open_agent_ui(role_name)
            event.prevent_default()
            return

    def _answer_decision(self, decision_id: int, answer: str) -> None:
        """Answer a decision with a predefined option."""
        conn = self._get_ledger_conn()
        if conn is not None:
            try:
                from team import decisions
                decisions.answer(conn, decision_id, answer)
                self.notify(f"Answered decision {decision_id}: {answer}")
                self._refresh_ui()
            except Exception as e:
                self.notify(f"Failed to answer: {e}", severity="error")
            finally:
                conn.close()

    def _free_answer_decision(self, decision_id: int) -> None:
        """Show input for free-form answer."""
        self.push_screen(FreeAnswerScreen(decision_id))

    def _dismiss_decision(self, decision_id: int) -> None:
        """Dismiss a decision with confirmation."""
        self.push_screen(DismissConfirmScreen(decision_id))

    def _show_goal_detail(self, goal_id: int) -> None:
        """Show detailed view of a goal."""
        conn = self._get_ledger_conn()
        if conn is None:
            return
        try:
            goal = conn.execute("SELECT * FROM goals WHERE id = ?", (goal_id,)).fetchone()
            if goal:
                self.push_screen(GoalDetailScreen(goal))
        finally:
            conn.close()

    def _go_back_from_detail(self) -> None:
        """Go back from a detail screen."""
        # The screen stack handles this with pop_screen
        pass

    def _open_agent_chat(self, role_name: str) -> None:
        """Open a chat with an agent in a new herdr pane."""
        if role_name == "boss":
            self.notify("Boss is the left pane", severity="warning")
            return
        
        import subprocess
        import json
        import os
        from pathlib import Path
        
        pane_id = os.environ.get("HERDR_PANE_ID")
        if not pane_id:
            self.notify("Not in a herdr pane", severity="error")
            return
        
        try:
            # Split pane to the right
            result = subprocess.run(
                [
                    "herdr", "pane", "split", pane_id,
                    "--direction", "right",
                    "--cwd", str(Path.home()),
                    "--no-focus"
                ],
                capture_output=True,
                text=True,
                check=True
            )
            
            # Parse the new pane ID from JSON output
            output = result.stdout.strip()
            new_pane_id = None
            try:
                data = json.loads(output)
                if "result" in data and "pane" in data["result"]:
                    new_pane_id = data["result"]["pane"]["pane_id"]
                elif "pane_id" in data:
                    new_pane_id = data["pane_id"]
            except json.JSONDecodeError:
                # Try root_pane format
                new_pane_id = output
            
            if new_pane_id:
                # Send hermes command to the new pane
                subprocess.run(
                    ["herdr", "pane", "send-text", new_pane_id, f"hermes -p team-{role_name} chat"],
                    check=True
                )
                subprocess.run(
                    ["herdr", "pane", "send-keys", new_pane_id, "enter"],
                    check=True
                )
                self.notify(f"Opened chat for {role_name} in pane {new_pane_id}")
            else:
                self.notify("Failed to parse new pane ID", severity="error")
        except subprocess.CalledProcessError as e:
            self.notify(f"Failed to open chat: {e}", severity="error")
        except Exception as e:
            self.notify(f"Error: {e}", severity="error")

    def _open_agent_ui(self, role_name: str) -> None:
        """Open team UI in browser at agent overview."""
        import subprocess
        url = f"http://127.0.0.1:7341/#{role_name}/overview"
        try:
            subprocess.run(["open", url], check=True)
            self.notify(f"Opened {role_name} in browser")
        except subprocess.CalledProcessError:
            self.notify("Failed to open browser", severity="error")


class FreeAnswerScreen(Screen):
    """Screen for free-form answer to a decision."""

    CSS = """
    FreeAnswerScreen {
        align: center middle;
    }
    #answer-form {
        width: 80;
        height: auto;
        padding: 2;
        border: solid $primary;
        background: $surface;
    }
    #answer-form Label {
        margin-top: 1;
    }
    #answer-form Input {
        margin-bottom: 1;
    }
    #answer-form #buttons {
        margin-top: 1;
        layout: horizontal;
    }
    #answer-form Button {
        margin-right: 1;
    }
    """

    def __init__(self, decision_id: int) -> None:
        super().__init__()
        self.decision_id = decision_id

    def compose(self) -> ComposeResult:
        with Container(id="answer-form"):
            yield Label("Free Answer")
            yield Input(placeholder="Your answer", id="answer-input")
            with Horizontal(id="buttons"):
                yield Button("Submit", variant="primary", id="submit-btn")
                yield Button("Cancel", variant="default", id="cancel-btn")

    @on(Button.Pressed, "#submit-btn")
    def submit_answer(self) -> None:
        answer = self.query_one("#answer-input", Input).value
        if not answer:
            self.notify("Answer cannot be empty", severity="error")
            return
        
        conn = self.app._get_ledger_conn()
        if conn is not None:
            try:
                from team import decisions
                decisions.answer(conn, self.decision_id, answer)
                self.app.notify(f"Answered decision {self.decision_id}")
                self.app._refresh_ui()
            except Exception as e:
                self.app.notify(f"Failed to answer: {e}", severity="error")
            finally:
                conn.close()
        self.app.pop_screen()

    @on(Button.Pressed, "#cancel-btn")
    def cancel(self) -> None:
        self.app.pop_screen()


class DismissConfirmScreen(Screen):
    """Screen for confirming decision dismissal."""

    CSS = """
    DismissConfirmScreen {
        align: center middle;
    }
    #confirm-form {
        width: 60;
        height: auto;
        padding: 2;
        border: solid $primary;
        background: $surface;
    }
    #confirm-form Label {
        margin-bottom: 1;
    }
    #confirm-form #buttons {
        margin-top: 1;
        layout: horizontal;
    }
    #confirm-form Button {
        margin-right: 1;
    }
    """

    def __init__(self, decision_id: int) -> None:
        super().__init__()
        self.decision_id = decision_id

    def compose(self) -> ComposeResult:
        with Container(id="confirm-form"):
            yield Label(f"Dismiss decision {self.decision_id}? (y/n)")
            with Horizontal(id="buttons"):
                yield Button("Yes", variant="error", id="yes-btn")
                yield Button("No", variant="default", id="no-btn")

    @on(Button.Pressed, "#yes-btn")
    def confirm_dismiss(self) -> None:
        conn = self.app._get_ledger_conn()
        if conn is not None:
            try:
                from team import decisions
                decisions.dismiss(conn, self.decision_id)
                self.app.notify(f"Dismissed decision {self.decision_id}")
                self.app._refresh_ui()
            except Exception as e:
                self.app.notify(f"Failed to dismiss: {e}", severity="error")
            finally:
                conn.close()
        self.app.pop_screen()

    @on(Button.Pressed, "#no-btn")
    def cancel(self) -> None:
        self.app.pop_screen()


class GoalDetailScreen(Screen):
    """Screen showing detailed view of a goal."""

    CSS = """
    GoalDetailScreen {
        align: center middle;
    }
    #detail-container {
        width: 90%;
        height: 90%;
        padding: 2;
        border: solid $primary;
        background: $surface;
        layout: vertical;
    }
    #detail-header {
        margin-bottom: 1;
        text-style: bold;
    }
    #detail-content {
        height: 1fr;
        overflow-y: auto;
    }
    #detail-footer {
        margin-top: 1;
        layout: horizontal;
    }
    #detail-footer Button {
        margin-right: 1;
    }
    """

    def __init__(self, goal: dict) -> None:
        super().__init__()
        self.goal = goal

    def compose(self) -> ComposeResult:
        with Container(id="detail-container"):
            yield Label(f"Goal #{self.goal['id']} — {self.goal['target']} / {self.goal['role']}", id="detail-header")
            with Container(id="detail-content"):
                yield Static(self._build_detail_text())
            with Horizontal(id="detail-footer"):
                yield Button("Close", variant="primary", id="close-btn")

    def _build_detail_text(self) -> str:
        lines = [
            f"Goal: {self.goal['goal']}",
            f"Branch: {self.goal['branch']}",
            f"Worktree: {self.goal['worktree']}",
            f"Check: {self.goal['check_cmd']}",
            f"Attempt: {self.goal['attempt']}",
            f"Created: {self.goal['created']}",
            "",
            "=== Attempts (Jobs) ===",
        ]
        
        # Fetch jobs for this goal
        conn = self.app._get_ledger_conn()
        if conn is not None:
            try:
                jobs = conn.execute(
                    "SELECT * FROM jobs WHERE task_ref LIKE ? ORDER BY id",
                    (f"%goal:{self.goal['id']}%",)
                ).fetchall()
                if not jobs:
                    # Also try by target/role
                    jobs = conn.execute(
                        "SELECT * FROM jobs WHERE target = ? AND role = ? ORDER BY id",
                        (self.goal['target'], self.goal['role'])
                    ).fetchall()
                
                for job in jobs:
                    lines.append(f"Job #{job['id']}: {job['outcome'] or 'running'} (attempt {job['attempt']})")
                    lines.append(f"  Model: {job.get('role_version', 'unknown')}")
                    lines.append(f"  Started: {job['started']}")
                    if job['ended']:
                        lines.append(f"  Ended: {job['ended']}")
                    if job['grader']:
                        lines.append(f"  Grader: {job['grader']}")
                    if job['grader_detail']:
                        detail = job['grader_detail'][:200]
                        lines.append(f"  Detail: {detail}")
                    lines.append("")
            finally:
                conn.close()
        
        return "\n".join(lines)

    @on(Button.Pressed, "#close-btn")
    def close(self) -> None:
        self.app.pop_screen()


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


# Actions (these belong to TeamDeck, not NewGoalScreen)
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


def main() -> int:
    """Entry point for `team deck`."""
    app = TeamDeck()
    app.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())