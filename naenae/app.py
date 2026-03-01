"""Nae Nae — Claude Max Capacity Monitor. macOS menu bar app (PyObjC, no RUMPS)."""

from __future__ import annotations

import os
import signal
import subprocess
import threading
from datetime import datetime, timezone
from typing import Any

import objc
import setproctitle
import yaml
from AppKit import (
    NSApplication,
    NSPopover,
    NSStatusBar,
    NSVariableStatusItemLength,
    NSUserNotification,
    NSUserNotificationCenter,
)
from Foundation import NSObject, NSTimer

from .analysis import build_prediction, get_usage_bar, should_trigger
from .bg_worker import BackgroundWorker
from .onboarding import fix_missing_beads, needs_onboarding, run_onboarding
from .paths import data_dir
from .popover_vc import ControlCenterViewController
from .preflight import format_issues_for_alert, has_errors, run_preflight
from .report import generate_report, open_report
from .spawner import check_running_agents, send_notification, spawn_claude_agent
from .state import load_state, reset_period_if_needed, save_state
from .tasks import filter_tasks, get_ready_tasks, get_task_description

CONFIG_PATH = data_dir() / "config.yaml"


def _safe_load_config() -> tuple[dict[str, Any], str | None]:
    if not CONFIG_PATH.exists():
        return {}, None
    try:
        with CONFIG_PATH.open() as f:
            return yaml.safe_load(f) or {}, None
    except yaml.YAMLError as exc:
        return {}, str(exc)


class NaeNaeApp(NSObject):
    """Main application delegate — NSStatusItem + NSPopover, no RUMPS."""

    def init(self) -> "NaeNaeApp":
        self = objc.super(NaeNaeApp, self).init()
        if self is None:
            return self

        self.config: dict[str, Any] = {}
        self.state: dict[str, Any] = {}
        self._prediction: Any = None
        self._all_ready_tasks: list[Any] = []
        self._ready_tasks: list[Any] = []
        self._has_setup_issues: bool = False

        # Build status item (icon in menu bar)
        status_bar = NSStatusBar.systemStatusBar()
        self._status_item = status_bar.statusItemWithLength_(NSVariableStatusItemLength)
        btn = self._status_item.button()
        if btn:
            btn.setTitle_("● Nae Nae")
            btn.setTarget_(self)
            btn.setAction_("togglePopover:")

        # Build popover
        self._vc = ControlCenterViewController.alloc().init()
        self._vc._app = self

        self._popover = NSPopover.alloc().init()
        self._popover.setContentViewController_(self._vc)
        self._popover.setBehavior_(1)   # NSPopoverBehaviorTransient

        # Background data worker
        self._worker = BackgroundWorker(self)

        # Refresh timer: 5 minutes
        self._timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            300.0, self, "_timerFired:", None, True
        )

        return self

    # ── NSApplicationDelegate ──────────────────────────────────────────────

    def applicationDidFinishLaunching_(self, notification: Any) -> None:
        NSApplication.sharedApplication().setActivationPolicy_(1)   # Accessory
        # Defer first load so the menu bar icon is visible before any dialogs
        NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            0.4, self, "_startup:", None, False
        )

    # ── Startup ───────────────────────────────────────────────────────────

    def _startup_(self, timer: Any) -> None:
        self._load_and_refresh()

    # ── Toggle popover ────────────────────────────────────────────────────

    def togglePopover_(self, sender: Any) -> None:
        if self._popover.isShown():
            self._popover.performClose_(sender)
        else:
            btn = self._status_item.button()
            if btn:
                self._popover.showRelativeToRect_ofView_preferredEdge_(
                    btn.bounds(), btn, 3  # NSRectEdgeMaxY = bottom edge of menu bar
                )
                # Trigger a background refresh when the popover opens
                self._worker.fetch()

    # ── Timer ─────────────────────────────────────────────────────────────

    def _timerFired_(self, timer: Any) -> None:
        self._worker.fetch()

    # ── Background → main thread callback ────────────────────────────────

    def _didFetchData_(self, result: dict) -> None:
        """Called on main thread after BackgroundWorker._run() completes."""
        if not isinstance(result, dict):
            return
        if "error" in result:
            return

        state = result.get("state", {})
        pred = result.get("prediction")
        newly_done = result.get("newly_done", [])

        self.state = state
        self._prediction = pred

        # Handle newly-completed agents
        for agent in newly_done:
            state.setdefault("spawned_this_week", []).append(agent)
            if self.config.get("notifications", {}).get("completion", True):
                send_notification(
                    "Nae Nae",
                    f"{agent['task_id']} completed \u2713 \u2014 {agent['title']} ({agent['project']})",
                )
        if newly_done:
            save_state(state)

        # Refresh task list with current config
        projects = self.config.get("projects", [])
        all_tasks = get_ready_tasks(projects)
        self._all_ready_tasks = all_tasks
        self._ready_tasks = filter_tasks(all_tasks, state, self.config)

        # Update status item title
        self._update_status_title()

        # Update popover if open
        if self._popover.isShown():
            data = {
                "prediction": pred,
                "state": state,
                "ready_tasks": self._all_ready_tasks,
            }
            self._vc.updateWithData_(data)

        # Auto-spawn if trigger conditions met
        if pred and should_trigger(pred, self.config):
            self._spawn_agents()

    # ── Status item title ─────────────────────────────────────────────────

    @objc.python_method
    def _update_status_title(self) -> None:
        pred = self._prediction
        agents_running = self.state.get("agents_running", [])
        n_running = len(agents_running)
        btn = self._status_item.button()
        if btn is None:
            return
        if n_running > 0:
            btn.setTitle_(f"\u2699 {n_running}")
        elif pred and pred.will_trigger:
            btn.setTitle_(f"\u25d0 {100 - pred.projected_pct_all:.0f}%")
        else:
            btn.setTitle_("\u25cf Nae Nae")

    # ── Task/agent actions ────────────────────────────────────────────────

    def spawnTask_(self, task: Any) -> None:
        desc = get_task_description(task)
        record = spawn_claude_agent(task, desc)
        self.state.setdefault("agents_running", []).append(record)
        save_state(self.state)
        self._update_status_title()
        if self.config.get("notifications", {}).get("spawn", True):
            send_notification(
                "Nae Nae",
                f"Starting agent \u2014 {task.task_id}: {task.title} ({task.project_name})",
            )
        # Refresh UI
        self._worker.fetch()

    def stopAgent_(self, pid: int) -> None:
        if pid is None or pid <= 0:
            return
        try:
            os.killpg(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
        self.state["agents_running"] = [
            a for a in self.state.get("agents_running", []) if a.get("pid") != pid
        ]
        save_state(self.state)
        self._update_status_title()
        self._worker.fetch()

    def runBdAction_(self, args_cwd: Any) -> None:
        """Run a bd command in the background, then refresh."""
        args, cwd = args_cwd
        if not cwd:
            return

        def _run() -> None:
            try:
                subprocess.run(
                    ["bd"] + list(args),
                    cwd=cwd,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
            except Exception:
                pass
            finally:
                self._worker.fetch()

        threading.Thread(target=_run, daemon=True).start()

    def _newTaskSheet_(self, sender: Any) -> None:
        """Open config for now — TODO: implement inline task creation form."""
        subprocess.run(["open", str(CONFIG_PATH)], check=False)

    # ── Footer button actions ─────────────────────────────────────────────

    def viewReport_(self, sender: Any) -> None:
        try:
            path = generate_report(self.state, self.config)
            open_report(path)
        except Exception:
            pass
        if self._popover.isShown():
            self._popover.performClose_(sender)

    def openPrefs_(self, sender: Any) -> None:
        subprocess.run(["open", str(CONFIG_PATH)], check=False)

    def quitApp_(self, sender: Any) -> None:
        NSApplication.sharedApplication().terminate_(sender)

    # ── Core load/refresh cycle ───────────────────────────────────────────

    @objc.python_method
    def _load_and_refresh(self) -> None:
        config, yaml_err = _safe_load_config()
        if yaml_err:
            self._show_alert(
                "Nae Nae \u2014 Config Error",
                f"config.yaml syntax error:\n{yaml_err}\n\nFix: open {CONFIG_PATH}",
            )
            btn = self._status_item.button()
            btn and btn.setTitle_("\u25cf Nae Nae \u26a0")
            return

        self.state = load_state()
        self.state = reset_period_if_needed(self.state)

        if needs_onboarding(config) and not self.state.get("onboarding_deferred"):
            updated = run_onboarding(CONFIG_PATH, config)
            if updated is not None:
                config = updated
                self.state.pop("onboarding_deferred", None)
            else:
                self.state["onboarding_deferred"] = True
                save_state(self.state)
                btn = self._status_item.button()
                btn and btn.setTitle_("\u25cf Setup")
                return

        self.config = config

        issues = run_preflight(config)
        tool_errors = [
            i for i in issues
            if i.severity == "error" and "project" not in i.message.lower()
        ]
        if tool_errors:
            self._show_alert("Nae Nae \u2014 Setup Required",
                             format_issues_for_alert(tool_errors))
            btn = self._status_item.button()
            btn and btn.setTitle_("\u25cf Nae Nae \u26a0")

        self._has_setup_issues = bool(issues)
        save_state(self.state)

        # Kick off first data fetch
        self._worker.fetch()

    @objc.python_method
    def _spawn_agents(self) -> None:
        if not self._ready_tasks:
            return
        spawned = []
        for task in self._ready_tasks:
            desc = get_task_description(task)
            record = spawn_claude_agent(task, desc)
            self.state.setdefault("agents_running", []).append(record)
            spawned.append(f"{task.project_name}/{task.task_id}")
        if spawned:
            save_state(self.state)
            self._update_status_title()
            pred = self._prediction
            if self.config.get("notifications", {}).get("spawn", True) and pred:
                msg = (
                    f"Starting {len(spawned)} agent(s) \u2014 "
                    + ", ".join(spawned)
                    + f". {100 - pred.projected_pct_all:.0f}% capacity unused, "
                    + f"{pred.days_remaining:.1f} days left."
                )
                send_notification("Nae Nae", msg)

    @objc.python_method
    def _show_alert(self, title: str, message: str) -> None:
        from AppKit import NSAlert
        alert = NSAlert.alloc().init()
        alert.setMessageText_(title)
        alert.setInformativeText_(message)
        alert.runModal()


# ── PID lock ──────────────────────────────────────────────────────────────────

def _acquire_pid_lock() -> None:
    pid_file = data_dir() / "naenae.pid"
    if pid_file.exists():
        try:
            old_pid = int(pid_file.read_text().strip())
            os.kill(old_pid, 0)
            print(f"Nae Nae already running (PID {old_pid}). Exiting.")
            raise SystemExit(1)
        except (ProcessLookupError, ValueError):
            pass
    pid_file.write_text(str(os.getpid()))


def _release_pid_lock() -> None:
    pid_file = data_dir() / "naenae.pid"
    try:
        pid_file.unlink()
    except FileNotFoundError:
        pass


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    _acquire_pid_lock()
    try:
        setproctitle.setproctitle("Nae Nae")

        app = NSApplication.sharedApplication()
        app.setActivationPolicy_(1)   # NSApplicationActivationPolicyAccessory

        delegate = NaeNaeApp.alloc().init()
        app.setDelegate_(delegate)

        # Trigger startup after run loop starts
        NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            0.1, delegate, "_startup:", None, False
        )

        app.run()
    finally:
        _release_pid_lock()


if __name__ == "__main__":
    main()
