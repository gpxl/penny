"""Penny — Claude Code Token Monitor. macOS menu bar app (PyObjC, no RUMPS)."""

from __future__ import annotations

import os
import plistlib
import shutil
import signal
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
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
from .dashboard import DashboardServer, bump_state_generation
from .deps import ensure_deps
from .onboarding import check_full_permissions_consent, needs_onboarding, run_onboarding
from .paths import data_dir
from .plugin import PluginManager
from .popover_vc import ControlCenterViewController
from .preflight import format_issues_for_alert, run_preflight
from .report import generate_report, open_report
from .spawner import send_notification, spawn_claude_agent
from .state import load_state, reset_period_if_needed, save_state

ensure_deps()

CONFIG_PATH = data_dir() / "config.yaml"

PLIST_LABEL = "com.gpxl.penny"
PLIST_LAUNCHAGENTS = Path.home() / "Library" / "LaunchAgents" / f"{PLIST_LABEL}.plist"


def _script_dir_from_plist() -> Path | None:
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
            cfg = yaml.safe_load(f) or {}
            return _normalize_config(cfg), None
    except yaml.YAMLError as exc:
        return {}, str(exc)


_LEGACY_BAR_MODES = {
    "hbars": "bars", "bars+t": "bars", "hbars+t": "bars",
    "compact": "bars", "minimal": "bars",
}


def _normalize_config(cfg: dict[str, Any]) -> dict[str, Any]:
    """Migrate legacy menubar mode values to current names."""
    mb = cfg.get("menubar", {})
    if isinstance(mb, dict):
        mode = mb.get("mode", "")
        if mode in _LEGACY_BAR_MODES:
            mb["mode"] = _LEGACY_BAR_MODES[mode]
    return cfg


def _config_mtime() -> float | None:
    """Return config.yaml's mtime, or None if the file doesn't exist."""
    try:
        return CONFIG_PATH.stat().st_mtime
    except (FileNotFoundError, OSError):
        return None


# ── Easing helpers ──────────────────────────────────────────────────────
# Cubic curves: standard for UI motion — perceptually smooth without
# feeling sluggish.  Each function maps t ∈ [0,1] → [0,1].


def _ease_out_cubic(t: float) -> float:
    """Decelerates to zero velocity — use for elements arriving."""
    return 1.0 - (1.0 - t) ** 3


def _ease_in_cubic(t: float) -> float:
    """Accelerates from zero velocity — use for elements departing."""
    return t * t * t


def _ease_in_out_cubic(t: float) -> float:
    """Accelerates then decelerates — use for settling to a value."""
    if t < 0.5:
        return 4.0 * t * t * t
    return 1.0 - (-2.0 * t + 2.0) ** 3 / 2.0


def _AnimPred(session_pct_all, pct_all, pct_sonnet, session_hours_remaining, outage=False,
              countdown_pct=None, countdown_emptying=False):
    """Create a fake prediction namespace for passing animated values to _make_status_image.

    countdown_pct: optional explicit arc fill (0-100).  When set, the countdown
    arc renders at this exact percentage instead of deriving from session_pct_all.
    Used by the loading animation to sweep the arc independently of bar values.
    countdown_emptying: when True the filled wedge is anchored at the leading
    edge and the trailing edge sweeps clockwise (i.e. "draining" clockwise).
    """
    return SimpleNamespace(
        session_pct_all=session_pct_all, pct_all=pct_all,
        pct_sonnet=pct_sonnet, session_hours_remaining=session_hours_remaining,
        outage=outage, session_reset_label="",
        _countdown_pct=countdown_pct,
        _countdown_emptying=countdown_emptying,
    )


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
        self._pending_spawns: dict = {}
        self._loading_frame: int = 0
        self._loading_anim_timer: Any = None
        self._anim_bar_vals: list[float] = [0.0, 0.0, 0.0]
        self._anim_bar_targets: list[float] = [0.0, 0.0, 0.0]
        # Countdown arc animation state (0-100 percentage).
        self._anim_arc_val: float = 0.0
        self._anim_arc_target: float = 0.0
        self._anim_arc_emptying: bool = False
        self._loading_phase: str = "loading"
        self._data_pending: bool = False

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

        # Animate "Loading" spinner until the first prediction arrives
        self._loading_anim_timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            0.1, self, "_loadingAnimTick:", None, True
        )

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
                # Show cached data immediately — force-fetch only on first open
                # (fetch_live_status spawns claude which counts against session budget).
                # Subsequent opens do a non-force fetch: uses cached Claude status but
                # re-runs `bd ready` so newly-added tasks appear without waiting for
                # the 5-minute background timer.
                self._vc.updateWithData_({
                    "prediction": self._prediction,
                    "state": self.state,
                    "ready_tasks": self._all_ready_tasks,
                    "fetched_at": self._last_fetch_at,
                    "update_check": self.state.get("update_check"),
                })
                self._worker.fetch(force=(self._prediction is None))
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

    # Bar animation constants
    _CAL_BAR_TICKS = 20            # Sequential bar calibration: 2.0s total
    _CAL_CLOCK_TICKS = 20          # Clock 360° sweep: 2.0s
    # Total cycle: 40 ticks = 4.0s

    @objc.python_method
    def _tick_loading_bars(self, btn: Any, mode: str) -> None:
        """Meter-calibration loading: bars sweep sequentially, then arc sweeps 0→100→0."""
        n_bars = len(self._anim_bar_vals)
        total_ticks = self._CAL_BAR_TICKS + self._CAL_CLOCK_TICKS
        frame = self._loading_frame % total_ticks

        # Check if data arrived and we're at a cycle boundary
        if self._data_pending and self._loading_frame > 0 and frame == 0:
            self._start_final_cycle()
            self._tick_final_bars(btn)
            return

        # Reset all bars to 0 each tick, then set the active one
        for i in range(n_bars):
            self._anim_bar_vals[i] = 0.0
            self._anim_bar_targets[i] = 0.0

        if frame < self._CAL_BAR_TICKS:
            # Bar calibration phase: sequential, one bar at a time
            ticks_per_bar = self._CAL_BAR_TICKS / n_bars
            bar_idx = min(int(frame / ticks_per_bar), n_bars - 1)
            local_t = (frame - bar_idx * ticks_per_bar) / ticks_per_bar
            # Triangle wave: 0→100→0 over one bar period, with easing
            if local_t < 0.5:
                pct = _ease_out_cubic(local_t / 0.5) * 100.0
            else:
                pct = (1.0 - _ease_in_cubic((local_t - 0.5) / 0.5)) * 100.0
            self._anim_bar_vals[bar_idx] = pct
            self._anim_bar_targets[bar_idx] = pct
            # Arc stays empty during bar phase
            self._anim_arc_val = 0.0
        else:
            # Arc sweep phase: triangle wave 0→100→0, with easing
            clk_frame = frame - self._CAL_BAR_TICKS
            t = clk_frame / self._CAL_CLOCK_TICKS
            if t < 0.5:
                self._anim_arc_val = _ease_out_cubic(t / 0.5) * 100.0
            else:
                self._anim_arc_val = (1.0 - _ease_in_cubic((t - 0.5) / 0.5)) * 100.0
            self._anim_arc_emptying = t >= 0.5

        self._loading_frame += 1

        self._render_anim_frame(btn)

    @objc.python_method
    def _render_anim_frame(self, btn: Any) -> None:
        """Build an _AnimPred from current anim state and paint onto the button."""
        n_bars = len(self._anim_bar_vals)
        fake = _AnimPred(
            session_pct_all=self._anim_bar_vals[0] if n_bars > 0 else 0.0,
            pct_all=self._anim_bar_vals[1] if n_bars > 1 else 0.0,
            pct_sonnet=self._anim_bar_vals[2] if n_bars > 2 else 0.0,
            session_hours_remaining=0.0,
            countdown_pct=self._anim_arc_val,
            countdown_emptying=self._anim_arc_emptying,
        )
        img = self._make_status_image(fake)
        btn.setImage_(img)
        btn.setImagePosition_(1)  # NSImageOnly
        btn.setTitle_("")

    @objc.python_method
    def _start_final_cycle(self) -> None:
        """Compute targets from prediction and start the final animation cycle."""
        pred = self._prediction
        show_sonnet = bool(self.config.get("menubar", {}).get("show_sonnet", True))
        targets = [pred.session_pct_all, pred.pct_all]
        if show_sonnet:
            targets.append(pred.pct_sonnet)
        self._anim_bar_targets = targets
        # Resize bar vals to match targets
        self._anim_bar_vals = [0.0] * len(targets)

        # Arc target: time-based fill (elapsed fraction of 5-hour session window)
        hrs_rem = getattr(pred, "session_hours_remaining", 0.0)
        self._anim_arc_target = max(0.0, min(100.0, (1.0 - hrs_rem / 5.0) * 100.0))
        self._anim_arc_emptying = False

        self._loading_phase = "final_bars"
        self._loading_frame = 0
        self._data_pending = False

    @objc.python_method
    def _tick_final_bars(self, btn: Any) -> None:
        """Final cycle bar phase: bars sweep sequentially 0→100→target over _CAL_BAR_TICKS ticks."""
        n_bars = len(self._anim_bar_targets)
        frame = self._loading_frame
        ticks_per_bar = self._CAL_BAR_TICKS / n_bars if n_bars > 0 else 1

        for i in range(n_bars):
            bar_start = i * ticks_per_bar
            bar_end = (i + 1) * ticks_per_bar
            if frame < bar_start:
                self._anim_bar_vals[i] = 0.0
            elif frame >= bar_end:
                self._anim_bar_vals[i] = self._anim_bar_targets[i]
            else:
                local_t = (frame - bar_start) / ticks_per_bar
                target = self._anim_bar_targets[i]
                if local_t < 0.5:
                    # Rise 0 → 100 (ease-out: decelerates into peak)
                    self._anim_bar_vals[i] = _ease_out_cubic(local_t / 0.5) * 100.0
                else:
                    # Settle 100 → target (ease-in-out: smooth landing)
                    eased = _ease_in_out_cubic((local_t - 0.5) / 0.5)
                    self._anim_bar_vals[i] = 100.0 + eased * (target - 100.0)

        # Arc stays empty during bar phase
        self._anim_arc_val = 0.0

        self._loading_frame += 1
        if self._loading_frame >= self._CAL_BAR_TICKS:
            # Snap bars to final targets, transition to arc phase
            for i in range(n_bars):
                self._anim_bar_vals[i] = self._anim_bar_targets[i]
            self._loading_phase = "final_clock"
            self._loading_frame = 0

        self._render_anim_frame(btn)

    @objc.python_method
    def _tick_final_clock(self, btn: Any) -> None:
        """Final cycle arc phase: arc sweeps to session time-elapsed target."""
        frame = self._loading_frame
        t = frame / self._CAL_CLOCK_TICKS if self._CAL_CLOCK_TICKS > 0 else 1.0
        t = min(t, 1.0)

        target = self._anim_arc_target
        if t < 0.5:
            # Rise 0 → 100 (ease-out: decelerates into peak)
            self._anim_arc_val = _ease_out_cubic(t / 0.5) * 100.0
        else:
            # Settle 100 → target (ease-in-out: smooth landing)
            eased = _ease_in_out_cubic((t - 0.5) / 0.5)
            self._anim_arc_val = 100.0 + eased * (target - 100.0)
        self._anim_arc_emptying = False

        self._loading_frame += 1
        if self._loading_frame >= self._CAL_CLOCK_TICKS:
            self._loading_phase = "done"
            if self._loading_anim_timer is not None:
                self._loading_anim_timer.invalidate()
                self._loading_anim_timer = None
            self._update_status_title()

        self._render_anim_frame(btn)

    def _loadingAnimTick_(self, timer: Any) -> None:
        """Loading animation: bars sweep sequentially, clock sweeps 360°, then final settle."""
        btn = self._status_item.button()
        if btn is None:
            return

        # ── PHASE: final_bars — bars rise to 100 then settle to real values ──
        if self._loading_phase == "final_bars":
            self._tick_final_bars(btn)
            return

        # ── PHASE: final_clock — clock hands sweep to actual positions ──
        if self._loading_phase == "final_clock":
            self._tick_final_clock(btn)
            return

        # ── PHASE: loading — data just arrived? mark pending ──
        if self._prediction is not None and self._loading_phase == "loading":
            self._data_pending = True

        # ── PHASE: loading — still waiting / looping with data_pending ──
        self._tick_loading_bars(btn, "bars")

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
        if self._vc is not None:
            self._vc.rebuild_plugin_sections()
            # Populate newly-added plugin sections with cached data immediately
            # so users see content without needing to close/reopen the popover.
            self._vc.updateWithData_({
                "prediction": self._prediction,
                "state": self.state,
                "ready_tasks": self._all_ready_tasks,
                "fetched_at": self._last_fetch_at,
                "update_check": self.state.get("update_check"),
            })
        # Trigger a fetch so newly-added project tasks appear immediately
        self._worker.fetch()
        print("[penny] config.yaml reloaded", flush=True)

    @objc.python_method
    def set_plugin_enabled(self, plugin_name: str, enabled: bool) -> None:
        """Enable or disable a plugin by name, writing to config.yaml."""
        plugins_cfg = self.config.setdefault("plugins", {})
        pcfg = plugins_cfg.get(plugin_name, {})
        if isinstance(pcfg, bool):
            pcfg = {}
        pcfg["enabled"] = enabled
        plugins_cfg[plugin_name] = pcfg
        self.config["plugins"] = plugins_cfg
        self._write_config()
        self._hot_reload_config()

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
        bump_state_generation()

        # Notify about available updates (at most once per new version)
        update_check = result.get("update_check", {})
        if update_check and update_check.get("update_available"):
            from .update_checker import mark_notified, should_notify
            if should_notify(state):
                send_notification(
                    "Penny Update",
                    f"Version {update_check['latest_version']} available. Run 'penny update'.",
                )
                mark_notified(state, update_check["latest_version"])
                save_state(state)

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

        # Reconcile recently_completed against the live task list.
        # If a task reappears in `bd ready` it was falsely detected as done
        # (e.g. pane_current_command returning a version-named binary).
        # Remove it from recently_completed so it re-surfaces as ready.
        still_open_ids = {t.task_id for t in all_tasks}
        rc_before = state.get("recently_completed", [])
        rc_after = [a for a in rc_before if a.get("task_id") not in still_open_ids]
        if len(rc_after) != len(rc_before):
            state["recently_completed"] = rc_after
            save_state(state)
            print(
                f"[penny] reconciled recently_completed: removed "
                f"{len(rc_before) - len(rc_after)} false-completed task(s)",
                flush=True,
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
            "update_check": state.get("update_check"),
        })

        # Auto-spawn if trigger conditions met
        if pred and should_trigger(pred, self.config):
            self._spawn_agents()

    def runUpdate_(self, sender: Any) -> None:
        """Open a Terminal window to run `penny update`."""
        subprocess.Popen([
            "osascript", "-e",
            'tell application "Terminal" to do script "penny update"',
        ])
        self._popover.performClose_(sender)

    @objc.python_method
    def _dismiss_update(self) -> None:
        """Dismiss the current update banner."""
        from .update_checker import dismiss_version
        uc = self.state.get("update_check", {})
        latest = uc.get("latest_version", "")
        if latest:
            dismiss_version(self.state, latest)
            save_state(self.state)
            # Re-push data to VC to hide the banner
            self._vc.updateWithData_({
                "prediction": self._prediction,
                "state": self.state,
                "ready_tasks": self._all_ready_tasks,
                "fetched_at": self._last_fetch_at,
                "update_check": self.state.get("update_check"),
            })

    def dismissUpdate_(self, sender: Any) -> None:
        self._dismiss_update()

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
    def _make_status_image(self, pred: Any) -> Any:
        """Draw monochromatic bars + 5-hour session clock as a template NSImage.

        The clock treats one full 360° sweep as 5 hours (one session window).
        A filled wedge grows clockwise from 12 o'clock to show how much
        session time has elapsed (time-based, not usage-based).  All elements are drawn in black; the image
        is marked as a template so macOS renders it in the standard menu bar
        label color, adapting automatically to light/dark mode.
        """
        from AppKit import NSBezierPath, NSColor, NSImage
        mb = self.config.get("menubar", {})
        show_sonnet = bool(mb.get("show_sonnet", True))

        pcts = [pred.session_pct_all, pred.pct_all]
        if show_sonnet:
            pcts.append(pred.pct_sonnet)

        n_bars = len(pcts)
        bar_w = 5.0
        bar_h_max = 16.0
        gap_v = 3.0
        bars_w = n_bars * bar_w + (n_bars - 1) * gap_v
        img_h = bar_h_max

        clk_gap = 4.0
        clk_size = img_h   # square, same height as the bars
        img_w = bars_w + clk_gap + clk_size

        # Monochromatic: black fill, macOS templates handle the rest.
        fill = NSColor.blackColor()
        dim = NSColor.blackColor().colorWithAlphaComponent_(0.25)

        img = NSImage.alloc().initWithSize_((img_w, img_h))
        img.lockFocus()
        NSColor.clearColor().setFill()
        NSBezierPath.fillRect_(((0, 0), (img_w, img_h)))

        # ── Vertical bars ─────────────────────────────────────────────────
        for i, pct in enumerate(pcts):
            x = i * (bar_w + gap_v)
            bar_r = 2.0

            # Track — full height, dim
            dim.setFill()
            NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                ((x, 0), (bar_w, img_h)), bar_r, bar_r
            ).fill()

            # Fill — rises from bottom
            fill_h = (pct / 100.0) * img_h
            if fill_h > 0:
                r = min(bar_r, fill_h / 2.0)
                fill.setFill()
                NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                    ((x, 0), (bar_w, fill_h)), r, r
                ).fill()

        # ── 5-hour session clock ──────────────────────────────────────────
        cx = bars_w + clk_gap + clk_size / 2
        cy = img_h / 2
        r = clk_size / 2 - 1.0   # 1 pt margin

        # Dim filled circle (track)
        dim.setFill()
        NSBezierPath.bezierPathWithOvalInRect_(
            ((cx - r, cy - r), (r * 2, r * 2))
        ).fill()

        # Determine fill: _countdown_pct (animation) or time-based from hours_remaining.
        _cd_pct = getattr(pred, "_countdown_pct", None)
        if _cd_pct is not None:
            used_pct = _cd_pct
        else:
            hrs_rem = getattr(pred, "session_hours_remaining", 0.0)
            used_pct = max(0.0, min(100.0, (1.0 - hrs_rem / 5.0) * 100.0))
        sweep_deg = min(used_pct, 100.0) / 100.0 * 360.0
        emptying = getattr(pred, "_countdown_emptying", False)

        if sweep_deg >= 1.0:
            if emptying:
                # Emptying: trailing edge sweeps clockwise from 12 o'clock,
                # filled region stays anchored at the leading edge.
                start = 90.0 - (360.0 - sweep_deg)
                end = 90.0 - 360.0
            else:
                # Filling: wedge grows clockwise from 12 o'clock.
                start = 90.0
                end = 90.0 - sweep_deg
            wedge = NSBezierPath.bezierPath()
            wedge.moveToPoint_((cx, cy))
            wedge.appendBezierPathWithArcWithCenter_radius_startAngle_endAngle_clockwise_(
                (cx, cy), r, start, end, True
            )
            wedge.closePath()
            fill.setFill()
            wedge.fill()

        img.unlockFocus()
        img.setTemplate_(True)
        return img

    @objc.python_method
    def _format_menubar_title(self, pred: Any, n_running: int) -> str:
        """Return title string for the menubar (bars mode only)."""
        if pred is None:
            return f"\u2728{n_running}" if n_running > 0 else "Loading\u2026"
        return f" \u2728{n_running}" if n_running > 0 else ""

    @objc.python_method
    def _update_status_title(self) -> None:
        # Don't clobber a running animation — timer calls us back when done
        if self._loading_phase in ("loading", "final_bars", "final_clock"):
            return

        pred = self._prediction
        n_running = len(self.state.get("agents_running", []))
        btn = self._status_item.button()
        if btn is None:
            return

        title = self._format_menubar_title(pred, n_running)

        # Always render bars image when prediction exists
        if pred:
            img = self._make_status_image(pred)
            btn.setImage_(img)
            show_title = n_running > 0
            btn.setImagePosition_(2 if show_title else 1)   # NSImageLeft / NSImageOnly
        else:
            btn.setImage_(None)
            btn.setImagePosition_(0)   # NSNoImage

        btn.setTitle_(title or "")

        # Tooltip — shown automatically after ~0.8s hover; no Accessibility permission needed
        if pred:
            pct_s = int(round(pred.session_pct_all))
            pct_all = int(round(pred.pct_all))
            pct_son = int(round(pred.pct_sonnet))
            reset_time = self._compact_reset_time(pred.session_reset_label)
            reset_label = f"resets {reset_time}" if reset_time else "—"
            btn.setToolTip_(
                f"Session: {pct_s}%  ({reset_label})\n"
                f"Weekly all models: {pct_all}%\n"
                f"Weekly Sonnet: {pct_son}%"
            )

    # ── Task/agent actions ────────────────────────────────────────────────

    def spawnTask_(self, task: Any) -> None:
        if self.config.get("work", {}).get("agent_permissions") == "off":
            print(f"[penny] agent_permissions=off — skipping spawn of {task.task_id}", flush=True)
            return

        # Optimistic main-thread state update before background thread starts
        self._all_ready_tasks = [t for t in self._all_ready_tasks if t.task_id != task.task_id]
        self._pending_spawns[task.task_id] = task

        self._update_status_title()
        self._vc.updateWithData_({
            "prediction": self._prediction,
            "state": self.state,
            "ready_tasks": self._all_ready_tasks,
            "fetched_at": self._last_fetch_at,
        })

        task_id = task.task_id
        config_snapshot = dict(self.config)

        def _bg() -> None:
            try:
                desc = self._plugin_mgr.get_task_description(task)
                prompt_tmpl = self._plugin_mgr.get_agent_prompt_template(task)
                record = spawn_claude_agent(
                    task, desc, interactive=True,
                    prompt_template=prompt_tmpl, config=config_snapshot,
                )
                payload: dict = {"task_id": task_id, "record": record, "error": None}
            except Exception as exc:
                print(f"[penny] spawn error for {task_id}: {exc}", flush=True)
                payload = {"task_id": task_id, "record": None, "error": str(exc)}
            self.performSelectorOnMainThread_withObject_waitUntilDone_(
                "_finishSpawn:", payload, False
            )

        threading.Thread(target=_bg, daemon=True).start()

    def _finishSpawn_(self, payload: dict) -> None:
        """Main-thread callback when background spawn completes or fails."""
        task_id = payload["task_id"]
        task = self._pending_spawns.pop(task_id, None)

        if payload.get("error") or payload.get("record") is None:
            print(f"[penny] spawn failed for {task_id}: {payload.get('error')}", flush=True)
            self._worker.fetch()
            return

        record = payload["record"]
        self.state.setdefault("agents_running", []).append(record)
        if task:
            self._plugin_mgr.notify_agent_spawned(task, record, self.state)
        save_state(self.state)
        self._update_status_title()
        if self.config.get("notifications", {}).get("spawn", True):
            title = task.title if task else task_id
            project = task.project_name if task else ""
            send_notification("Penny", f"Starting agent \u2014 {task_id}: {title} ({project})")
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
        # Use stored tmux_bin (full path) from agent record; fall back to PATH lookup
        tmux_bin = agent.get("tmux_bin") or shutil.which("tmux") or "tmux"
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

        # KeepAlive and RunAtLoad are evaluated by launchd at load time only.
        # Writing the plist is sufficient; changes take effect on next launch.
        # Do NOT bootout/bootstrap here — that would kill the running process.

    def toggleKeepAlive_(self, sender: Any) -> None:
        self.config.setdefault("service", {})["keep_alive"] = bool(sender.state())
        self._write_config()
        self._sync_launchd_service()

    def toggleLaunchAtLogin_(self, sender: Any) -> None:
        self.config.setdefault("service", {})["launch_at_login"] = bool(sender.state())
        self._write_config()
        self._sync_launchd_service()

    @objc.python_method
    def set_menubar_mode(self, mode: str) -> None:
        """Set the menubar display mode, persist to config.yaml, and refresh."""
        self.config.setdefault("menubar", {})["mode"] = mode
        self._write_config()
        self._update_status_title()

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
        svc = (self.config or {}).get("service", {})
        keep_alive = bool(svc.get("keep_alive", True))

        if keep_alive:
            # Keep Alive is on — "Quit" means reboot: kill this instance and
            # have launchd start a fresh one so the menubar icon reappears.
            # launchctl kickstart -k stops the running job then immediately
            # restarts it, which is the equivalent of a controlled reboot.
            #
            # start_new_session detaches the child into its own process group
            # so launchd does not kill it when it cleans up our service exit.
            uid = os.getuid()
            subprocess.Popen(
                ["launchctl", "kickstart", "-k", f"gui/{uid}/{PLIST_LABEL}"],
                close_fds=True,
                start_new_session=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            # terminate_() cleans up AppKit state; kickstart -k will SIGKILL
            # us momentarily if terminate_ hasn't exited by then — that's fine.
            NSApplication.sharedApplication().terminate_(sender)
        else:
            # Keep Alive is off — plain quit, launchd will not restart.
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
            updated = run_onboarding(CONFIG_PATH, config, plugin_manager=self._plugin_mgr)
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
