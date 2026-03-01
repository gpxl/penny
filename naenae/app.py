"""Nae Nae — Claude Max Capacity Monitor. macOS menu bar app."""

from __future__ import annotations

import os
import re
import signal
import subprocess
from datetime import datetime, timezone
from typing import Any

import rumps
import setproctitle
import yaml

try:
    from AppKit import (
        NSAlert,
        NSApplication,
        NSAttributedString,
        NSColor,
        NSFont,
        NSFontAttributeName,
        NSForegroundColorAttributeName,
        NSMenu,
        NSMenuItem as _NSMenuItem,
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

# ── Markdown rendering helpers ─────────────────────────────────────────

_SECTION_RE = re.compile(r'^[A-Z][A-Z _]+$')
_INLINE_RE = re.compile(r'\*\*(.+?)\*\*|`(.+?)`')
_BULLET_RE = re.compile(r'^(\s*)- ')


def _extract_description(bd_show_output: str) -> str:
    """Strip the bd show header block; return content from first section header on."""
    lines = bd_show_output.splitlines()
    for i, line in enumerate(lines):
        if _SECTION_RE.match(line.strip()) and line.strip():
            return '\n'.join(lines[i:]).strip()
    return bd_show_output.strip()


def _markdown_to_attrstr(text: str) -> Any:
    """Convert simple markdown to NSMutableAttributedString for display in NSTextView."""
    from AppKit import (  # noqa: PLC0415 — local import, only called when AppKit available
        NSAttributedString,
        NSMutableAttributedString,
        NSFont,
        NSColor,
        NSFontAttributeName,
        NSForegroundColorAttributeName,
    )

    body_font = NSFont.systemFontOfSize_(13)
    bold_font = NSFont.boldSystemFontOfSize_(13)
    mono_font = NSFont.fontWithName_size_("Menlo", 12) or body_font
    hdr_font = NSFont.boldSystemFontOfSize_(11)
    label_color = NSColor.labelColor()
    secondary_color = NSColor.secondaryLabelColor()

    result = NSMutableAttributedString.alloc().init()

    def append(s: str, font: Any, color: Any) -> None:
        if not s:
            return
        attrs = {NSFontAttributeName: font, NSForegroundColorAttributeName: color}
        frag = NSAttributedString.alloc().initWithString_attributes_(s, attrs)
        result.appendAttributedString_(frag)

    for i, raw_line in enumerate(text.splitlines()):
        if i > 0:
            append("\n", body_font, label_color)

        # Section header (e.g. DESCRIPTION, NOTES)
        if _SECTION_RE.match(raw_line.strip()) and raw_line.strip():
            append(raw_line.strip(), hdr_font, secondary_color)
            continue

        # Convert "- item" bullet to "• item"
        line = _BULLET_RE.sub(lambda m: m.group(1) + "• ", raw_line)

        # Inline markdown: **bold** and `code`
        pos = 0
        for m in _INLINE_RE.finditer(line):
            if m.start() > pos:
                append(line[pos:m.start()], body_font, label_color)
            if m.group(1) is not None:   # **bold**
                append(m.group(1), bold_font, label_color)
            elif m.group(2) is not None:  # `code`
                append(m.group(2), mono_font, secondary_color)
            pos = m.end()
        if pos < len(line):
            append(line[pos:], body_font, label_color)

    return result


def _make_content_view(text: str, width: float = 480.0, height: float = 220.0) -> Any:
    """Return a scrollable NSTextView filled with rendered markdown."""
    from AppKit import NSScrollView, NSTextView  # noqa: PLC0415

    frame = ((0.0, 0.0), (width, height))

    scroll = NSScrollView.alloc().initWithFrame_(frame)
    scroll.setHasVerticalScroller_(True)
    scroll.setAutohidesScrollers_(True)
    scroll.setBorderType_(0)  # NSNoBorder

    tv = NSTextView.alloc().initWithFrame_(frame)
    tv.setEditable_(False)
    tv.setSelectable_(True)
    tv.setDrawsBackground_(False)
    tv.setRichText_(True)

    container = tv.textContainer()
    if container is not None:
        container.setWidthTracksTextView_(True)
        container.setContainerSize_((width - 16.0, 1e7))

    tv.textStorage().setAttributedString_(_markdown_to_attrstr(text))

    scroll.setDocumentView_(tv)
    return scroll


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
        self._all_ready_tasks: list[Any] = []
        self._prediction: Any = None
        self._has_setup_issues: bool = False
        # Keep strong references to submenu objects so PyObjC/Cocoa don't GC them
        self._agents_submenu: Any = None
        self._agent_submenu_items: list[Any] = []
        self._task_queue_submenu: Any = None
        self._task_queue_items: list[Any] = []
        self._completed_submenu: Any = None
        self._completed_items: list[Any] = []

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

    def _noop(self, _: Any) -> None:
        """No-op callback so Task Queue and Completed items get a valid action selector,
        preventing macOS from greying them out during menu validation."""

    @staticmethod
    def _set_display_title(
        item: rumps.MenuItem, title: str, secondary: bool = False
    ) -> None:
        """Set title on a display-only item.

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
            item._menuitem.setEnabled_(True)

    def _style_section_header(self, item: rumps.MenuItem) -> None:
        """Style a menu item as a disabled section header (Apple HIG style)."""
        self._set_display_title(item, item.title, secondary=True)
        if _HAS_APPKIT:
            item._menuitem.setEnabled_(False)

    def _build_menu(self) -> None:
        setup_item = rumps.MenuItem("Setup Issues\u2026", callback=self.show_setup_issues)
        self.menu = [
            rumps.separator,
            rumps.MenuItem("Weekly Budget", callback=None),
            rumps.MenuItem("  All models: —", callback=None),
            rumps.MenuItem("  Sonnet: —", callback=None),
            rumps.MenuItem("  Reset: —", callback=None),
            rumps.separator,
            rumps.MenuItem("Session Budget", callback=None),
            rumps.MenuItem("  Session: —", callback=None),
            rumps.MenuItem("  Session resets: —", callback=None),
            rumps.separator,
            rumps.MenuItem("Tasks", callback=None),
            rumps.MenuItem("📋 Task Queue (—)", callback=self._noop),
            rumps.MenuItem("✅ Completed This Week (—)", callback=self._noop),
            rumps.separator,
            rumps.MenuItem("⚙ No agents running", callback=self._noop),
            rumps.separator,
            rumps.MenuItem("View Full Report", callback=self.view_report, key="r"),
            setup_item,
            rumps.MenuItem("Preferences\u2026", callback=self.open_prefs),
            rumps.separator,
            rumps.MenuItem("Quit Nae Nae", callback=rumps.quit_application),
        ]
        # Hidden by default; shown only when issues are detected
        setup_item._menuitem.setHidden_(True)
        # Style section headers as disabled group labels (Apple HIG)
        self._style_section_header(self.menu["Weekly Budget"])
        self._style_section_header(self.menu["Session Budget"])
        self._style_section_header(self.menu["Tasks"])

    # ── Agent submenu ─────────────────────────────────────────────────────

    def _rebuild_agents_menu(self, agents_running: list[Any]) -> None:
        """Rebuild the ⚙ Running Agents submenu.

        Creates a fresh NSMenu on each call and stores strong references on self
        so PyObjC/Cocoa cannot GC them between refreshes. Avoids item._menu
        entirely because setSubmenu_() transfers Cocoa ownership and the PyObjC
        proxy can silently become None.
        """
        item = self.menu["⚙ No agents running"]

        # Detach old submenu and drop all strong refs before rebuilding
        item._menuitem.setSubmenu_(None)
        self._agents_submenu = None
        self._agent_submenu_items = []

        if not agents_running:
            self._set_display_title(item, "⚙ No agents running")
            return

        self._set_display_title(item, f"⚙ {len(agents_running)} Running")

        if not _HAS_APPKIT:
            return

        # Flat submenu: header (disabled) + Open Log + Stop, separated per agent
        submenu = NSMenu.alloc().init()

        for i, agent in enumerate(agents_running):
            task_id = agent.get("task_id", "?")
            title = agent.get("title", "")
            project = agent.get("project", "")
            pid = agent.get("pid")
            log_path = agent.get("log", "")

            label = f"{task_id}: {title}"
            if len(label) > 45:
                label = label[:42] + "…"
            if project:
                label += f"  ({project})"

            # Non-interactive section header
            header = _NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(label, None, "")
            header.setEnabled_(False)
            submenu.addItem_(header)

            # Clickable actions — use rumps MenuItem so callback wiring is handled
            open_log = rumps.MenuItem(
                "   Open Log",
                callback=lambda _, p=log_path: subprocess.run(["open", p], check=False),
            )
            stop = rumps.MenuItem(
                "   Stop",
                callback=lambda _, p=pid: self._stop_agent(p),
            )
            submenu.addItem_(open_log._menuitem)
            submenu.addItem_(stop._menuitem)
            self._agent_submenu_items.extend([open_log, stop])  # keep Python refs alive

            if i < len(agents_running) - 1:
                submenu.addItem_(_NSMenuItem.separatorItem())

        self._agents_submenu = submenu  # keep strong ref so Cocoa doesn't GC it
        item._menuitem.setSubmenu_(submenu)

    def _rebuild_task_queue_menu(self) -> None:
        """Rebuild the 📋 Task Queue submenu with one clickable item per ready task."""
        item = self.menu["📋 Task Queue (—)"]

        # Detach old submenu and drop strong refs before rebuilding
        item._menuitem.setSubmenu_(None)
        self._task_queue_submenu = None
        self._task_queue_items = []

        tasks = self._all_ready_tasks
        count = len(tasks)
        self._set_display_title(item, f"📋 Task Queue ({count} ready)")

        if not tasks or not _HAS_APPKIT:
            return

        running_ids = {a["task_id"] for a in self.state.get("agents_running", [])}

        submenu = NSMenu.alloc().init()

        for task in tasks:
            is_running = task.task_id in running_ids
            label = f"[{task.priority}] {task.project_name}/{task.task_id}: {task.title}"
            if len(label) > 58:
                label = label[:55] + "…"
            if is_running:
                label = f"⚙ {label}"
                ns_item = _NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                    label, None, ""
                )
                ns_item.setEnabled_(False)
                submenu.addItem_(ns_item)
            else:
                menu_item = rumps.MenuItem(
                    label,
                    callback=lambda _, t=task: self._show_task_details(t),
                )
                submenu.addItem_(menu_item._menuitem)
                self._task_queue_items.append(menu_item)  # keep Python ref alive

        self._task_queue_submenu = submenu
        item._menuitem.setSubmenu_(submenu)

    def _show_task_details(self, task: Any) -> None:
        """Show task details dialog; user must confirm before spawning."""
        desc = get_task_description(task)
        if _HAS_APPKIT:
            alert = NSAlert.alloc().init()
            alert.setMessageText_(f"{task.task_id}: {task.title}")
            alert.setInformativeText_(
                f"Project: {task.project_name}   Priority: {task.priority}"
            )
            desc_clean = _extract_description(desc)
            if desc_clean:
                alert.setAccessoryView_(_make_content_view(desc_clean))
                alert.layout()
            alert.addButtonWithTitle_("Run Task")   # response 1000
            alert.addButtonWithTitle_("Cancel")     # response 1001
            if alert.runModal() == 1000:
                self._spawn_single_task(task)
        else:
            self._spawn_single_task(task)

    def _rebuild_completed_tasks_menu(self) -> None:
        """Rebuild the ✅ Completed This Week submenu (newest first)."""
        item = self.menu["✅ Completed This Week (—)"]

        # Detach old submenu and drop strong refs before rebuilding
        item._menuitem.setSubmenu_(None)
        self._completed_submenu = None
        self._completed_items = []

        completed = list(reversed(self.state.get("spawned_this_week", [])))
        if not completed or not _HAS_APPKIT:
            return

        submenu = NSMenu.alloc().init()

        for agent in completed:
            task_id = agent.get("task_id", "?")
            title = agent.get("title", "")
            project = agent.get("project", "")
            label = f"{task_id}: {title}"
            if len(label) > 45:
                label = label[:42] + "…"
            if project:
                label += f"  ({project})"

            menu_item = rumps.MenuItem(
                label,
                callback=lambda _, a=agent: self._show_completed_task_details(a),
            )
            submenu.addItem_(menu_item._menuitem)
            self._completed_items.append(menu_item)

        self._completed_submenu = submenu
        item._menuitem.setSubmenu_(submenu)

    def _show_completed_task_details(self, agent: dict) -> None:
        """Show completed task details with option to open log."""
        task_id  = agent.get("task_id", "?")
        title    = agent.get("title", "—")
        project  = agent.get("project", "—")
        status   = agent.get("status", "completed")
        started  = agent.get("spawned_at", "")[:16].replace("T", " ")
        log_path = agent.get("log", "")

        body = f"Project: {project}\nStatus: {status}\nStarted: {started}"

        if _HAS_APPKIT:
            alert = NSAlert.alloc().init()
            alert.setMessageText_(f"{task_id}: {title}")
            alert.setInformativeText_(body)
            if log_path:
                alert.addButtonWithTitle_("Open Log")   # response 1000
            alert.addButtonWithTitle_("Close")           # response 1000 or 1001
            if alert.runModal() == 1000 and log_path:
                subprocess.run(["open", log_path], check=False)

    def _spawn_single_task(self, task: Any) -> None:
        """Spawn a Claude agent for a single user-selected task."""
        desc = get_task_description(task)
        record = spawn_claude_agent(task, desc)
        self.state.setdefault("agents_running", []).append(record)
        save_state(self.state)
        self._update_ui()
        if self.config.get("notifications", {}).get("spawn", True):
            send_notification(
                "Nae Nae",
                f"Starting agent — {task.task_id}: {task.title} ({task.project_name})",
            )

    def _stop_agent(self, pid: int | None) -> None:
        """Stop a running agent and remove it from state immediately.

        Sends SIGTERM to the process group (start_new_session=True makes the
        spawned claude its own group leader, so this also kills any children).
        Zombies can't be signalled but are still removed from state.
        """
        if pid is None:
            return
        try:
            os.killpg(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass  # already dead or zombie — still remove from state below

        # Remove immediately from in-memory state and persist so the next
        # _update_ui call reflects the change without waiting for the 4-hour cycle.
        self.state["agents_running"] = [
            a for a in self.state.get("agents_running", []) if a.get("pid") != pid
        ]
        save_state(self.state)
        self._update_ui()

    # ── Timers ────────────────────────────────────────────────────────────

    @rumps.timer(300)   # Every 5 minutes: refresh display
    def refresh_display(self, _: Any) -> None:
        self._update_analysis()
        self._update_ui()

    @rumps.timer(14400)  # Every 4 hours: full analysis + spawn cycle
    def run_analysis_cycle(self, _: Any) -> None:
        self._run_cycle()

    # ── Actions ───────────────────────────────────────────────────────────

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
        self._all_ready_tasks = all_tasks
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
            self.menu["  All models: —"],
            f"  All models: {bar} {pred.pct_all:.0f}%",
            secondary=True,
        )

        bar_s = get_usage_bar(pred.pct_sonnet)
        self._set_display_title(
            self.menu["  Sonnet: —"],
            f"  Sonnet: {bar_s} {pred.pct_sonnet:.0f}%",
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
            f"  Resets {pred.session_reset_label}  ({pred.session_hours_remaining:.1f}h)",
            secondary=True,
        )

        self._set_display_title(
            self.menu["  Reset: —"],
            f"  Resets {pred.reset_label}  ({pred.days_remaining:.1f}d)",
            secondary=True,
        )

        self._set_display_title(
            self.menu["✅ Completed This Week (—)"],
            f"✅ Completed This Week ({len(spawned)})",
        )

        self._rebuild_task_queue_menu()
        self._rebuild_completed_tasks_menu()
        self._rebuild_agents_menu(agents_running)


def _acquire_pid_lock() -> None:
    """Ensure only one instance runs. Exits immediately if another is alive."""
    pid_file = data_dir() / "naenae.pid"
    if pid_file.exists():
        try:
            old_pid = int(pid_file.read_text().strip())
            os.kill(old_pid, 0)  # raises if process is gone
            print(f"Nae Nae already running (PID {old_pid}). Exiting.")
            raise SystemExit(1)
        except (ProcessLookupError, ValueError):
            pass  # stale PID — overwrite below
    pid_file.write_text(str(os.getpid()))


def _release_pid_lock() -> None:
    pid_file = data_dir() / "naenae.pid"
    try:
        pid_file.unlink()
    except FileNotFoundError:
        pass


def main() -> None:
    _acquire_pid_lock()
    try:
        setproctitle.setproctitle("Nae Nae")
        app = NaeNaeApp()
        app.run()
    finally:
        _release_pid_lock()


if __name__ == "__main__":
    main()
