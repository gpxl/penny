"""Penny — Claude Max Capacity Monitor. macOS menu bar app (PyObjC, no RUMPS)."""

from __future__ import annotations

import os
import plistlib
import signal
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import objc
import setproctitle
import yaml
from AppKit import (
    NSApplication,
    NSEvent,
    NSPopover,
    NSStatusBar,
    NSVariableStatusItemLength,
)
from Foundation import NSObject, NSTimer

from .analysis import should_trigger, uses_24h_time
from .bg_worker import BackgroundWorker
from .onboarding import check_full_permissions_consent, needs_onboarding, run_onboarding
from .paths import data_dir
from .plugin import PluginManager
from .popover_vc import ControlCenterViewController
from .preflight import format_issues_for_alert, run_preflight
from .dashboard import DashboardServer
from .report import generate_report, open_report
from .spawner import send_notification, spawn_claude_agent
from .state import load_state, reset_period_if_needed, save_state

CONFIG_PATH = data_dir() / "config.yaml"

PLIST_LABEL = "com.gpxl.penny"
PLIST_LAUNCHAGENTS = Path.home() / "Library" / "LaunchAgents" / f"{PLIST_LABEL}.plist"


def _script_dir_from_plist() -> "Path | None":
    """Return WorkingDirectory from installed plist (= SCRIPT_DIR)."""
    try:
        with PLIST_LAUNCHAGENTS.open("rb") as f:
            pl = plistlib.load(f)
        wd = pl.get("WorkingDirectory", "")
        return Path(wd) if wd else None
    except Exception:
        return None


def _safe_load_config() -> tuple[dict[str, Any], str | None]:
    if not CONFIG_PATH.exists():
        return {}, None
    try:
        with CONFIG_PATH.open() as f:
            return yaml.safe_load(f) or {}, None
    except yaml.YAMLError as exc:
        return {}, str(exc)


def _config_mtime() -> float | None:
    """Return config.yaml's mtime, or None if the file doesn't exist."""
    try:
        return CONFIG_PATH.stat().st_mtime
    except (FileNotFoundError, OSError):
        return None


class PennyApp(NSObject):
    """Main application delegate — NSStatusItem + NSPopover, no RUMPS."""

    def init(self) -> PennyApp:
        self = objc.super(PennyApp, self).init()
        if self is None:
            return self

        self.config: dict[str, Any] = {}
        self.state: dict[str, Any] = {}
        self._prediction: Any = None
        self._all_ready_tasks: list[Any] = []
        self._ready_tasks: list[Any] = []
        self._has_setup_issues: bool = False
        self._last_fetch_at: datetime | None = None
        self._event_monitor: Any = None

        # Plugin system
        self._plugin_mgr = PluginManager()
        self._plugin_mgr.discover()

        # Build status item (icon in menu bar)
        status_bar = NSStatusBar.systemStatusBar()
        self._status_item = status_bar.statusItemWithLength_(NSVariableStatusItemLength)
        btn = self._status_item.button()
        if btn:
            btn.setTitle_("Loading\u2026")
            btn.setTarget_(self)
            btn.setAction_("togglePopover:")

        # Build popover
        self._vc = ControlCenterViewController.alloc().init()
        self._vc._app = self

        self._popover = NSPopover.alloc().init()
        self._popover.setContentViewController_(self._vc)
        self._popover.setBehavior_(0)   # NSPopoverBehaviorApplicationDefined — avoids click-eating on macOS 26+

        # Live dashboard HTTP server (lazy-started on first "View Report")
        self._dashboard = DashboardServer(self)

        # Background data worker
        self._worker = BackgroundWorker(self)

        # Refresh timer: 5 minutes
        self._timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            300.0, self, "_timerFired:", None, True
        )

        # Config watcher: poll mtime every 5s (single stat() syscall, ~1μs)
        self._config_mtime: float | None = None
        self._config_timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            5.0, self, "_checkConfig:", None, True
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
        self._dashboard.ensure_started()
        self._load_and_refresh()

    # ── Toggle popover ────────────────────────────────────────────────────

    def togglePopover_(self, sender: Any) -> None:
        if self._popover.isShown():
            self._popover.performClose_(sender)
            self._remove_event_monitor()
        else:
            btn = self._status_item.button()
            if btn:
                # Activate so the popover renders at full opacity immediately
                NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
                self._popover.showRelativeToRect_ofView_preferredEdge_(
                    btn.bounds(), btn, 3  # NSRectEdgeMaxY = bottom edge of menu bar
                )
                # Show cached data immediately — no force-fetch on open since
                # fetch_live_status spawns claude which counts against session budget.
                # The background timer refreshes every 5 minutes automatically.
                self._vc.updateWithData_({
                    "prediction": self._prediction,
                    "state": self.state,
                    "ready_tasks": self._all_ready_tasks,
                    "fetched_at": self._last_fetch_at,
                })
                # Auto-refresh on first open if no data has loaded yet
                if self._prediction is None:
                    self._worker.fetch(force=True)
                # Global monitor to close on outside click (ApplicationDefined behavior
                # requires manual dismissal — avoids the click-eating bug on macOS 26+).
                self._add_event_monitor()

    # ── Event monitor (outside-click dismissal) ───────────────────────────

    @objc.python_method
    def _add_event_monitor(self) -> None:
        if self._event_monitor is not None:
            return

        def _on_outside_click(event: Any) -> None:
            if self._popover.isShown():
                self._popover.performClose_(None)
            self._remove_event_monitor()

        # NSEventMaskLeftMouseDown = 1 << 1
        self._event_monitor = NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(
            1 << 1, _on_outside_click
        )

    @objc.python_method
    def _remove_event_monitor(self) -> None:
        if self._event_monitor is not None:
            NSEvent.removeMonitor_(self._event_monitor)
            self._event_monitor = None

    # ── Timer ─────────────────────────────────────────────────────────────

    def _timerFired_(self, timer: Any) -> None:
        self._worker.fetch()

    # ── Config hot-reload ──────────────────────────────────────────────────

    def _checkConfig_(self, timer: Any) -> None:
        """Lightweight timer callback: reload config if file changed on disk."""
        mt = _config_mtime()
        if mt is not None and mt != self._config_mtime:
            self._hot_reload_config()

    @objc.python_method
    def _hot_reload_config(self) -> None:
        """Re-read config.yaml and apply changes without restarting."""
        config, yaml_err = _safe_load_config()
        if yaml_err:
            print(f"[penny] config hot-reload skipped (YAML error): {yaml_err}", flush=True)
            return
        self._config_mtime = _config_mtime()
        self.config = config
        self._sync_launchd_service()
        self._plugin_mgr.sync_with_config(self, config)
        print("[penny] config.yaml reloaded", flush=True)

    def refreshNow_(self, sender: Any) -> None:
        """Refresh button: force-bypass cache and fetch live /status data."""
        self._vc.setRefreshing_(True)
        self._worker.fetch(force=True)

    # ── Background → main thread callback ────────────────────────────────

    def _didFetchData_(self, result: dict) -> None:
        """Called on main thread after BackgroundWorker._run() completes."""
        # Always clear loading state regardless of outcome
        self._vc.setRefreshing_(False)
        if not isinstance(result, dict):
            return
        if "error" in result:
            return

        state = result.get("state", {})
        pred = result.get("prediction")
        newly_done = result.get("newly_done", [])

        self.state = state
        self._prediction = pred
        self._last_fetch_at = datetime.now(timezone.utc)

        # Handle newly-completed agents
        for agent in newly_done:
            rc = state.setdefault("recently_completed", [])
            if not any(a.get("task_id") == agent.get("task_id") for a in rc):
                rc.append(agent)
            state["recently_completed"] = rc[-20:]  # keep last 20
            if agent.get("status") != "unknown" and self.config.get("notifications", {}).get("completion", True):
                send_notification(
                    "Penny",
                    f"{agent['task_id']} completed \u2713 \u2014 {agent['title']} ({agent['project']})",
                )
            self._plugin_mgr.notify_agent_completed(agent, state)
        if newly_done:
            save_state(state)

        # Refresh task list from plugins
        projects = self.config.get("projects", [])
        all_tasks = self._plugin_mgr.get_all_tasks(projects)

        # Detect tasks completed by external processes (humans, other tools).
        # Each plugin tracks which IDs it has already reported in plugin_state.
        new_external = self._plugin_mgr.get_all_completed_tasks(projects, state)
        if new_external:
            now_iso = datetime.now(timezone.utc).isoformat()
            for task in new_external:
                record = {
                    "task_id": task.task_id,
                    "project": task.project_name,
                    "project_path": task.project_path,
                    "title": task.title,
                    "priority": task.priority,
                    "status": "completed",
                    "completed_by": "external",
                    "spawned_at": now_iso,
                    "log": "",
                }
                rc = state.setdefault("recently_completed", [])
                rc.append(record)
                state["recently_completed"] = rc[-20:]
            save_state(state)
            if self.config.get("notifications", {}).get("completion", True):
                for task in new_external:
                    send_notification(
                        "Penny",
                        f"{task.task_id} done externally \u2713 \u2014 {task.title} ({task.project_name})",
                    )

        # Exclude tasks already in recently_completed or running from the display list.
        # Tasks closed in beads won't reappear in `bd ready` anyway.
        recently_ids = {a.get("task_id") for a in state.get("recently_completed", [])}
        running_ids = {a.get("task_id") for a in state.get("agents_running", [])}
        exclude_ids = recently_ids | running_ids
        self._all_ready_tasks = [t for t in all_tasks if t.task_id not in exclude_ids]
        self._ready_tasks = self._plugin_mgr.filter_all_tasks(all_tasks, state, self.config)

        # Update status item title
        self._update_status_title()

        # Always update the VC so cached data is fresh for next popover open
        self._vc.updateWithData_({
            "prediction": pred,
            "state": state,
            "ready_tasks": self._all_ready_tasks,
            "fetched_at": self._last_fetch_at,
        })

        # Auto-spawn if trigger conditions met
        if pred and should_trigger(pred, self.config):
            self._spawn_agents()

    # ── Status item title ─────────────────────────────────────────────────

    @objc.python_method
    def _compact_reset_time(self, label: str) -> str:
        """Return a compact reset time string from a reset label.

        Handles multiple formats, converting to 24h if the OS is set that way:
        - Long form 12h (local estimation): "Today at 4:59 PM" → "4:59pm" / "16:59"
        - Long form 24h (local estimation): "Today at 16:59" → "16:59"
        - Compact from live /status data: "4:59pm", "2pm" → convert if 24h mode
        """
        import re
        if not label or label == "—":
            return ""
        use_24h = uses_24h_time()

        # Long form 12h: "Today at 12:00 PM" or "Mon at 5:30 PM"
        m = re.search(r"at (\d+):(\d+) (AM|PM)", label, re.IGNORECASE)
        if m:
            h, mins, ampm = int(m.group(1)), m.group(2), m.group(3).upper()
            if use_24h:
                h24 = (0 if h == 12 else h) if ampm == "AM" else (12 if h == 12 else h + 12)
                return f"{h24}:{mins}" if mins != "00" else str(h24)
            return f"{h}:{mins}{ampm.lower()}" if mins != "00" else f"{h}{ampm.lower()}"

        # Long form 24h: "Today at 16:59" or "Mon at 0:00"
        m = re.search(r"at (\d+):(\d+)$", label)
        if m:
            h, mins = m.group(1), m.group(2)
            return f"{h}:{mins}" if mins != "00" else h

        # Compact live /status data: "4:59pm", "2pm", "12:30am"
        m = re.match(r"^(\d+)(?::(\d+))?(am|pm)$", label, re.IGNORECASE)
        if m and use_24h:
            h, mins, ampm = int(m.group(1)), m.group(2) or "00", m.group(3).upper()
            h24 = (0 if h == 12 else h) if ampm == "AM" else (12 if h == 12 else h + 12)
            return f"{h24}:{mins}" if mins != "00" else str(h24)

        return label

    @objc.python_method
    def _update_status_title(self) -> None:
        pred = self._prediction
        agents_running = self.state.get("agents_running", [])
        n_running = len(agents_running)
        btn = self._status_item.button()
        if btn is None:
            return
        if pred:
            reset_time = self._compact_reset_time(pred.session_reset_label)
            session = f"{pred.session_pct_all:.0f}/{reset_time}" if reset_time else f"{pred.session_pct_all:.0f}"
            stats = f"{session} {pred.pct_all:.0f}/{pred.pct_sonnet:.0f}"
            prefix = "\u26a0\ufe0f " if pred.outage else ""
            if n_running > 0:
                btn.setTitle_(f"{prefix}{stats} \u2728{n_running}")
            else:
                btn.setTitle_(f"{prefix}{stats}")
        elif n_running > 0:
            btn.setTitle_(f"\u2728{n_running}")
        else:
            btn.setTitle_("Loading\u2026")

    # ── Task/agent actions ────────────────────────────────────────────────

    def spawnTask_(self, task: Any) -> None:
        if self.config.get("work", {}).get("agent_permissions") == "off":
            print(f"[penny] agent_permissions=off — skipping spawn of {task.task_id}", flush=True)
            return
        desc = self._plugin_mgr.get_task_description(task)
        prompt_tmpl = self._plugin_mgr.get_agent_prompt_template(task)
        record = spawn_claude_agent(task, desc, interactive=True, prompt_template=prompt_tmpl, config=self.config)
        self.state.setdefault("agents_running", []).append(record)
        self._plugin_mgr.notify_agent_spawned(task, record, self.state)
        # Remove from ready list immediately so the popover reflects the change now
        self._all_ready_tasks = [t for t in self._all_ready_tasks if t.task_id != task.task_id]
        save_state(self.state)
        self._update_status_title()
        if self.config.get("notifications", {}).get("spawn", True):
            send_notification(
                "Penny",
                f"Starting agent \u2014 {task.task_id}: {task.title} ({task.project_name})",
            )
        # Immediately update UI — task moves from Ready → Running Agents without reopen
        self._vc.updateWithData_({
            "prediction": self._prediction,
            "state": self.state,
            "ready_tasks": self._all_ready_tasks,
            "fetched_at": self._last_fetch_at,
        })
        self._worker.fetch()

    def spawnTaskById_(self, task_id: str) -> None:
        """Spawn an agent by task_id string — used by the dashboard API."""
        task = next((t for t in self._all_ready_tasks if t.task_id == str(task_id)), None)
        if task:
            self.spawnTask_(task)

    def stopAgentByTaskId_(self, task_id: str) -> None:
        if not task_id:
            return
        agent = next(
            (a for a in self.state.get("agents_running", []) if a.get("task_id") == task_id),
            None,
        )
        if agent is None:
            return
        session = agent.get("session", "")
        pid = agent.get("pid") or 0
        # Use stored tmux_bin (full path) from agent record to bypass login-PATH gap
        tmux_bin = agent.get("tmux_bin") or "/opt/homebrew/bin/tmux"
        if session:
            subprocess.run([tmux_bin, "kill-session", "-t", session],
                           capture_output=True, check=False)
            subprocess.run(["screen", "-X", "-S", session, "quit"],
                           capture_output=True, check=False)
        if pid > 0:
            try:
                os.killpg(pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
        self.state["agents_running"] = [
            a for a in self.state.get("agents_running", []) if a.get("task_id") != task_id
        ]
        save_state(self.state)
        self._update_status_title()
        # Immediately refresh the VC so the agent row disappears without waiting for fetch
        self._vc.updateWithData_({
            "prediction": self._prediction,
            "state": self.state,
            "ready_tasks": self._all_ready_tasks,
            "fetched_at": self._last_fetch_at,
        })
        self._worker.fetch()

    def stopAgent_(self, pid: int) -> None:
        """Backwards-compatibility shim — looks up by pid then delegates to stopAgentByTaskId_."""
        if pid is None or pid <= 0:
            return
        agent = next(
            (a for a in self.state.get("agents_running", []) if a.get("pid") == pid),
            None,
        )
        if agent:
            self.stopAgentByTaskId_(agent.get("task_id", ""))
        else:
            # No matching agent; clean up by pid directly as before
            try:
                os.killpg(pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass

    def dismissCompleted_(self, task_id: str) -> None:
        rc = self.state.get("recently_completed", [])
        self.state["recently_completed"] = [a for a in rc if a.get("task_id") != task_id]
        save_state(self.state)
        self._vc.updateWithData_({
            "prediction": self._prediction,
            "state": self.state,
            "ready_tasks": self._all_ready_tasks,
            "fetched_at": self._last_fetch_at,
        })

    def clearAllCompleted_(self, sender: Any) -> None:
        self.state["recently_completed"] = []
        save_state(self.state)
        self._vc.updateWithData_({
            "prediction": self._prediction,
            "state": self.state,
            "ready_tasks": self._all_ready_tasks,
            "fetched_at": self._last_fetch_at,
        })

    def runBdAction_(self, args_cwd: Any) -> None:
        """Run a bd command via the beads plugin, then refresh."""
        self.pluginAction_(("bd_command", args_cwd))

    def pluginAction_(self, action_payload: Any) -> None:
        """Dispatch a plugin-specific action in the background, then refresh."""
        action, payload = action_payload

        def _run() -> None:
            try:
                self._plugin_mgr.dispatch_action(str(action), payload)
            except Exception as exc:
                print(f"[penny] pluginAction_ error: {exc}", flush=True)
            finally:
                self._worker.fetch(force=True)

        threading.Thread(target=_run, daemon=True).start()

    @objc.python_method
    def _write_config(self) -> None:
        """Persist self.config to config.yaml."""
        try:
            with CONFIG_PATH.open("w") as f:
                yaml.dump(self.config, f, default_flow_style=False, allow_unicode=True)
        except Exception as exc:
            print(f"[penny] _write_config failed: {exc}", flush=True)

    @objc.python_method
    def _sync_launchd_service(self) -> None:
        """Sync plist KeepAlive/RunAtLoad from config.yaml service: section."""
        svc = self.config.get("service", {})
        want_keep_alive  = bool(svc.get("keep_alive", True))
        want_run_at_load = bool(svc.get("launch_at_login", True))

        if not PLIST_LAUNCHAGENTS.exists():
            return

        try:
            with PLIST_LAUNCHAGENTS.open("rb") as f:
                pl = plistlib.load(f)
        except Exception as exc:
            print(f"[penny] _sync_launchd_service read error: {exc}", flush=True)
            return

        if (pl.get("KeepAlive", True) == want_keep_alive
                and pl.get("RunAtLoad", True) == want_run_at_load):
            return  # already in sync — no-op

        pl["KeepAlive"]  = want_keep_alive
        pl["RunAtLoad"]  = want_run_at_load
        plist_bytes = plistlib.dumps(pl)

        try:
            PLIST_LAUNCHAGENTS.write_bytes(plist_bytes)
        except Exception as exc:
            print(f"[penny] _sync_launchd_service write error: {exc}", flush=True)
            return

        # Update source copy in SCRIPT_DIR (for install.sh re-runs)
        sd = _script_dir_from_plist()
        if sd:
            try:
                (sd / f"{PLIST_LABEL}.plist").write_bytes(plist_bytes)
            except Exception:
                pass

        uid = str(os.getuid())
        subprocess.run(["launchctl", "bootout", f"gui/{uid}/{PLIST_LABEL}"],
                       capture_output=True, check=False)
        subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(PLIST_LAUNCHAGENTS)],
                       capture_output=True, check=False)
        # If bootout succeeded, this process exits here; launchd handles restart

    def toggleKeepAlive_(self, sender: Any) -> None:
        self.config.setdefault("service", {})["keep_alive"] = bool(sender.state())
        self._write_config()
        self._sync_launchd_service()

    def toggleLaunchAtLogin_(self, sender: Any) -> None:
        self.config.setdefault("service", {})["launch_at_login"] = bool(sender.state())
        self._write_config()
        self._sync_launchd_service()

    def _newTaskSheet_(self, sender: Any) -> None:
        subprocess.run(["open", str(CONFIG_PATH)], check=False)

    # ── Footer button actions ─────────────────────────────────────────────

    def viewReport_(self, sender: Any) -> None:
        try:
            port = self._dashboard.ensure_started()
            subprocess.run(["open", f"http://127.0.0.1:{port}/"], check=False)
        except Exception:
            try:  # fallback to static report
                path = generate_report(self.state, self.config, self._plugin_mgr)
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
                "Penny \u2014 Config Error",
                f"config.yaml syntax error:\n{yaml_err}\n\nFix: open {CONFIG_PATH}",
            )
            btn = self._status_item.button()
            btn and btn.setTitle_("\u25cf Penny \u26a0")
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
                # Surface a non-blocking hint so the user knows how to resume
                NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                    0.5, self, "_showSetupHint:", None, False
                )
                return

        self.config = config
        self._config_mtime = _config_mtime()
        self._sync_launchd_service()   # apply any config.yaml service changes at startup

        # One-time consent check: if agent_permissions=full was enabled without going
        # through onboarding, show a confirmation dialog and record consent in state.
        if config.get("work", {}).get("agent_permissions") == "full":
            if not check_full_permissions_consent(config, self.state):
                # User declined — revert to off in config so agents don't spawn
                config.setdefault("work", {})["agent_permissions"] = "off"
                self.config = config
                self._write_config()
                print("[penny] Full-permission consent declined — reverted to off", flush=True)
            else:
                save_state(self.state)

        self._plugin_mgr.sync_with_config(self, config)

        try:
            issues = run_preflight(config)
            issues.extend(self._plugin_mgr.get_all_preflight_checks(config))
        except Exception as exc:
            print(f"[penny] preflight error: {exc}", flush=True)
            issues = []
        tool_errors = [
            i for i in issues
            if i.severity == "error" and "project" not in i.message.lower()
        ]
        if tool_errors:
            self._show_alert("Penny \u2014 Setup Required",
                             format_issues_for_alert(tool_errors))
            btn = self._status_item.button()
            btn and btn.setTitle_("\u25cf Penny \u26a0")

        self._has_setup_issues = bool(issues)
        save_state(self.state)

        # Kick off first data fetch — force=True bypasses disk cache so the
        # menu bar shows live stats immediately on launch, not cached values.
        self._worker.fetch(force=True)

    @objc.python_method
    def _spawn_agents(self) -> None:
        if not self._ready_tasks:
            return
        if self.config.get("work", {}).get("agent_permissions") == "off":
            print("[penny] agent_permissions=off — automatic spawning disabled", flush=True)
            return
        spawned = []
        for task in self._ready_tasks:
            desc = self._plugin_mgr.get_task_description(task)
            prompt_tmpl = self._plugin_mgr.get_agent_prompt_template(task)
            record = spawn_claude_agent(task, desc, interactive=False, prompt_template=prompt_tmpl, config=self.config)
            self.state.setdefault("agents_running", []).append(record)
            self._plugin_mgr.notify_agent_spawned(task, record, self.state)
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
                send_notification("Penny", msg)

    def _showSetupHint_(self, timer: Any) -> None:
        """Non-blocking hint shown after the user clicks 'Set Up Later'."""
        self._show_alert(
            "Penny \u2014 Setup Deferred",
            "Click \u201c\u25cf Setup\u201d in the menu bar to complete configuration whenever you\u2019re ready.",
        )

    @objc.python_method
    def _show_alert(self, title: str, message: str) -> None:
        from AppKit import NSAlert
        alert = NSAlert.alloc().init()
        alert.setMessageText_(title)
        alert.setInformativeText_(message)
        alert.runModal()


# ── PID lock ──────────────────────────────────────────────────────────────────

def _acquire_pid_lock() -> None:
    pid_file = data_dir() / "penny.pid"
    if pid_file.exists():
        try:
            old_pid = int(pid_file.read_text().strip())
            os.kill(old_pid, 0)
            print(f"Penny already running (PID {old_pid}). Exiting.")
            raise SystemExit(1)
        except (ProcessLookupError, ValueError):
            pass
    pid_file.write_text(str(os.getpid()))


def _release_pid_lock() -> None:
    pid_file = data_dir() / "penny.pid"
    try:
        # Only unlink if the file still belongs to this process.
        # Prevents a slow-dying old instance from deleting the new instance's lock.
        stored = int(pid_file.read_text().strip())
        if stored == os.getpid():
            pid_file.unlink()
    except (FileNotFoundError, ValueError):
        pass


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    _acquire_pid_lock()
    try:
        setproctitle.setproctitle("Penny")

        app = NSApplication.sharedApplication()
        app.setActivationPolicy_(1)   # NSApplicationActivationPolicyAccessory

        delegate = PennyApp.alloc().init()
        app.setDelegate_(delegate)

        app.run()
    finally:
        _release_pid_lock()


if __name__ == "__main__":
    main()
