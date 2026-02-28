"""Watcher — Claude Max Capacity Monitor. macOS menu bar app."""

from __future__ import annotations

import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import rumps
import yaml

try:
    from AppKit import NSAttributedString, NSColor, NSForegroundColorAttributeName
    _HAS_APPKIT = True
except ImportError:
    _HAS_APPKIT = False

from .analysis import (
    build_prediction,
    current_billing_period,
    get_usage_bar,
    should_trigger,
)
from .report import generate_report, open_report
from .spawner import check_running_agents, send_notification, spawn_claude_agent
from .state import detect_new_sessions, load_state, reset_period_if_needed, save_state
from .tasks import filter_tasks, get_ready_tasks, get_task_description

ROOT = Path(__file__).parent.parent
CONFIG_PATH = ROOT / "config.yaml"


def load_config() -> dict[str, Any]:
    """Load user config from config.yaml."""
    if not CONFIG_PATH.exists():
        return {}
    with CONFIG_PATH.open() as f:
        return yaml.safe_load(f) or {}


class WatcherApp(rumps.App):
    def __init__(self) -> None:
        super().__init__("● Watcher", quit_button=None)
        self.config: dict[str, Any] = {}
        self.state: dict[str, Any] = {}
        self._ready_tasks: list[Any] = []
        self._prediction: Any = None

        self._build_menu()
        self._load_and_refresh()

    # ── Menu construction ─────────────────────────────────────────────────

    @staticmethod
    def _set_display_title(item: rumps.MenuItem, title: str) -> None:
        """Set title on a display-only (callback=None) item with label-color text.

        macOS greys disabled NSMenuItems by default. Setting an attributedTitle
        with NSColor.labelColor() forces dark text (adapts to light/dark mode)
        while keeping the item disabled so it has no hover highlight — the same
        technique used by the system Battery status menu.
        """
        item.title = title
        if _HAS_APPKIT:
            attrs = {NSForegroundColorAttributeName: NSColor.labelColor()}
            attr_str = NSAttributedString.alloc().initWithString_attributes_(title, attrs)
            item._menuitem.setAttributedTitle_(attr_str)

    def _build_menu(self) -> None:
        self.menu = [
            rumps.MenuItem("📊 Usage This Week", callback=None),
            rumps.MenuItem("  Sonnet: —", callback=None),
            rumps.MenuItem("  Session: —", callback=None),
            rumps.MenuItem("  Session resets: —", callback=None),
            rumps.MenuItem("  Reset: —", callback=None),
            rumps.separator,
            rumps.MenuItem("📋 Task Queue (—)", callback=None),
            rumps.separator,
            rumps.MenuItem("✅ Completed This Week (—)", callback=None),
            rumps.separator,
            rumps.MenuItem("View Full Report", callback=self.view_report, key="r"),
            rumps.MenuItem("Run Now", callback=self.run_now),
            rumps.MenuItem("Preferences…", callback=self.open_prefs),
            rumps.separator,
            rumps.MenuItem("Quit Watcher", callback=rumps.quit_application),
        ]

    # ── Timers ────────────────────────────────────────────────────────────

    @rumps.timer(300)  # Every 5 minutes: refresh display
    def refresh_display(self, _: Any) -> None:
        self._update_analysis()
        self._update_ui()

    @rumps.timer(14400)  # Every 4 hours: full analysis cycle
    def run_analysis_cycle(self, _: Any) -> None:
        self._run_cycle()

    # ── Actions ───────────────────────────────────────────────────────────

    @rumps.clicked("Run Now")
    def run_now(self, _: Any) -> None:
        self._run_cycle(force=True)

    @rumps.clicked("View Full Report")
    def view_report(self, _: Any) -> None:
        try:
            path = generate_report(self.state, self.config)
            open_report(path)
        except Exception as exc:
            rumps.alert("Watcher", f"Failed to generate report:\n{exc}")

    @rumps.clicked("Preferences…")
    def open_prefs(self, _: Any) -> None:
        subprocess.run(["open", str(CONFIG_PATH)], check=False)

    # ── Core logic ────────────────────────────────────────────────────────

    def _load_and_refresh(self) -> None:
        """Initial load on startup."""
        self.config = load_config()
        self.state = load_state()
        self.state = reset_period_if_needed(self.state)
        self._update_analysis()
        self._update_ui()

    def _run_cycle(self, force: bool = False) -> None:
        """Full analysis + optional agent spawning cycle."""
        self.config = load_config()
        self.state = load_state()
        self.state = reset_period_if_needed(self.state)

        # Check for completed agents
        newly_done = check_running_agents(self.state)
        for agent in newly_done:
            self.state.setdefault("spawned_this_week", []).append(agent)
            if self.config.get("notifications", {}).get("completion", True):
                send_notification(
                    "Watcher",
                    f"{agent['task_id']} completed ✓ — {agent['title']} ({agent['project']})",
                )

        self._update_analysis()

        trigger = should_trigger(self._prediction, self.config) or force
        if trigger:
            self._spawn_agents(force=force)

        self.state["last_check"] = datetime.now(timezone.utc).isoformat()
        save_state(self.state)
        self._update_ui()

    def _update_analysis(self) -> None:
        """Recompute token usage and predictions; update state in memory."""
        pred = build_prediction(self.state)
        self._prediction = pred

        # Persist prediction to state
        self.state["predictions"] = {
            "pct_all": pred.pct_all,
            "pct_sonnet": pred.pct_sonnet,
            "output_all": pred.output_all,
            "output_sonnet": pred.output_sonnet,
            "budget_all": pred.budget_all,
            "budget_sonnet": pred.budget_sonnet,
            "days_remaining": pred.days_remaining,
            "reset_label": pred.reset_label,
            "period_start": pred.period_start,
            "period_end": pred.period_end,
            "will_trigger": pred.will_trigger,
            "projected_pct_all": pred.projected_pct_all,
            "session_start": pred.session_start,
            "session_pct_all": pred.session_pct_all,
            "session_pct_sonnet": pred.session_pct_sonnet,
            "session_hours_remaining": pred.session_hours_remaining,
            "session_reset_label": pred.session_reset_label,
            "sessions_remaining_week": pred.sessions_remaining_week,
        }

        # Update ready tasks cache
        projects = self.config.get("projects", [])
        all_tasks = get_ready_tasks(projects)
        self._ready_tasks = filter_tasks(all_tasks, self.state, self.config)

    def _spawn_agents(self, force: bool = False) -> None:
        """Spawn Claude agents for available tasks."""
        if not self._ready_tasks:
            return

        spawned_names = []
        for task in self._ready_tasks:
            desc = get_task_description(task)
            record = spawn_claude_agent(task, desc)
            self.state.setdefault("agents_running", []).append(record)
            spawned_names.append(f"{task.project_name}/{task.task_id}")

        if spawned_names and self.config.get("notifications", {}).get("spawn", True):
            pred = self._prediction
            msg = (
                f"Starting {len(spawned_names)} agent(s) — "
                + ", ".join(spawned_names)
                + f". {100 - pred.projected_pct_all:.0f}% capacity unused, "
                + f"{pred.days_remaining:.1f} days left."
            )
            send_notification("Watcher", msg)

    def _update_ui(self) -> None:
        """Refresh menu bar icon and menu items from current prediction."""
        pred = self._prediction
        if pred is None:
            return

        agents_running = self.state.get("agents_running", [])
        spawned = self.state.get("spawned_this_week", [])
        n_running = len(agents_running)

        # Title / icon
        if n_running > 0:
            self.title = f"⚙ {n_running}"
        elif pred.will_trigger:
            self.title = f"◐ {100 - pred.projected_pct_all:.0f}%"
        else:
            self.title = "● Watcher"

        # All-models usage bar
        bar = get_usage_bar(pred.pct_all)
        self._set_display_title(
            self.menu["📊 Usage This Week"],
            f"📊 All models: {bar} {pred.pct_all:.0f}%",
        )

        # Sonnet-only usage bar
        bar_s = get_usage_bar(pred.pct_sonnet)
        self._set_display_title(
            self.menu["  Sonnet: —"],
            f"  Sonnet only: {bar_s} {pred.pct_sonnet:.0f}%",
        )

        # Session usage bar
        bar_sess = get_usage_bar(pred.session_pct_all)
        self._set_display_title(
            self.menu["  Session: —"],
            f"  Session: {bar_sess} {pred.session_pct_all:.0f}%",
        )

        # Session reset time
        self._set_display_title(
            self.menu["  Session resets: —"],
            f"  Resets {pred.session_reset_label}  ({pred.session_hours_remaining:.1f}h, "
            f"{pred.sessions_remaining_week} sessions left)",
        )

        # Reset time + days remaining
        self._set_display_title(
            self.menu["  Reset: —"],
            f"  Resets {pred.reset_label}  ({pred.days_remaining:.1f}d)",
        )

        # Task queue
        all_tasks_count = len(get_ready_tasks(self.config.get("projects", [])))
        self._set_display_title(
            self.menu["📋 Task Queue (—)"],
            f"📋 Task Queue ({all_tasks_count} ready)",
        )

        # Completed this week
        self._set_display_title(
            self.menu["✅ Completed This Week (—)"],
            f"✅ Completed This Week ({len(spawned)})",
        )


def main() -> None:
    app = WatcherApp()
    app.run()


if __name__ == "__main__":
    main()
