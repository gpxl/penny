"""Nae Nae — Claude Max Capacity Monitor. macOS menu bar app."""

from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from typing import Any

import rumps
import yaml

try:
    from AppKit import (
        NSApplication,
        NSAttributedString,
        NSColor,
        NSFont,
        NSFontAttributeName,
        NSForegroundColorAttributeName,
    )
    _HAS_APPKIT = True
except ImportError:
    _HAS_APPKIT = False

from .analysis import (
    build_prediction,
    get_usage_bar,
    should_trigger,
)
from .onboarding import fix_missing_beads, needs_onboarding, run_onboarding
from .paths import data_dir
from .preflight import (
    format_issues_for_alert,
    has_errors,
    run_preflight,
)
from .report import generate_report, open_report
from .spawner import check_running_agents, send_notification, spawn_claude_agent
from .state import load_state, reset_period_if_needed, save_state
from .tasks import filter_tasks, get_ready_tasks, get_task_description

CONFIG_PATH = data_dir() / "config.yaml"


def _safe_load_config() -> tuple[dict[str, Any], str | None]:
    """Load config.yaml.

    Returns (config_dict, error_str). On YAML parse failure returns
    ({}, error_message) instead of crashing.
    """
    if not CONFIG_PATH.exists():
        return {}, None
    try:
        with CONFIG_PATH.open() as f:
            return yaml.safe_load(f) or {}, None
    except yaml.YAMLError as exc:
        return {}, str(exc)


class NaeNaeApp(rumps.App):
    def __init__(self) -> None:
        super().__init__("● Nae Nae", quit_button=None)

        self.config: dict[str, Any] = {}
        self.state: dict[str, Any] = {}
        self._ready_tasks: list[Any] = []
        self._prediction: Any = None
        self._has_setup_issues: bool = False

        self._build_menu()

        # Defer first load until the event loop is running so the menu bar
        # title is already visible when any onboarding dialogs appear.
        self._startup_timer = rumps.Timer(self._on_startup, 0.4)
        self._startup_timer.start()

    # ── Startup ───────────────────────────────────────────────────────────

    def _on_startup(self, timer: Any) -> None:
        timer.stop()
        # Set activation policy now that the event loop is running and NSApplication
        # is fully initialised — hides the Python Dock icon (Apple HIG: background agents).
        if _HAS_APPKIT:
            NSApplication.sharedApplication().setActivationPolicy_(1)
        self._load_and_refresh()

    # ── Setup item visibility ─────────────────────────────────────────────

    def _refresh_setup_item(self) -> None:
        """Show 'Setup Issues…' only when there are actual issues to address."""
        self.menu["Setup Issues\u2026"]._menuitem.setHidden_(not self._has_setup_issues)

    # ── Menu construction ─────────────────────────────────────────────────

    @staticmethod
    def _set_display_title(
        item: rumps.MenuItem, title: str, secondary: bool = False
    ) -> None:
        """Set title on a display-only (callback=None) item.

        Primary items (section headers) use labelColor at the standard menu font
        size. Secondary items (indented stat rows) use secondaryLabelColor at
        11pt — visually distinct from interactive menu items, matching the style
        used by native macOS status menus like Battery and Wi-Fi.
        """
        item.title = title
        if _HAS_APPKIT:
            if secondary:
                color = NSColor.secondaryLabelColor()
                font = NSFont.menuFontOfSize_(11)
            else:
                color = NSColor.labelColor()
                font = NSFont.menuFontOfSize_(0)
            attrs = {
                NSForegroundColorAttributeName: color,
                NSFontAttributeName: font,
            }
            attr_str = NSAttributedString.alloc().initWithString_attributes_(title, attrs)
            item._menuitem.setAttributedTitle_(attr_str)
            # rumps sets setEnabled_(False) for callback=None items, which causes macOS
            # to grey out the text even when an attributedTitle is set.
            # Re-enable so our color/font are respected (matches Battery menu style).
            item._menuitem.setEnabled_(True)

    def _build_menu(self) -> None:
        setup_item = rumps.MenuItem("Setup Issues\u2026", callback=self.show_setup_issues)
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
            setup_item,
            rumps.MenuItem("Preferences\u2026", callback=self.open_prefs),
            rumps.separator,
            rumps.MenuItem("Quit Nae Nae", callback=rumps.quit_application),
        ]
        # Hidden by default; shown only when issues are detected
        setup_item._menuitem.setHidden_(True)

    # ── Timers ────────────────────────────────────────────────────────────

    @rumps.timer(300)   # Every 5 minutes: refresh display
    def refresh_display(self, _: Any) -> None:
        self._update_analysis()
        self._update_ui()

    @rumps.timer(14400)  # Every 4 hours: full analysis + spawn cycle
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
            rumps.alert("Nae Nae", f"Failed to generate report:\n{exc}")

    @rumps.clicked("Setup Issues\u2026")
    def show_setup_issues(self, _: Any) -> None:
        """Re-run setup checks, auto-fix what can be fixed, then report status."""
        config, yaml_err = _safe_load_config()
        if yaml_err:
            rumps.alert(
                "Nae Nae \u2014 Config Error",
                f"config.yaml has a syntax error:\n{yaml_err}\n\nFix: open {CONFIG_PATH}",
            )
            return

        if needs_onboarding(config):
            updated = run_onboarding(CONFIG_PATH, config)
            if updated is not None:
                self.config = updated
                self.state.pop("onboarding_deferred", None)
                save_state(self.state)
                self._update_analysis()
                self._update_ui()
            return

        # Auto-fix projects missing .beads/ before running the full preflight check
        fixed = fix_missing_beads(config)

        issues = run_preflight(config)
        self._has_setup_issues = bool(issues)
        self._refresh_setup_item()

        if not issues:
            msg = "\u2705 All checks passed."
            if fixed:
                msg += f"\n\nBeads initialised in: {', '.join(fixed)}"
            rumps.alert("Nae Nae \u2014 Setup", msg)
        else:
            body = format_issues_for_alert(issues)
            if fixed:
                body += f"\n\n\u2705 Auto-fixed: Beads initialised in {', '.join(fixed)}"
            rumps.alert("Nae Nae \u2014 Setup Issues", body)

    @rumps.clicked("Preferences\u2026")
    def open_prefs(self, _: Any) -> None:
        subprocess.run(["open", str(CONFIG_PATH)], check=False)

    # ── Core logic ────────────────────────────────────────────────────────

    def _load_and_refresh(self) -> None:
        """Initial load — runs once after the event loop starts."""
        config, yaml_err = _safe_load_config()
        if yaml_err:
            rumps.alert(
                "Nae Nae \u2014 Config Error",
                f"config.yaml syntax error:\n{yaml_err}\n\nFix: open {CONFIG_PATH}",
            )
            self.title = "\u25cf Nae Nae \u26a0"
            return

        self.state = load_state()
        self.state = reset_period_if_needed(self.state)

        # First-run onboarding — only if user hasn't already deferred
        if needs_onboarding(config) and not self.state.get("onboarding_deferred"):
            updated = run_onboarding(CONFIG_PATH, config)
            if updated is not None:
                config = updated
                self.state.pop("onboarding_deferred", None)
            else:
                # User clicked "Set Up Later"
                self.state["onboarding_deferred"] = True
                save_state(self.state)
                self.title = "\u25cf Setup"
                return   # Don't run analysis — no projects configured yet

        self.config = config

        # Surface remaining errors (missing CLI tools, etc.) via a simple alert.
        # Project-related errors are already handled by the onboarding wizard above.
        issues = run_preflight(config)
        tool_errors = [
            i for i in issues
            if i.severity == "error" and "project" not in i.message.lower()
        ]
        if tool_errors:
            rumps.alert("Nae Nae \u2014 Setup Required", format_issues_for_alert(tool_errors))
            self.title = "\u25cf Nae Nae \u26a0"

        self._has_setup_issues = bool(issues)
        self._refresh_setup_item()

        save_state(self.state)
        self._update_analysis()
        self._update_ui()

    def _run_cycle(self, force: bool = False) -> None:
        """Full analysis + optional agent spawning cycle."""
        config, yaml_err = _safe_load_config()
        if yaml_err:
            rumps.alert(
                "Nae Nae \u2014 Config Error",
                f"config.yaml syntax error:\n{yaml_err}\n\nFix: open {CONFIG_PATH}",
            )
            return
        self.config = config

        self.state = load_state()
        self.state = reset_period_if_needed(self.state)

        # Check for completed agents
        newly_done = check_running_agents(self.state)
        for agent in newly_done:
            self.state.setdefault("spawned_this_week", []).append(agent)
            if self.config.get("notifications", {}).get("completion", True):
                send_notification(
                    "Nae Nae",
                    f"{agent['task_id']} completed \u2713 \u2014 {agent['title']} ({agent['project']})",
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
                f"Starting {len(spawned_names)} agent(s) \u2014 "
                + ", ".join(spawned_names)
                + f". {100 - pred.projected_pct_all:.0f}% capacity unused, "
                + f"{pred.days_remaining:.1f} days left."
            )
            send_notification("Nae Nae", msg)

    def _update_ui(self) -> None:
        """Refresh menu bar icon and menu items from current prediction."""
        pred = self._prediction
        if pred is None:
            return

        agents_running = self.state.get("agents_running", [])
        spawned = self.state.get("spawned_this_week", [])
        n_running = len(agents_running)

        if n_running > 0:
            self.title = f"\u2699 {n_running}"
        elif pred.will_trigger:
            self.title = f"\u25d0 {100 - pred.projected_pct_all:.0f}%"
        else:
            self.title = "\u25cf Nae Nae"

        bar = get_usage_bar(pred.pct_all)
        self._set_display_title(
            self.menu["📊 Usage This Week"],
            f"📊 All models: {bar} {pred.pct_all:.0f}%",
        )

        bar_s = get_usage_bar(pred.pct_sonnet)
        self._set_display_title(
            self.menu["  Sonnet: —"],
            f"  Sonnet only: {bar_s} {pred.pct_sonnet:.0f}%",
            secondary=True,
        )

        bar_sess = get_usage_bar(pred.session_pct_all)
        self._set_display_title(
            self.menu["  Session: —"],
            f"  Session: {bar_sess} {pred.session_pct_all:.0f}%",
            secondary=True,
        )

        self._set_display_title(
            self.menu["  Session resets: —"],
            f"  Resets {pred.session_reset_label}  ({pred.session_hours_remaining:.1f}h, "
            f"{pred.sessions_remaining_week} sessions left)",
            secondary=True,
        )

        self._set_display_title(
            self.menu["  Reset: —"],
            f"  Resets {pred.reset_label}  ({pred.days_remaining:.1f}d)",
            secondary=True,
        )

        all_tasks_count = len(get_ready_tasks(self.config.get("projects", [])))
        self._set_display_title(
            self.menu["📋 Task Queue (—)"],
            f"📋 Task Queue ({all_tasks_count} ready)",
        )

        self._set_display_title(
            self.menu["✅ Completed This Week (—)"],
            f"✅ Completed This Week ({len(spawned)})",
        )


def main() -> None:
    app = NaeNaeApp()
    app.run()


if __name__ == "__main__":
    main()
