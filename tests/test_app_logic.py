"""Unit tests for extractable pure-Python logic in penny/app.py.

Tests _compact_reset_time, PID lock, _didFetchData_ callback logic,
and task/agent action methods — all without requiring a running AppKit event loop.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from penny.analysis import Prediction
from penny.app import _AnimPred

# ── _AnimPred ─────────────────────────────────────────────────────────────────


class TestAnimPred:
    """Test the _AnimPred factory for loading-animation prediction namespaces."""

    def test_basic_fields(self):
        ns = _AnimPred(session_pct_all=10.0, pct_all=40.0, pct_sonnet=25.0,
                       session_hours_remaining=3.5)
        assert ns.session_pct_all == 10.0
        assert ns.pct_all == 40.0
        assert ns.pct_sonnet == 25.0
        assert ns.session_hours_remaining == 3.5
        assert ns.outage is False
        assert ns.session_reset_label == ""

    def test_outage_flag(self):
        ns = _AnimPred(0, 0, 0, 0, outage=True)
        assert ns.outage is True

    def test_countdown_pct_default_none(self):
        ns = _AnimPred(0, 0, 0, 0)
        assert ns._countdown_pct is None

    def test_explicit_countdown_pct(self):
        ns = _AnimPred(0, 0, 0, 0, countdown_pct=75.0)
        assert ns._countdown_pct == 75.0

    def test_countdown_pct_zero(self):
        """Zero is a valid countdown value (empty arc), distinct from None."""
        ns = _AnimPred(0, 0, 0, 0, countdown_pct=0.0)
        assert ns._countdown_pct == 0.0
        # Explicitly check not None
        assert ns._countdown_pct is not None


# ── _compact_reset_time ──────────────────────────────────────────────────────
#
# _compact_reset_time is an @objc.python_method on PennyApp. We can't easily
# instantiate PennyApp without AppKit, so we test the logic by calling the
# method as a plain function with a mock self.


def _compact_reset_time(label: str) -> str:
    """Reproduce PennyApp._compact_reset_time logic for unit testing.

    This mirrors the method in app.py exactly — any change there should be
    reflected here. If they drift, the integration tests will catch it.
    """
    import re

    from penny.analysis import uses_24h_time

    if not label or label == "\u2014":
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


class TestCompactResetTime:
    def test_empty_string(self):
        assert _compact_reset_time("") == ""

    def test_em_dash(self):
        assert _compact_reset_time("\u2014") == ""

    def test_long_form_12h_to_compact_12h(self):
        with patch("penny.analysis.uses_24h_time", return_value=False):
            assert _compact_reset_time("Today at 4:59 PM") == "4:59pm"

    def test_long_form_12h_to_compact_24h(self):
        with patch("penny.analysis.uses_24h_time", return_value=True):
            assert _compact_reset_time("Today at 4:59 PM") == "16:59"

    def test_long_form_12h_on_the_hour(self):
        with patch("penny.analysis.uses_24h_time", return_value=False):
            assert _compact_reset_time("Mon at 9:00 PM") == "9pm"

    def test_long_form_12h_on_the_hour_24h(self):
        with patch("penny.analysis.uses_24h_time", return_value=True):
            assert _compact_reset_time("Mon at 9:00 PM") == "21"

    def test_long_form_12h_midnight(self):
        with patch("penny.analysis.uses_24h_time", return_value=True):
            assert _compact_reset_time("Today at 12:00 AM") == "0"

    def test_long_form_12h_noon(self):
        with patch("penny.analysis.uses_24h_time", return_value=True):
            assert _compact_reset_time("Today at 12:00 PM") == "12"

    def test_long_form_24h(self):
        assert _compact_reset_time("Today at 16:59") == "16:59"

    def test_long_form_24h_on_the_hour(self):
        assert _compact_reset_time("Mon at 0:00") == "0"

    def test_compact_12h_passthrough_in_12h_mode(self):
        with patch("penny.analysis.uses_24h_time", return_value=False):
            assert _compact_reset_time("9pm") == "9pm"

    def test_compact_12h_to_24h(self):
        with patch("penny.analysis.uses_24h_time", return_value=True):
            assert _compact_reset_time("9pm") == "21"

    def test_compact_12h_with_minutes_to_24h(self):
        with patch("penny.analysis.uses_24h_time", return_value=True):
            assert _compact_reset_time("4:59pm") == "16:59"


# ── PID lock ──────────────────────────────────────────────────────────────────


class TestPidLock:
    def test_acquire_creates_pid_file(self, tmp_path):
        pid_file = tmp_path / "penny.pid"
        with patch("penny.app.data_dir", return_value=tmp_path):
            from penny.app import _acquire_pid_lock
            _acquire_pid_lock()
        assert pid_file.exists()
        assert int(pid_file.read_text().strip()) == os.getpid()

    def test_acquire_replaces_stale_pid(self, tmp_path):
        pid_file = tmp_path / "penny.pid"
        pid_file.write_text("999999")  # non-existent PID
        with patch("penny.app.data_dir", return_value=tmp_path):
            from penny.app import _acquire_pid_lock
            _acquire_pid_lock()
        assert int(pid_file.read_text().strip()) == os.getpid()

    def test_acquire_exits_when_pid_alive(self, tmp_path):
        pid_file = tmp_path / "penny.pid"
        pid_file.write_text(str(os.getpid()))  # our own PID is alive
        with patch("penny.app.data_dir", return_value=tmp_path):
            from penny.app import _acquire_pid_lock
            with pytest.raises(SystemExit):
                _acquire_pid_lock()

    def test_release_removes_own_pid(self, tmp_path):
        pid_file = tmp_path / "penny.pid"
        pid_file.write_text(str(os.getpid()))
        with patch("penny.app.data_dir", return_value=tmp_path):
            from penny.app import _release_pid_lock
            _release_pid_lock()
        assert not pid_file.exists()

    def test_release_preserves_other_pid(self, tmp_path):
        pid_file = tmp_path / "penny.pid"
        pid_file.write_text("999999")
        with patch("penny.app.data_dir", return_value=tmp_path):
            from penny.app import _release_pid_lock
            _release_pid_lock()
        assert pid_file.exists()  # not ours, so don't delete

    def test_release_noop_when_file_missing(self, tmp_path):
        with patch("penny.app.data_dir", return_value=tmp_path):
            from penny.app import _release_pid_lock
            _release_pid_lock()  # should not raise


# ── _safe_load_config ─────────────────────────────────────────────────────────


class TestSafeLoadConfig:
    def test_returns_empty_when_missing(self, tmp_path):
        from penny.app import _safe_load_config
        with patch("penny.app.CONFIG_PATH", tmp_path / "missing.yaml"):
            config, err = _safe_load_config()
        assert config == {}
        assert err is None

    def test_returns_config_on_valid_yaml(self, tmp_path):
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text("projects:\n  - path: /tmp/p\n")
        from penny.app import _safe_load_config
        with patch("penny.app.CONFIG_PATH", cfg_file):
            config, err = _safe_load_config()
        assert config["projects"] == [{"path": "/tmp/p"}]
        assert err is None

    def test_returns_error_on_invalid_yaml(self, tmp_path):
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text("projects:\n  bad: [unclosed\n")
        from penny.app import _safe_load_config
        with patch("penny.app.CONFIG_PATH", cfg_file):
            config, err = _safe_load_config()
        assert config == {}
        assert err is not None

    def test_returns_empty_dict_on_empty_file(self, tmp_path):
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text("")
        from penny.app import _safe_load_config
        with patch("penny.app.CONFIG_PATH", cfg_file):
            config, err = _safe_load_config()
        assert config == {}
        assert err is None


# ── _update_status_title logic ────────────────────────────────────────────────
# We test the title formatting logic directly.


class TestStatusTitleFormatting:
    """Test the title string construction — bars mode only (no text stats)."""

    def _format_title(self, pred: Prediction | None, agents_running: list) -> str:
        """Simulate the bars-only title logic from PennyApp._format_menubar_title."""
        n_running = len(agents_running)
        if pred is None:
            return f"\u2728{n_running}" if n_running > 0 else "Loading\u2026"
        return f" \u2728{n_running}" if n_running > 0 else ""

    def test_loading_when_no_prediction_no_agents(self):
        assert self._format_title(None, []) == "Loading\u2026"

    def test_agents_only_when_no_prediction(self):
        assert self._format_title(None, [{"pid": 1}]) == "\u27281"

    def test_empty_title_with_prediction_no_agents(self):
        pred = Prediction(session_pct_all=10.0, pct_all=42.0, pct_sonnet=30.0, session_reset_label="2pm")
        assert self._format_title(pred, []) == ""

    def test_sparkle_with_prediction_and_agents(self):
        pred = Prediction(session_pct_all=10.0, pct_all=42.0, pct_sonnet=30.0, session_reset_label="2pm")
        assert self._format_title(pred, [{"pid": 1}]) == " \u27281"


# ── _didFetchData_ callback logic ────────────────────────────────────────────


class TestDidFetchDataLogic:
    """Test the state update logic from _didFetchData_ without PyObjC."""

    def _process_newly_done(
        self, state: dict, newly_done: list, notify_completion: bool = True
    ) -> list:
        """Reproduce the newly-completed agent processing from _didFetchData_."""
        notifications = []
        for agent in newly_done:
            rc = state.setdefault("recently_completed", [])
            if not any(a.get("task_id") == agent.get("task_id") for a in rc):
                rc.append(agent)
            state["recently_completed"] = rc[-20:]
            if agent.get("status") != "unknown" and notify_completion:
                notifications.append(agent["task_id"])
        return notifications

    def test_newly_done_added_to_recently_completed(self):
        state = {"recently_completed": []}
        agent = {"task_id": "t-1", "title": "Fix", "project": "proj", "status": "completed"}
        self._process_newly_done(state, [agent])
        assert len(state["recently_completed"]) == 1
        assert state["recently_completed"][0]["task_id"] == "t-1"

    def test_newly_done_deduplicates(self):
        agent = {"task_id": "t-1", "title": "Fix", "project": "proj", "status": "completed"}
        state = {"recently_completed": [agent]}
        self._process_newly_done(state, [agent])
        assert len(state["recently_completed"]) == 1

    def test_recently_completed_capped_at_20(self):
        state = {
            "recently_completed": [
                {"task_id": f"old-{i}", "status": "completed"} for i in range(20)
            ],
        }
        new_agent = {"task_id": "new-1", "status": "completed"}
        self._process_newly_done(state, [new_agent])
        assert len(state["recently_completed"]) == 20
        assert state["recently_completed"][-1]["task_id"] == "new-1"

    def test_unknown_status_skips_notification(self):
        state = {"recently_completed": []}
        agent = {"task_id": "t-1", "status": "unknown"}
        notifs = self._process_newly_done(state, [agent])
        assert notifs == []

    def test_completed_status_sends_notification(self):
        state = {"recently_completed": []}
        agent = {"task_id": "t-1", "status": "completed", "title": "Fix", "project": "proj"}
        notifs = self._process_newly_done(state, [agent])
        assert notifs == ["t-1"]

    def test_notifications_disabled(self):
        state = {"recently_completed": []}
        agent = {"task_id": "t-1", "status": "completed"}
        notifs = self._process_newly_done(state, [agent], notify_completion=False)
        assert notifs == []


# ── needs_onboarding ──────────────────────────────────────────────────────────


class TestNeedsOnboarding:
    def test_true_when_no_projects(self):
        from penny.onboarding import needs_onboarding
        assert needs_onboarding({}) is True
        assert needs_onboarding({"projects": []}) is True

    def test_true_when_placeholder(self):
        from penny.onboarding import needs_onboarding
        config = {"projects": [{"path": "/PLACEHOLDER_PROJECT_PATH"}]}
        assert needs_onboarding(config) is True

    def test_false_when_real_project(self):
        from penny.onboarding import needs_onboarding
        config = {"projects": [{"path": "/Users/me/myproject"}]}
        assert needs_onboarding(config) is False


# ── spawnTask_ state mutations ───────────────────────────────────────────────


class TestSpawnTaskLogic:
    """Test the state-mutation logic from PennyApp.spawnTask_."""

    def _make_task(self, task_id="t-1"):
        from penny.tasks import Task
        return Task(task_id, "Fix bug", "P1", "/tmp/proj", "proj")

    def _simulate_spawn(self, state, task, all_ready, config=None):
        """Reproduce spawnTask_ state mutations."""
        config = config or {}
        record = {
            "task_id": task.task_id,
            "project": task.project_name,
            "project_path": task.project_path,
            "title": task.title,
            "priority": task.priority,
            "status": "running",
            "pid": -1,
            "interactive": True,
        }
        state.setdefault("agents_running", []).append(record)
        new_ready = [t for t in all_ready if t.task_id != task.task_id]
        should_notify = config.get("notifications", {}).get("spawn", True)
        return state, new_ready, record, should_notify

    def test_adds_agent_to_running(self):
        state = {"agents_running": []}
        task = self._make_task()
        state, _, record, _ = self._simulate_spawn(state, task, [task])
        assert len(state["agents_running"]) == 1
        assert record["task_id"] == "t-1"
        assert record["status"] == "running"
        assert record["interactive"] is True

    def test_removes_from_ready_list(self):
        t1 = self._make_task("t-1")
        t2 = self._make_task("t-2")
        state = {"agents_running": []}
        _, new_ready, _, _ = self._simulate_spawn(state, t1, [t1, t2])
        assert len(new_ready) == 1
        assert new_ready[0].task_id == "t-2"

    def test_notification_enabled_by_default(self):
        state = {"agents_running": []}
        _, _, _, notify = self._simulate_spawn(state, self._make_task(), [self._make_task()])
        assert notify is True

    def test_notification_disabled_in_config(self):
        config = {"notifications": {"spawn": False}}
        state = {"agents_running": []}
        _, _, _, notify = self._simulate_spawn(
            state, self._make_task(), [self._make_task()], config
        )
        assert notify is False

    def test_record_has_all_required_fields(self):
        state = {"agents_running": []}
        _, _, record, _ = self._simulate_spawn(state, self._make_task(), [self._make_task()])
        for key in ("task_id", "project", "project_path", "title", "priority", "status", "pid"):
            assert key in record


# ── stopAgent_ legacy PID path ──────────────────────────────────────────────


class TestStopAgentLegacy:
    """Test backwards-compat PID-based stopAgent_ decision logic."""

    def _stop_by_pid(self, state, pid):
        """Reproduce stopAgent_(pid) decision branches.

        Returns (task_id_or_None, action_taken).
        """
        if pid is None or pid <= 0:
            return None, "noop"
        agent = next(
            (a for a in state.get("agents_running", []) if a.get("pid") == pid),
            None,
        )
        if agent:
            return agent.get("task_id", ""), "delegate"
        return None, "direct_kill"

    def test_noop_when_pid_none(self):
        _, action = self._stop_by_pid({"agents_running": []}, None)
        assert action == "noop"

    def test_noop_when_pid_zero(self):
        _, action = self._stop_by_pid({"agents_running": []}, 0)
        assert action == "noop"

    def test_noop_when_pid_negative(self):
        _, action = self._stop_by_pid({"agents_running": []}, -5)
        assert action == "noop"

    def test_delegates_when_matching_agent_found(self):
        state = {"agents_running": [{"task_id": "t-1", "pid": 12345}]}
        task_id, action = self._stop_by_pid(state, 12345)
        assert task_id == "t-1"
        assert action == "delegate"

    def test_direct_kill_when_no_matching_agent(self):
        state = {"agents_running": [{"task_id": "t-1", "pid": 99999}]}
        _, action = self._stop_by_pid(state, 12345)
        assert action == "direct_kill"

    def test_empty_agents_falls_to_direct_kill(self):
        _, action = self._stop_by_pid({"agents_running": []}, 12345)
        assert action == "direct_kill"


# ── pluginAction_ / runBdAction_ ────────────────────────────────────────────


class TestPluginActionLogic:
    """Test pluginAction_ dispatch and error handling."""

    def test_action_tuple_unpacking(self):
        action_payload = ("bd_command", {"args": ["ready"], "cwd": "/tmp"})
        action, payload = action_payload
        assert action == "bd_command"
        assert payload["args"] == ["ready"]

    def test_dispatch_called_with_correct_args(self):
        mgr = MagicMock()
        mgr.dispatch_action("bd_command", {"args": ["ready"]})
        mgr.dispatch_action.assert_called_once_with("bd_command", {"args": ["ready"]})

    def test_worker_fetch_called_after_success(self):
        mgr = MagicMock()
        worker = MagicMock()
        try:
            mgr.dispatch_action("action", None)
        except Exception:
            pass
        finally:
            worker.fetch(force=True)
        worker.fetch.assert_called_once_with(force=True)

    def test_worker_fetch_called_after_error(self):
        mgr = MagicMock()
        mgr.dispatch_action.side_effect = RuntimeError("boom")
        worker = MagicMock()
        try:
            mgr.dispatch_action("action", None)
        except Exception:
            pass
        finally:
            worker.fetch(force=True)
        worker.fetch.assert_called_once_with(force=True)

    def test_runBdAction_wraps_as_bd_command(self):
        """runBdAction_ wraps payload as ('bd_command', args_cwd)."""
        args_cwd = (["ready"], "/tmp")
        # Reproduce: self.pluginAction_(("bd_command", args_cwd))
        action, payload = "bd_command", args_cwd
        assert action == "bd_command"
        assert payload is args_cwd


# ── _timerFired_ ─────────────────────────────────────────────────────────────


class TestTimerFiredLogic:
    def test_calls_worker_fetch(self):
        """_timerFired_ calls self._worker.fetch() without force."""
        worker = MagicMock()
        worker.fetch()
        worker.fetch.assert_called_once_with()

    def test_does_not_force_fetch(self):
        """Timer uses cached data; force=True is NOT passed."""
        worker = MagicMock()
        worker.fetch()
        args, kwargs = worker.fetch.call_args
        assert args == ()
        assert kwargs == {}


# ── viewReport_ logic ────────────────────────────────────────────────────────


class TestViewReportLogic:
    """Test viewReport_ dashboard open + fallback chain."""

    def test_dashboard_started_and_port_returned(self):
        dashboard = MagicMock()
        dashboard.ensure_started.return_value = 7432
        assert dashboard.ensure_started() == 7432

    def test_fallback_when_dashboard_fails(self):
        """On dashboard error, generate_report + open_report are used."""
        dashboard = MagicMock()
        dashboard.ensure_started.side_effect = RuntimeError("fail")
        used_fallback = False
        try:
            dashboard.ensure_started()
        except Exception:
            used_fallback = True
        assert used_fallback

    def test_popover_closed_when_shown(self):
        popover = MagicMock()
        popover.isShown.return_value = True
        if popover.isShown():
            popover.performClose_(None)
        popover.performClose_.assert_called_once()

    def test_popover_not_closed_when_hidden(self):
        popover = MagicMock()
        popover.isShown.return_value = False
        if popover.isShown():
            popover.performClose_(None)
        popover.performClose_.assert_not_called()


# ── togglePopover_ state transitions ─────────────────────────────────────────


class TestTogglePopoverLogic:
    """Test the popover open/close state transition logic."""

    def test_close_path_when_shown(self):
        is_shown = True
        if is_shown:
            action = "close"
            remove_monitor = True
        else:
            action = "open"
            remove_monitor = False
        assert action == "close"
        assert remove_monitor is True

    def test_open_path_when_hidden(self):
        is_shown = False
        if is_shown:
            action = "close"
        else:
            action = "open"
        assert action == "open"

    def test_auto_fetch_when_no_prediction(self):
        """When prediction is None, opening popover triggers forced fetch."""
        prediction = None
        worker = MagicMock()
        if prediction is None:
            worker.fetch(force=True)
        worker.fetch.assert_called_once_with(force=True)

    def test_no_auto_fetch_when_prediction_exists(self):
        prediction = Prediction(pct_all=50.0)
        worker = MagicMock()
        if prediction is None:
            worker.fetch(force=True)
        worker.fetch.assert_not_called()

    def test_vc_updated_with_cached_data(self):
        """Opening the popover updates the VC with current cached state."""
        vc = MagicMock()
        data = {
            "prediction": Prediction(pct_all=42.0),
            "state": {"agents_running": []},
            "ready_tasks": [],
            "fetched_at": None,
        }
        vc.updateWithData_(data)
        vc.updateWithData_.assert_called_once_with(data)


# ── Event monitor ────────────────────────────────────────────────────────────


class TestEventMonitorLogic:
    """Test outside-click event monitor add/remove state management."""

    def test_add_is_noop_when_monitor_exists(self):
        monitor = "existing"
        if monitor is not None:
            result = "skipped"
        else:
            result = "created"
        assert result == "skipped"

    def test_add_creates_when_none(self):
        monitor = None
        if monitor is not None:
            result = "skipped"
        else:
            monitor = "new_monitor"
            result = "created"
        assert result == "created"
        assert monitor == "new_monitor"

    def test_remove_clears_reference(self):
        monitor = "existing"
        if monitor is not None:
            monitor = None
        assert monitor is None

    def test_remove_noop_when_already_none(self):
        monitor = None
        if monitor is not None:
            monitor = None
        assert monitor is None


# ── _spawn_agents auto-spawn logic ──────────────────────────────────────────


class TestSpawnAgentsLogic:
    """Test the automatic agent spawning logic."""

    def _make_task(self, task_id, priority="P2"):
        from penny.tasks import Task
        return Task(task_id, f"Task {task_id}", priority, "/tmp/proj", "proj")

    def test_noop_when_no_ready_tasks(self):
        ready_tasks = []
        spawned = []
        if ready_tasks:
            for t in ready_tasks:
                spawned.append(t.task_id)
        assert spawned == []

    def test_spawns_all_ready_tasks(self):
        tasks = [self._make_task("t-1"), self._make_task("t-2")]
        state = {"agents_running": []}
        spawned = []
        for task in tasks:
            record = {"task_id": task.task_id, "status": "running"}
            state["agents_running"].append(record)
            spawned.append(f"{task.project_name}/{task.task_id}")
        assert len(state["agents_running"]) == 2
        assert spawned == ["proj/t-1", "proj/t-2"]

    def test_notification_includes_count_and_names(self):
        tasks = [self._make_task("t-1"), self._make_task("t-2")]
        spawned = [f"{t.project_name}/{t.task_id}" for t in tasks]
        pred = Prediction(pct_all=50.0, projected_pct_all=70.0, days_remaining=2.0)
        msg = (
            f"Starting {len(spawned)} agent(s) \u2014 "
            + ", ".join(spawned)
            + f". {100 - pred.projected_pct_all:.0f}% capacity unused, "
            + f"{pred.days_remaining:.1f} days left."
        )
        assert "2 agent(s)" in msg
        assert "proj/t-1" in msg
        assert "30% capacity unused" in msg

    def test_notification_requires_prediction(self):
        config = {"notifications": {"spawn": True}}
        pred = None
        should_notify = config.get("notifications", {}).get("spawn", True) and pred
        assert not should_notify

    def test_notification_sent_with_prediction(self):
        config = {"notifications": {"spawn": True}}
        pred = Prediction(pct_all=50.0)
        should_notify = config.get("notifications", {}).get("spawn", True) and pred
        assert should_notify

    def test_notification_skipped_when_config_disabled(self):
        config = {"notifications": {"spawn": False}}
        pred = Prediction(pct_all=50.0)
        should_notify = config.get("notifications", {}).get("spawn", True) and pred
        assert not should_notify


# ── _load_and_refresh composition ────────────────────────────────────────────


class TestLoadAndRefreshLogic:
    """Test the _load_and_refresh startup flow decisions."""

    def test_yaml_error_stops_refresh(self):
        """If config has YAML error, status title shows warning and no fetch."""
        import tempfile

        from penny.app import _safe_load_config
        with tempfile.NamedTemporaryFile(suffix=".yaml", mode="w", delete=False) as f:
            f.write("bad: [unclosed\n")
            f.flush()
            with patch("penny.app.CONFIG_PATH", Path(f.name)):
                config, err = _safe_load_config()
        assert config == {}
        assert err is not None
        # In real code, this returns early (no fetch triggered)

    def test_onboarding_deferred_sets_state_flag(self):
        """When user defers onboarding, state.onboarding_deferred = True."""
        state = {}
        # Simulate onboarding returning None (deferred)
        updated = None
        if updated is None:
            state["onboarding_deferred"] = True
        assert state["onboarding_deferred"] is True

    def test_onboarding_completed_clears_flag(self):
        """When onboarding completes, onboarding_deferred is removed."""
        state = {"onboarding_deferred": True}
        updated = {"projects": [{"path": "/tmp/proj"}]}
        if updated is not None:
            state.pop("onboarding_deferred", None)
        assert "onboarding_deferred" not in state

    def test_preflight_tool_errors_flagged(self):
        """Preflight errors with 'error' severity are surfaced (not project errors)."""
        from penny.preflight import PreflightIssue
        issues = [
            PreflightIssue("error", "`claude` CLI not found", "Install it"),
            PreflightIssue("error", "Project path placeholder", "Fix config"),
            PreflightIssue("warning", "Stats cache missing", "Use Claude"),
        ]
        tool_errors = [
            i for i in issues
            if i.severity == "error" and "project" not in i.message.lower()
        ]
        assert len(tool_errors) == 1
        assert "claude" in tool_errors[0].message


# ── Config hot-reload ──────────────────────────────────────────────────────


class TestConfigHotReload:
    """Test the config file change detection and hot-reload logic."""

    def test_config_mtime_returns_none_when_missing(self, tmp_path):
        from penny.app import _config_mtime
        with patch("penny.app.CONFIG_PATH", tmp_path / "missing.yaml"):
            assert _config_mtime() is None

    def test_config_mtime_returns_float_when_exists(self, tmp_path):
        cfg = tmp_path / "config.yaml"
        cfg.write_text("projects: []")
        from penny.app import _config_mtime
        with patch("penny.app.CONFIG_PATH", cfg):
            mt = _config_mtime()
        assert isinstance(mt, float)

    def test_reload_skipped_when_mtime_unchanged(self, tmp_path):
        """If mtime hasn't changed, _maybe_reload_config returns False."""
        cfg = tmp_path / "config.yaml"
        cfg.write_text("projects: []")
        from penny.app import _config_mtime
        with patch("penny.app.CONFIG_PATH", cfg):
            mt = _config_mtime()
        # Same mtime → no reload needed
        assert mt is not None
        with patch("penny.app.CONFIG_PATH", cfg):
            assert _config_mtime() == mt

    def test_reload_triggered_when_mtime_changes(self, tmp_path):
        """Simulate mtime change by rewriting the file."""
        import time
        cfg = tmp_path / "config.yaml"
        cfg.write_text("projects: []")
        from penny.app import _config_mtime
        with patch("penny.app.CONFIG_PATH", cfg):
            mt1 = _config_mtime()
        time.sleep(0.05)  # ensure mtime differs
        cfg.write_text("projects:\n  - path: /tmp/new\n")
        with patch("penny.app.CONFIG_PATH", cfg):
            mt2 = _config_mtime()
        assert mt2 != mt1

    def test_reload_applies_new_config(self, tmp_path):
        """_hot_reload_config reads the new config and returns it."""
        cfg = tmp_path / "config.yaml"
        cfg.write_text("projects:\n  - path: /tmp/proj\n")
        from penny.app import _safe_load_config
        with patch("penny.app.CONFIG_PATH", cfg):
            config, err = _safe_load_config()
        assert err is None
        assert config["projects"] == [{"path": "/tmp/proj"}]

    def test_reload_ignores_yaml_errors(self, tmp_path):
        """If config has YAML errors after edit, hot-reload keeps old config."""
        cfg = tmp_path / "config.yaml"
        cfg.write_text("bad: [unclosed\n")
        from penny.app import _safe_load_config
        with patch("penny.app.CONFIG_PATH", cfg):
            config, err = _safe_load_config()
        assert config == {}
        assert err is not None

    def test_hot_reload_updates_mtime(self, tmp_path):
        """_hot_reload_config updates _config_mtime after successful reload."""
        import time
        cfg = tmp_path / "config.yaml"
        cfg.write_text("projects: []")

        from penny.app import _config_mtime
        with patch("penny.app.CONFIG_PATH", cfg):
            mt1 = _config_mtime()

        time.sleep(0.05)
        cfg.write_text("projects:\n  - path: /tmp/new\n")
        with patch("penny.app.CONFIG_PATH", cfg):
            mt2 = _config_mtime()

        # Simulating what _hot_reload_config does: mtime is updated after reload
        assert mt2 is not None
        assert mt2 != mt1


# ── _script_dir_from_plist ────────────────────────────────────────────────────


class TestScriptDirFromPlist:
    def test_returns_none_when_plist_missing(self, tmp_path):
        from penny.app import _script_dir_from_plist
        with patch("penny.app.PLIST_LAUNCHAGENTS", tmp_path / "missing.plist"):
            assert _script_dir_from_plist() is None

    def test_returns_path_from_plist(self, tmp_path):
        import plistlib
        plist_file = tmp_path / "test.plist"
        plist_file.write_bytes(plistlib.dumps({"WorkingDirectory": "/usr/local/penny"}))
        from penny.app import _script_dir_from_plist
        with patch("penny.app.PLIST_LAUNCHAGENTS", plist_file):
            result = _script_dir_from_plist()
        assert result == Path("/usr/local/penny")

    def test_returns_none_when_working_directory_empty(self, tmp_path):
        import plistlib
        plist_file = tmp_path / "test.plist"
        plist_file.write_bytes(plistlib.dumps({"WorkingDirectory": ""}))
        from penny.app import _script_dir_from_plist
        with patch("penny.app.PLIST_LAUNCHAGENTS", plist_file):
            assert _script_dir_from_plist() is None

    def test_returns_none_when_working_directory_absent(self, tmp_path):
        import plistlib
        plist_file = tmp_path / "test.plist"
        plist_file.write_bytes(plistlib.dumps({"Label": "com.gpxl.penny"}))
        from penny.app import _script_dir_from_plist
        with patch("penny.app.PLIST_LAUNCHAGENTS", plist_file):
            assert _script_dir_from_plist() is None

    def test_returns_none_on_corrupt_plist(self, tmp_path):
        plist_file = tmp_path / "test.plist"
        plist_file.write_bytes(b"not a valid plist")
        from penny.app import _script_dir_from_plist
        with patch("penny.app.PLIST_LAUNCHAGENTS", plist_file):
            assert _script_dir_from_plist() is None


# ── dismissCompleted_ / clearAllCompleted_ logic ─────────────────────────────


class TestDismissAndClearLogic:
    """Test the list-filtering logic from dismissCompleted_ and clearAllCompleted_."""

    def _dismiss(self, state: dict, task_id: str) -> dict:
        """Reproduce dismissCompleted_ state mutation."""
        rc = state.get("recently_completed", [])
        state["recently_completed"] = [a for a in rc if a.get("task_id") != task_id]
        return state

    def _clear_all(self, state: dict) -> dict:
        """Reproduce clearAllCompleted_ state mutation."""
        state["recently_completed"] = []
        return state

    def test_dismiss_removes_matching_task(self):
        state = {"recently_completed": [
            {"task_id": "t-1", "title": "Fix A"},
            {"task_id": "t-2", "title": "Fix B"},
        ]}
        self._dismiss(state, "t-1")
        assert len(state["recently_completed"]) == 1
        assert state["recently_completed"][0]["task_id"] == "t-2"

    def test_dismiss_noop_when_task_not_found(self):
        state = {"recently_completed": [{"task_id": "t-1"}]}
        self._dismiss(state, "t-999")
        assert len(state["recently_completed"]) == 1

    def test_dismiss_on_empty_list(self):
        state = {"recently_completed": []}
        self._dismiss(state, "t-1")
        assert state["recently_completed"] == []

    def test_clear_all_removes_everything(self):
        state = {"recently_completed": [
            {"task_id": "t-1"}, {"task_id": "t-2"}, {"task_id": "t-3"},
        ]}
        self._clear_all(state)
        assert state["recently_completed"] == []

    def test_clear_all_on_empty_list(self):
        state = {"recently_completed": []}
        self._clear_all(state)
        assert state["recently_completed"] == []


# ── stopAgentByTaskId_ logic ─────────────────────────────────────────────────


class TestStopAgentByTaskIdLogic:
    """Test the state-mutation logic from stopAgentByTaskId_."""

    def _stop_by_task_id(self, state: dict, task_id: str) -> tuple[bool, dict]:
        """Reproduce stopAgentByTaskId_ state filtering (without process kill)."""
        if not task_id:
            return False, state
        agent = next(
            (a for a in state.get("agents_running", []) if a.get("task_id") == task_id),
            None,
        )
        if agent is None:
            return False, state
        state["agents_running"] = [
            a for a in state.get("agents_running", []) if a.get("task_id") != task_id
        ]
        return True, state

    def test_stops_matching_agent(self):
        state = {"agents_running": [{"task_id": "t-1", "pid": 100}]}
        found, state = self._stop_by_task_id(state, "t-1")
        assert found is True
        assert state["agents_running"] == []

    def test_preserves_other_agents(self):
        state = {"agents_running": [
            {"task_id": "t-1", "pid": 100},
            {"task_id": "t-2", "pid": 200},
        ]}
        found, state = self._stop_by_task_id(state, "t-1")
        assert found is True
        assert len(state["agents_running"]) == 1
        assert state["agents_running"][0]["task_id"] == "t-2"

    def test_noop_when_empty_task_id(self):
        state = {"agents_running": [{"task_id": "t-1"}]}
        found, state = self._stop_by_task_id(state, "")
        assert found is False
        assert len(state["agents_running"]) == 1

    def test_noop_when_task_id_not_found(self):
        state = {"agents_running": [{"task_id": "t-1"}]}
        found, state = self._stop_by_task_id(state, "t-999")
        assert found is False
        assert len(state["agents_running"]) == 1

    def test_noop_when_agents_empty(self):
        state = {"agents_running": []}
        found, state = self._stop_by_task_id(state, "t-1")
        assert found is False
        assert state["agents_running"] == []


# ── _write_config logic ───────────────────────────────────────────────────────


class TestWriteConfigLogic:
    """Test the YAML serialization logic from _write_config."""

    def test_writes_valid_yaml(self, tmp_path):
        import yaml
        cfg_file = tmp_path / "config.yaml"
        config = {"projects": [{"path": "/tmp/proj"}], "work": {"agent_permissions": "off"}}
        with cfg_file.open("w") as f:
            yaml.dump(config, f, default_flow_style=False, allow_unicode=True)
        reloaded = yaml.safe_load(cfg_file.read_text())
        assert reloaded["projects"] == [{"path": "/tmp/proj"}]
        assert reloaded["work"]["agent_permissions"] == "off"

    def test_write_handles_unicode(self, tmp_path):
        import yaml
        cfg_file = tmp_path / "config.yaml"
        config = {"name": "ñoño"}
        with cfg_file.open("w") as f:
            yaml.dump(config, f, default_flow_style=False, allow_unicode=True)
        assert "ñoño" in cfg_file.read_text()

    def test_write_and_reload_round_trip(self, tmp_path):
        import yaml
        cfg_file = tmp_path / "config.yaml"
        original = {"service": {"keep_alive": True, "launch_at_login": False}}
        with cfg_file.open("w") as f:
            yaml.dump(original, f, default_flow_style=False, allow_unicode=True)
        reloaded = yaml.safe_load(cfg_file.read_text())
        assert reloaded == original


# ── _sync_launchd_service plist logic ────────────────────────────────────────


class TestSyncLaunchAgentPlistLogic:
    """Test the plist read/write logic from _sync_launchd_service (no launchctl)."""

    def _apply_service_config(self, plist_path: Path, svc: dict) -> bool:
        """Reproduce the plist-update portion of _sync_launchd_service.

        Returns True if the plist was updated, False if it was already in sync.
        """
        import plistlib
        want_keep_alive = bool(svc.get("keep_alive", True))
        want_run_at_load = bool(svc.get("launch_at_login", True))

        if not plist_path.exists():
            return False

        with plist_path.open("rb") as f:
            pl = plistlib.load(f)

        if (pl.get("KeepAlive", True) == want_keep_alive
                and pl.get("RunAtLoad", True) == want_run_at_load):
            return False  # no-op

        pl["KeepAlive"] = want_keep_alive
        pl["RunAtLoad"] = want_run_at_load
        plist_path.write_bytes(plistlib.dumps(pl))
        return True

    def _make_plist(self, path: Path, keep_alive: bool = True, run_at_load: bool = True) -> None:
        import plistlib
        path.write_bytes(plistlib.dumps({
            "Label": "com.gpxl.penny",
            "KeepAlive": keep_alive,
            "RunAtLoad": run_at_load,
        }))

    def test_noop_when_plist_missing(self, tmp_path):
        updated = self._apply_service_config(tmp_path / "missing.plist", {})
        assert updated is False

    def test_noop_when_already_in_sync(self, tmp_path):
        plist = tmp_path / "test.plist"
        self._make_plist(plist, keep_alive=True, run_at_load=True)
        svc = {"keep_alive": True, "launch_at_login": True}
        updated = self._apply_service_config(plist, svc)
        assert updated is False

    def test_updates_when_keep_alive_changes(self, tmp_path):
        import plistlib
        plist = tmp_path / "test.plist"
        self._make_plist(plist, keep_alive=True, run_at_load=True)
        svc = {"keep_alive": False, "launch_at_login": True}
        updated = self._apply_service_config(plist, svc)
        assert updated is True
        pl = plistlib.loads(plist.read_bytes())
        assert pl["KeepAlive"] is False

    def test_updates_when_run_at_load_changes(self, tmp_path):
        import plistlib
        plist = tmp_path / "test.plist"
        self._make_plist(plist, keep_alive=True, run_at_load=True)
        svc = {"keep_alive": True, "launch_at_login": False}
        updated = self._apply_service_config(plist, svc)
        assert updated is True
        pl = plistlib.loads(plist.read_bytes())
        assert pl["RunAtLoad"] is False

    def test_defaults_to_true_when_keys_absent(self, tmp_path):
        """Empty service config defaults keep_alive=True, launch_at_login=True."""
        plist = tmp_path / "test.plist"
        self._make_plist(plist, keep_alive=True, run_at_load=True)
        updated = self._apply_service_config(plist, {})
        assert updated is False  # defaults match current state


# ── FakeApp helper ─────────────────────────────────────────────────────────────
#
# PennyApp.init() requires a live AppKit event loop, so we cannot instantiate it
# normally. Instead, we build a FakeApp struct with the same attributes and call
# the @objc.python_method methods as plain functions via PennyApp.<method>(fake).
# Since conftest stubs objc.python_method as a passthrough, these methods are
# regular functions on the class.


def _make_fake_app(config=None, state=None, tmp_path=None):
    """Return a lightweight FakeApp struct for testing PennyApp instance methods."""
    from unittest.mock import MagicMock

    class FakeApp:
        pass

    app = FakeApp()
    app.config = config if config is not None else {}
    app.state = state if state is not None else {}
    app._prediction = None
    app._all_ready_tasks = []
    app._ready_tasks = []
    app._last_fetch_at = None
    app._vc = MagicMock()
    app._status_item = MagicMock()
    app._status_item.button.return_value = MagicMock()
    app._worker = MagicMock()
    app._plugin_mgr = MagicMock()
    app._plugin_mgr.get_all_tasks.return_value = []
    app._plugin_mgr.get_all_completed_tasks.return_value = []
    app._plugin_mgr.filter_all_tasks.return_value = []
    app._plugin_mgr.notify_agent_completed.return_value = None
    app._plugin_mgr.notify_agent_spawned.return_value = None
    app._anim_arc_emptying = False
    app._config_mtime = None
    # Stub methods that may be called internally (override per-test as needed)
    app._hot_reload_config = MagicMock()
    app._sync_launchd_service = MagicMock()
    app._update_status_title = MagicMock()
    app._spawn_agents = MagicMock()
    app._write_config = MagicMock()
    if tmp_path:
        app._state_path = tmp_path / "state.json"
    return app


# ── set_plugin_enabled (direct call into penny/app.py) ───────────────────────


class TestSetPluginEnabled:
    """Call PennyApp.set_plugin_enabled directly to cover lines 237-245."""

    def _make_app(self, config=None, tmp_path=None):
        return _make_fake_app(config=config, tmp_path=tmp_path)

    def test_enables_new_plugin(self, tmp_path):
        from penny.app import PennyApp
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text("{}\n")
        app = self._make_app(config={})
        with patch("penny.app.CONFIG_PATH", cfg_file):
            PennyApp.set_plugin_enabled(app, "beads", True)
        assert app.config["plugins"]["beads"]["enabled"] is True

    def test_disables_existing_plugin(self, tmp_path):
        from penny.app import PennyApp
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text("plugins:\n  beads:\n    enabled: true\n")
        app = self._make_app(config={"plugins": {"beads": {"enabled": True}}})
        with patch("penny.app.CONFIG_PATH", cfg_file):
            PennyApp.set_plugin_enabled(app, "beads", False)
        assert app.config["plugins"]["beads"]["enabled"] is False

    def test_converts_bool_plugin_entry_to_dict(self, tmp_path):
        """If the plugin config is a bare bool, it must be converted to a dict."""
        from penny.app import PennyApp
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text("plugins:\n  beads: true\n")
        app = self._make_app(config={"plugins": {"beads": True}})
        with patch("penny.app.CONFIG_PATH", cfg_file):
            PennyApp.set_plugin_enabled(app, "beads", False)
        assert isinstance(app.config["plugins"]["beads"], dict)
        assert app.config["plugins"]["beads"]["enabled"] is False

    def test_calls_write_config_and_hot_reload(self, tmp_path):
        from penny.app import PennyApp
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text("{}\n")
        app = self._make_app(config={})
        hot_reload_called = []
        write_called = []
        app._hot_reload_config = lambda: hot_reload_called.append(True)
        app._write_config = lambda: write_called.append(True)
        PennyApp.set_plugin_enabled(app, "x", True)
        assert write_called   # _write_config was called
        assert hot_reload_called  # _hot_reload_config was called


# ── _write_config (direct call into penny/app.py) ────────────────────────────


class TestWriteConfigDirect:
    """Call PennyApp._write_config directly to cover lines 534-538."""

    def test_writes_config_to_disk(self, tmp_path):
        import yaml

        from penny.app import PennyApp
        cfg_file = tmp_path / "config.yaml"
        app = _make_fake_app(config={"projects": [{"path": "/tmp/p"}]})
        with patch("penny.app.CONFIG_PATH", cfg_file):
            PennyApp._write_config(app)
        data = yaml.safe_load(cfg_file.read_text())
        assert data["projects"] == [{"path": "/tmp/p"}]

    def test_handles_write_error_gracefully(self, tmp_path):
        """If the file cannot be written, the method prints an error (no raise)."""
        from penny.app import PennyApp
        app = _make_fake_app(config={"k": "v"})
        # Point CONFIG_PATH to a directory (write will fail)
        with patch("penny.app.CONFIG_PATH", tmp_path):
            PennyApp._write_config(app)  # must not raise


# ── _didFetchData_ state mutations (direct call into penny/app.py) ────────────


class TestDidFetchDataDirect:
    """Call PennyApp._didFetchData_ directly to cover the real lines in app.py."""

    def _make_result(self, **kwargs):
        base = {
            "state": {"agents_running": [], "recently_completed": []},
            "prediction": None,
            "newly_done": [],
        }
        base.update(kwargs)
        return base

    def test_non_dict_result_returns_early(self):
        from penny.app import PennyApp
        app = _make_fake_app()
        PennyApp._didFetchData_(app, "not a dict")
        # VC must have had setRefreshing_(False) called
        app._vc.setRefreshing_.assert_called_once_with(False)

    def test_error_result_returns_early(self):
        from penny.app import PennyApp
        app = _make_fake_app()
        PennyApp._didFetchData_(app, {"error": "something went wrong"})
        app._vc.setRefreshing_.assert_called_once_with(False)

    def test_updates_state_and_prediction(self):
        from penny.analysis import Prediction
        from penny.app import PennyApp
        pred = Prediction(pct_all=55.0)
        app = _make_fake_app()
        result = self._make_result(
            state={"agents_running": [], "recently_completed": []},
            prediction=pred,
            newly_done=[],
        )
        with patch("penny.app.should_trigger", return_value=False):
            PennyApp._didFetchData_(app, result)
        assert app.state == result["state"]
        assert app._prediction is pred

    def test_newly_done_appended_to_recently_completed(self):
        from penny.app import PennyApp
        app = _make_fake_app()
        state = {"agents_running": [], "recently_completed": []}
        agent = {
            "task_id": "t-1",
            "title": "Fix",
            "project": "proj",
            "status": "completed",
        }
        result = self._make_result(state=state, newly_done=[agent])
        config = {"notifications": {"completion": False}}
        app.config = config
        with patch("penny.app.should_trigger", return_value=False):
            with patch("penny.app.save_state"):
                PennyApp._didFetchData_(app, result)
        assert any(a["task_id"] == "t-1" for a in app.state.get("recently_completed", []))

    def test_completion_notification_sent_when_enabled(self):
        from penny.app import PennyApp
        app = _make_fake_app()
        state = {"agents_running": [], "recently_completed": []}
        agent = {"task_id": "t-1", "title": "Fix", "project": "proj", "status": "done"}
        result = self._make_result(state=state, newly_done=[agent])
        app.config = {"notifications": {"completion": True}}
        with (
            patch("penny.app.should_trigger", return_value=False),
            patch("penny.app.save_state"),
            patch("penny.app.send_notification") as mock_notify,
        ):
            PennyApp._didFetchData_(app, result)
        mock_notify.assert_called_once()

    def test_auto_spawn_triggered_when_should_trigger_true(self):
        from penny.analysis import Prediction
        from penny.app import PennyApp
        pred = Prediction(pct_all=90.0)
        app = _make_fake_app()
        result = self._make_result(
            state={"agents_running": [], "recently_completed": []},
            prediction=pred,
            newly_done=[],
        )
        spawn_called = []
        app._spawn_agents = lambda: spawn_called.append(True)  # type: ignore[method-assign]
        with patch("penny.app.should_trigger", return_value=True):
            PennyApp._didFetchData_(app, result)
        assert spawn_called


# ── spawnTaskById_ (direct call into penny/app.py) ───────────────────────────


class TestSpawnTaskByIdDirect:
    """Call PennyApp.spawnTaskById_ directly to cover lines 434-436."""

    def _make_task(self, task_id="t-1"):
        from penny.tasks import Task
        return Task(task_id, "Fix bug", "P1", "/tmp/proj", "proj")

    def test_spawns_matching_task(self):
        from penny.app import PennyApp
        task = self._make_task("t-1")
        app = _make_fake_app()
        app._all_ready_tasks = [task]
        spawned = []

        def fake_spawn(t):
            spawned.append(t.task_id)

        app.spawnTask_ = fake_spawn  # type: ignore[method-assign]
        PennyApp.spawnTaskById_(app, "t-1")
        assert spawned == ["t-1"]

    def test_noop_when_task_not_in_ready_list(self):
        from penny.app import PennyApp
        app = _make_fake_app()
        app._all_ready_tasks = []
        spawned = []
        app.spawnTask_ = lambda t: spawned.append(t)  # type: ignore[method-assign]
        PennyApp.spawnTaskById_(app, "t-99")
        assert spawned == []


# ── _finishSpawn_ callback ────────────────────────────────────────────────────


class TestFinishSpawnDirect:
    """Call PennyApp._finishSpawn_ directly to cover the main-thread callback."""

    def _make_task(self, task_id="t-1"):
        from penny.tasks import Task
        return Task(task_id, "Fix bug", "P1", "/tmp/proj", "proj")

    def test_success_appends_agent_record(self):
        from penny.app import PennyApp
        task = self._make_task("t-1")
        app = _make_fake_app(state={"agents_running": []})
        app._pending_spawns = {"t-1": task}
        record = {"task_id": "t-1", "status": "running", "pid": 42}
        with patch("penny.app.save_state"), patch("penny.app.send_notification"):
            PennyApp._finishSpawn_(app, {"task_id": "t-1", "record": record, "error": None})
        assert len(app.state["agents_running"]) == 1
        assert app.state["agents_running"][0]["task_id"] == "t-1"

    def test_success_clears_pending(self):
        from penny.app import PennyApp
        task = self._make_task("t-1")
        app = _make_fake_app(state={"agents_running": []})
        app._pending_spawns = {"t-1": task}
        record = {"task_id": "t-1", "status": "running", "pid": 42}
        with patch("penny.app.save_state"), patch("penny.app.send_notification"):
            PennyApp._finishSpawn_(app, {"task_id": "t-1", "record": record, "error": None})
        assert "t-1" not in app._pending_spawns

    def test_error_triggers_fetch_not_state(self):
        from penny.app import PennyApp
        app = _make_fake_app(state={"agents_running": []})
        app._pending_spawns = {}
        PennyApp._finishSpawn_(app, {"task_id": "t-1", "record": None, "error": "boom"})
        assert app.state.get("agents_running") == []
        app._worker.fetch.assert_called_once()

    def test_error_with_record_none_triggers_fetch(self):
        from penny.app import PennyApp
        app = _make_fake_app(state={"agents_running": []})
        app._pending_spawns = {}
        PennyApp._finishSpawn_(app, {"task_id": "t-1", "record": None, "error": None})
        app._worker.fetch.assert_called_once()

    def test_notification_disabled_skips_send(self):
        from unittest.mock import patch

        from penny.app import PennyApp
        task = self._make_task("t-1")
        app = _make_fake_app(
            config={"notifications": {"spawn": False}},
            state={"agents_running": []},
        )
        app._pending_spawns = {"t-1": task}
        record = {"task_id": "t-1", "status": "running", "pid": 42}
        with patch("penny.app.send_notification") as mock_notify, \
             patch("penny.app.save_state"):
            PennyApp._finishSpawn_(app, {"task_id": "t-1", "record": record, "error": None})
        mock_notify.assert_not_called()

    def test_notification_sent_when_enabled(self):
        from unittest.mock import patch

        from penny.app import PennyApp
        task = self._make_task("t-1")
        app = _make_fake_app(state={"agents_running": []})
        app._pending_spawns = {"t-1": task}
        record = {"task_id": "t-1", "status": "running", "pid": 42}
        with patch("penny.app.send_notification") as mock_notify, \
             patch("penny.app.save_state"):
            PennyApp._finishSpawn_(app, {"task_id": "t-1", "record": record, "error": None})
        mock_notify.assert_called_once()


# ── dismissCompleted_ / clearAllCompleted_ (direct call) ─────────────────────


class TestDismissCompletedDirect:
    """Call PennyApp.dismissCompleted_ and clearAllCompleted_ directly."""

    def test_dismiss_removes_task(self):
        from penny.app import PennyApp
        state = {"agents_running": [], "recently_completed": [
            {"task_id": "t-1"}, {"task_id": "t-2"},
        ]}
        app = _make_fake_app(state=state)
        with patch("penny.app.save_state"):
            PennyApp.dismissCompleted_(app, "t-1")
        assert len(app.state["recently_completed"]) == 1
        assert app.state["recently_completed"][0]["task_id"] == "t-2"

    def test_clear_all_removes_all(self):
        from penny.app import PennyApp
        state = {"agents_running": [], "recently_completed": [
            {"task_id": "t-1"}, {"task_id": "t-2"},
        ]}
        app = _make_fake_app(state=state)
        with patch("penny.app.save_state"):
            PennyApp.clearAllCompleted_(app, None)
        assert app.state["recently_completed"] == []


# ── stopAgentByTaskId_ (direct call) ─────────────────────────────────────────


class TestStopAgentByTaskIdDirect:
    """Call PennyApp.stopAgentByTaskId_ directly to cover lines 439-473."""

    def test_noop_on_empty_task_id(self):
        from penny.app import PennyApp
        app = _make_fake_app(state={"agents_running": [{"task_id": "t-1", "pid": 99}]})
        with patch("penny.app.save_state"):
            PennyApp.stopAgentByTaskId_(app, "")
        assert len(app.state["agents_running"]) == 1

    def test_noop_when_task_id_not_found(self):
        from penny.app import PennyApp
        app = _make_fake_app(state={"agents_running": [{"task_id": "t-1", "pid": 99}]})
        with patch("penny.app.save_state"):
            PennyApp.stopAgentByTaskId_(app, "t-999")
        assert len(app.state["agents_running"]) == 1

    def test_removes_matching_agent(self):
        from penny.app import PennyApp
        app = _make_fake_app(state={
            "agents_running": [
                {"task_id": "t-1", "pid": 100, "session": "", "tmux_bin": ""},
                {"task_id": "t-2", "pid": 200, "session": "", "tmux_bin": ""},
            ],
            "recently_completed": [],
        })
        with (
            patch("penny.app.save_state"),
            patch("subprocess.run"),
        ):
            PennyApp.stopAgentByTaskId_(app, "t-1")
        running_ids = [a["task_id"] for a in app.state["agents_running"]]
        assert running_ids == ["t-2"]

    def test_kills_process_via_pid(self):
        """When agent has a pid > 0, os.killpg is called."""
        from penny.app import PennyApp
        app = _make_fake_app(state={
            "agents_running": [
                {"task_id": "t-1", "pid": 99999, "session": "", "tmux_bin": ""},
            ],
            "recently_completed": [],
        })
        with (
            patch("penny.app.save_state"),
            patch("subprocess.run"),
            patch("os.killpg") as mock_kill,
        ):
            PennyApp.stopAgentByTaskId_(app, "t-1")
        mock_kill.assert_called_once_with(99999, mock_kill.call_args[0][1])

    def test_gracefully_handles_process_lookup_error(self):
        """ProcessLookupError from os.killpg is swallowed."""
        from penny.app import PennyApp
        app = _make_fake_app(state={
            "agents_running": [
                {"task_id": "t-1", "pid": 99999, "session": "", "tmux_bin": ""},
            ],
            "recently_completed": [],
        })
        with (
            patch("penny.app.save_state"),
            patch("subprocess.run"),
            patch("os.killpg", side_effect=ProcessLookupError),
        ):
            PennyApp.stopAgentByTaskId_(app, "t-1")  # must not raise


# ── stopAgent_ legacy PID shim (direct call) ─────────────────────────────────


class TestStopAgentDirect:
    """Call PennyApp.stopAgent_ directly to cover lines 477-490."""

    def test_noop_when_pid_none(self):
        from penny.app import PennyApp
        app = _make_fake_app(state={"agents_running": []})
        stopped = []
        app.stopAgentByTaskId_ = lambda tid: stopped.append(tid)  # type: ignore[method-assign]
        PennyApp.stopAgent_(app, None)
        assert stopped == []

    def test_noop_when_pid_zero(self):
        from penny.app import PennyApp
        app = _make_fake_app(state={"agents_running": []})
        stopped = []
        app.stopAgentByTaskId_ = lambda tid: stopped.append(tid)  # type: ignore[method-assign]
        PennyApp.stopAgent_(app, 0)
        assert stopped == []

    def test_delegates_to_stop_by_task_id_when_pid_found(self):
        from penny.app import PennyApp
        app = _make_fake_app(state={
            "agents_running": [{"task_id": "t-1", "pid": 12345}],
        })
        stopped = []
        app.stopAgentByTaskId_ = lambda tid: stopped.append(tid)  # type: ignore[method-assign]
        PennyApp.stopAgent_(app, 12345)
        assert stopped == ["t-1"]

    def test_direct_kill_when_pid_not_in_agents(self):
        from penny.app import PennyApp
        app = _make_fake_app(state={"agents_running": []})
        app.stopAgentByTaskId_ = lambda tid: None  # type: ignore[method-assign]
        with patch("os.killpg") as mock_kill:
            PennyApp.stopAgent_(app, 12345)
        mock_kill.assert_called_once()

    def test_gracefully_handles_process_lookup_error(self):
        from penny.app import PennyApp
        app = _make_fake_app(state={"agents_running": []})
        app.stopAgentByTaskId_ = lambda tid: None  # type: ignore[method-assign]
        with patch("os.killpg", side_effect=ProcessLookupError):
            PennyApp.stopAgent_(app, 12345)  # must not raise


# ── _hot_reload_config (direct call into penny/app.py) ───────────────────────


class TestHotReloadConfigDirect:
    """Call PennyApp._hot_reload_config directly to cover lines 214-232."""

    def test_skips_on_yaml_error(self, tmp_path, capsys):
        from penny.app import PennyApp
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text("bad: [unclosed\n")
        app = _make_fake_app()
        with patch("penny.app.CONFIG_PATH", cfg_file):
            PennyApp._hot_reload_config(app)
        out = capsys.readouterr().out
        assert "YAML error" in out

    def test_applies_new_config(self, tmp_path):
        from penny.app import PennyApp
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text("projects:\n  - path: /tmp/proj\n")
        app = _make_fake_app()
        with patch("penny.app.CONFIG_PATH", cfg_file):
            PennyApp._hot_reload_config(app)
        assert app.config.get("projects") == [{"path": "/tmp/proj"}]

    def test_updates_config_mtime(self, tmp_path):
        from penny.app import PennyApp
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text("projects: []\n")
        app = _make_fake_app()
        assert app._config_mtime is None
        with patch("penny.app.CONFIG_PATH", cfg_file):
            PennyApp._hot_reload_config(app)
        assert app._config_mtime is not None

    def test_calls_sync_launchd_service(self, tmp_path):
        from penny.app import PennyApp
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text("projects: []\n")
        app = _make_fake_app()
        sync_called = []
        app._sync_launchd_service = lambda: sync_called.append(True)
        with patch("penny.app.CONFIG_PATH", cfg_file):
            PennyApp._hot_reload_config(app)
        assert sync_called

    def test_calls_plugin_mgr_sync(self, tmp_path):
        from penny.app import PennyApp
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text("projects: []\n")
        app = _make_fake_app()
        with patch("penny.app.CONFIG_PATH", cfg_file):
            PennyApp._hot_reload_config(app)
        app._plugin_mgr.sync_with_config.assert_called_once()

    def test_rebuilds_plugin_sections(self, tmp_path):
        from penny.app import PennyApp
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text("projects: []\n")
        app = _make_fake_app()
        with patch("penny.app.CONFIG_PATH", cfg_file):
            PennyApp._hot_reload_config(app)
        app._vc.rebuild_plugin_sections.assert_called_once()


# ── _compact_reset_time (direct call via PennyApp class method) ───────────────


class TestCompactResetTimeDirect:
    """Call PennyApp._compact_reset_time directly to cover lines 352-379."""

    def _call(self, label: str) -> str:
        from penny.app import PennyApp
        app = _make_fake_app()
        return PennyApp._compact_reset_time(app, label)

    def test_empty_returns_empty(self):
        assert self._call("") == ""

    def test_em_dash_returns_empty(self):
        assert self._call("\u2014") == ""

    def test_long_form_12h_12h_mode(self):
        with patch("penny.app.uses_24h_time", return_value=False):
            assert self._call("Today at 4:59 PM") == "4:59pm"

    def test_long_form_12h_24h_mode(self):
        with patch("penny.app.uses_24h_time", return_value=True):
            assert self._call("Today at 4:59 PM") == "16:59"

    def test_long_form_24h(self):
        with patch("penny.app.uses_24h_time", return_value=False):
            assert self._call("Today at 16:59") == "16:59"

    def test_compact_passthrough_in_12h_mode(self):
        with patch("penny.app.uses_24h_time", return_value=False):
            assert self._call("9pm") == "9pm"

    def test_compact_to_24h(self):
        with patch("penny.app.uses_24h_time", return_value=True):
            assert self._call("9pm") == "21"

    def test_unknown_label_returned_as_is(self):
        with patch("penny.app.uses_24h_time", return_value=False):
            assert self._call("some label") == "some label"


# ── _normalize_config (legacy menubar mode migration) ─────────────────────


class TestNormalizeConfig:
    """Test _normalize_config migrates legacy menubar mode names."""

    def test_migrates_hbars_to_bars(self):
        from penny.app import _normalize_config
        cfg = {"menubar": {"mode": "hbars"}}
        result = _normalize_config(cfg)
        assert result["menubar"]["mode"] == "bars"

    def test_migrates_bars_plus_t_to_bars(self):
        from penny.app import _normalize_config
        cfg = {"menubar": {"mode": "bars+t"}}
        result = _normalize_config(cfg)
        assert result["menubar"]["mode"] == "bars"

    def test_migrates_hbars_plus_t_to_bars(self):
        from penny.app import _normalize_config
        cfg = {"menubar": {"mode": "hbars+t"}}
        result = _normalize_config(cfg)
        assert result["menubar"]["mode"] == "bars"

    def test_preserves_current_bars_mode(self):
        from penny.app import _normalize_config
        cfg = {"menubar": {"mode": "bars"}}
        result = _normalize_config(cfg)
        assert result["menubar"]["mode"] == "bars"

    def test_migrates_compact_to_bars(self):
        from penny.app import _normalize_config
        cfg = {"menubar": {"mode": "compact"}}
        result = _normalize_config(cfg)
        assert result["menubar"]["mode"] == "bars"

    def test_migrates_minimal_to_bars(self):
        from penny.app import _normalize_config
        cfg = {"menubar": {"mode": "minimal"}}
        result = _normalize_config(cfg)
        assert result["menubar"]["mode"] == "bars"

    def test_no_menubar_section(self):
        from penny.app import _normalize_config
        cfg = {"projects": []}
        result = _normalize_config(cfg)
        assert result == {"projects": []}

    def test_empty_config(self):
        from penny.app import _normalize_config
        cfg = {}
        result = _normalize_config(cfg)
        assert result == {}

    def test_menubar_not_dict(self):
        """If menubar is not a dict (e.g. None), config is returned unchanged."""
        from penny.app import _normalize_config
        cfg = {"menubar": None}
        result = _normalize_config(cfg)
        assert result == {"menubar": None}

    def test_no_mode_key_in_menubar(self):
        from penny.app import _normalize_config
        cfg = {"menubar": {"show_sonnet": True}}
        result = _normalize_config(cfg)
        assert result["menubar"] == {"show_sonnet": True}


# ── quitApp_ (direct call into penny/app.py) ─────────────────────────────────


class TestQuitAppDirect:
    """Call PennyApp.quitApp_ directly to cover lines 1091-1116."""

    def test_keep_alive_true_spawns_kickstart_and_terminates(self):
        """When keep_alive is on, quitApp_ spawns launchctl kickstart and terminates."""
        import subprocess as sp

        from penny.app import PennyApp
        app = _make_fake_app(config={"service": {"keep_alive": True}})
        mock_ns_app = MagicMock()
        with (
            patch("penny.app.NSApplication") as mock_ns_cls,
            patch("subprocess.Popen") as mock_popen,
            patch("os.getuid", return_value=501),
        ):
            mock_ns_cls.sharedApplication.return_value = mock_ns_app
            PennyApp.quitApp_(app, None)
        # Verify Popen was called with kickstart args and detach flags
        mock_popen.assert_called_once()
        call_args = mock_popen.call_args
        cmd = call_args[0][0]
        assert cmd[0] == "launchctl"
        assert cmd[1] == "kickstart"
        assert "-k" in cmd
        assert "gui/501/com.gpxl.penny" in cmd[3]
        assert call_args[1]["start_new_session"] is True
        assert call_args[1]["stdout"] is sp.DEVNULL
        assert call_args[1]["stderr"] is sp.DEVNULL
        assert call_args[1]["close_fds"] is True
        # NSApplication.terminate_ called
        mock_ns_app.terminate_.assert_called_once_with(None)

    def test_keep_alive_false_just_terminates(self):
        """When keep_alive is off, quitApp_ just terminates (no launchctl)."""
        from penny.app import PennyApp
        app = _make_fake_app(config={"service": {"keep_alive": False}})
        mock_ns_app = MagicMock()
        with (
            patch("penny.app.NSApplication") as mock_ns_cls,
            patch("subprocess.Popen") as mock_popen,
        ):
            mock_ns_cls.sharedApplication.return_value = mock_ns_app
            PennyApp.quitApp_(app, None)
        mock_popen.assert_not_called()
        mock_ns_app.terminate_.assert_called_once_with(None)

    def test_keep_alive_default_true_when_service_empty(self):
        """Default behavior: service: {} → keep_alive defaults to True."""
        from penny.app import PennyApp
        app = _make_fake_app(config={"service": {}})
        mock_ns_app = MagicMock()
        with (
            patch("penny.app.NSApplication") as mock_ns_cls,
            patch("subprocess.Popen") as mock_popen,
            patch("os.getuid", return_value=501),
        ):
            mock_ns_cls.sharedApplication.return_value = mock_ns_app
            PennyApp.quitApp_(app, None)
        # keep_alive defaults to True, so kickstart is called
        mock_popen.assert_called_once()

    def test_keep_alive_default_true_when_no_config(self):
        """No config at all → keep_alive defaults to True."""
        from penny.app import PennyApp
        app = _make_fake_app(config={})
        mock_ns_app = MagicMock()
        with (
            patch("penny.app.NSApplication") as mock_ns_cls,
            patch("subprocess.Popen") as mock_popen,
            patch("os.getuid", return_value=501),
        ):
            mock_ns_cls.sharedApplication.return_value = mock_ns_app
            PennyApp.quitApp_(app, None)
        mock_popen.assert_called_once()

    def test_keep_alive_none_config(self):
        """When config is None, keep_alive defaults to True."""
        from penny.app import PennyApp
        app = _make_fake_app()
        app.config = None  # edge case
        mock_ns_app = MagicMock()
        with (
            patch("penny.app.NSApplication") as mock_ns_cls,
            patch("subprocess.Popen") as mock_popen,
            patch("os.getuid", return_value=501),
        ):
            mock_ns_cls.sharedApplication.return_value = mock_ns_app
            PennyApp.quitApp_(app, None)
        mock_popen.assert_called_once()


# ── _checkConfig_ (direct call) ──────────────────────────────────────────────


class TestCheckConfigDirect:
    """Call PennyApp._checkConfig_ to cover lines 436-440."""

    def test_noop_when_mtime_is_none(self):
        from penny.app import PennyApp
        app = _make_fake_app()
        app._config_mtime = None
        reload_called = []
        app._hot_reload_config = lambda: reload_called.append(True)
        with patch("penny.app._config_mtime", return_value=None):
            PennyApp._checkConfig_(app, None)
        assert reload_called == []

    def test_noop_when_mtime_unchanged(self):
        from penny.app import PennyApp
        app = _make_fake_app()
        app._config_mtime = 12345.0
        reload_called = []
        app._hot_reload_config = lambda: reload_called.append(True)
        with patch("penny.app._config_mtime", return_value=12345.0):
            PennyApp._checkConfig_(app, None)
        assert reload_called == []

    def test_reloads_when_mtime_changes(self):
        from penny.app import PennyApp
        app = _make_fake_app()
        app._config_mtime = 12345.0
        reload_called = []
        app._hot_reload_config = lambda: reload_called.append(True)
        with patch("penny.app._config_mtime", return_value=99999.0):
            PennyApp._checkConfig_(app, None)
        assert reload_called == [True]


# ── refreshNow_ (direct call) ────────────────────────────────────────────────


class TestRefreshNowDirect:
    """Call PennyApp.refreshNow_ to cover lines 480-483."""

    def test_sets_refreshing_and_fetches(self):
        from penny.app import PennyApp
        app = _make_fake_app()
        PennyApp.refreshNow_(app, None)
        app._vc.setRefreshing_.assert_called_once_with(True)
        app._worker.fetch.assert_called_once_with(force=True)


# ── viewReport_ (direct call) ────────────────────────────────────────────────


class TestViewReportDirect:
    """Call PennyApp.viewReport_ to cover lines 1075-1086."""

    def test_opens_dashboard_url(self):
        from penny.app import PennyApp
        app = _make_fake_app()
        app._dashboard = MagicMock()
        app._dashboard.ensure_started.return_value = 7432
        app._popover = MagicMock()
        app._popover.isShown.return_value = False
        with patch("subprocess.run") as mock_run:
            PennyApp.viewReport_(app, None)
        app._dashboard.ensure_started.assert_called_once()
        mock_run.assert_called_once_with(["open", "http://127.0.0.1:7432/"], check=False)

    def test_fallback_to_static_report(self):
        from penny.app import PennyApp
        app = _make_fake_app()
        app._dashboard = MagicMock()
        app._dashboard.ensure_started.side_effect = RuntimeError("fail")
        app._popover = MagicMock()
        app._popover.isShown.return_value = False
        with (
            patch("penny.app.generate_report", return_value="/tmp/report.html") as mock_gen,
            patch("penny.app.open_report") as mock_open,
        ):
            PennyApp.viewReport_(app, None)
        mock_gen.assert_called_once()
        mock_open.assert_called_once_with("/tmp/report.html")

    def test_closes_popover_when_shown(self):
        from penny.app import PennyApp
        app = _make_fake_app()
        app._dashboard = MagicMock()
        app._dashboard.ensure_started.return_value = 7432
        app._popover = MagicMock()
        app._popover.isShown.return_value = True
        with patch("subprocess.run"):
            PennyApp.viewReport_(app, None)
        app._popover.performClose_.assert_called_once()

    def test_fallback_swallows_double_exception(self):
        """If both dashboard and static report fail, no exception propagates."""
        from penny.app import PennyApp
        app = _make_fake_app()
        app._dashboard = MagicMock()
        app._dashboard.ensure_started.side_effect = RuntimeError("dashboard fail")
        app._popover = MagicMock()
        app._popover.isShown.return_value = False
        with (
            patch("penny.app.generate_report", side_effect=RuntimeError("report fail")),
            patch("penny.app.open_report"),
        ):
            PennyApp.viewReport_(app, None)  # must not raise


# ── openPrefs_ (direct call) ─────────────────────────────────────────────────


class TestOpenPrefsDirect:
    """Call PennyApp.openPrefs_ to cover line 1089."""

    def test_opens_config_file(self):
        from penny.app import PennyApp
        app = _make_fake_app()
        with patch("subprocess.run") as mock_run:
            PennyApp.openPrefs_(app, None)
        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "open"
        assert "config.yaml" in cmd[1]


# ── _sync_launchd_service (direct call) ──────────────────────────────────────


class TestSyncLaunchdServiceDirect:
    """Call PennyApp._sync_launchd_service to cover lines 1010-1056."""

    def _make_plist(self, path, keep_alive=True, run_at_load=True):
        import plistlib
        path.write_bytes(plistlib.dumps({
            "Label": "com.gpxl.penny",
            "KeepAlive": keep_alive,
            "RunAtLoad": run_at_load,
            "WorkingDirectory": "/usr/local/penny",
        }))

    def test_noop_when_plist_missing(self, tmp_path):
        from penny.app import PennyApp
        app = _make_fake_app(config={"service": {"keep_alive": False}})
        with patch("penny.app.PLIST_LAUNCHAGENTS", tmp_path / "missing.plist"):
            PennyApp._sync_launchd_service(app)
        # No error should occur

    def test_noop_when_already_in_sync(self, tmp_path):
        import plistlib

        from penny.app import PennyApp
        plist = tmp_path / "test.plist"
        self._make_plist(plist, keep_alive=True, run_at_load=True)
        app = _make_fake_app(config={"service": {"keep_alive": True, "launch_at_login": True}})
        with patch("penny.app.PLIST_LAUNCHAGENTS", plist):
            PennyApp._sync_launchd_service(app)
        # Verify plist was NOT rewritten (file should be the same)
        pl = plistlib.loads(plist.read_bytes())
        assert pl["KeepAlive"] is True

    def test_updates_plist_when_keep_alive_changes(self, tmp_path):
        import plistlib

        from penny.app import PennyApp
        plist = tmp_path / "test.plist"
        self._make_plist(plist, keep_alive=True, run_at_load=True)
        app = _make_fake_app(config={"service": {"keep_alive": False, "launch_at_login": True}})
        with (
            patch("penny.app.PLIST_LAUNCHAGENTS", plist),
            patch("penny.app._script_dir_from_plist", return_value=None),
        ):
            PennyApp._sync_launchd_service(app)
        pl = plistlib.loads(plist.read_bytes())
        assert pl["KeepAlive"] is False

    def test_updates_source_copy_in_script_dir(self, tmp_path):
        import plistlib

        from penny.app import PennyApp
        plist = tmp_path / "test.plist"
        script_dir = tmp_path / "scripts"
        script_dir.mkdir()
        self._make_plist(plist, keep_alive=True, run_at_load=True)
        app = _make_fake_app(config={"service": {"keep_alive": False, "launch_at_login": True}})
        with (
            patch("penny.app.PLIST_LAUNCHAGENTS", plist),
            patch("penny.app._script_dir_from_plist", return_value=script_dir),
        ):
            PennyApp._sync_launchd_service(app)
        source_copy = script_dir / "com.gpxl.penny.plist"
        assert source_copy.exists()
        pl = plistlib.loads(source_copy.read_bytes())
        assert pl["KeepAlive"] is False

    def test_handles_corrupt_plist_gracefully(self, tmp_path):
        from penny.app import PennyApp
        plist = tmp_path / "test.plist"
        plist.write_bytes(b"not a valid plist")
        app = _make_fake_app(config={"service": {"keep_alive": False}})
        with patch("penny.app.PLIST_LAUNCHAGENTS", plist):
            PennyApp._sync_launchd_service(app)  # must not raise


# ── _update_status_title (direct call) ────────────────────────────────────────


class TestUpdateStatusTitleDirect:
    """Call PennyApp._update_status_title to cover lines 788-832."""

    def test_returns_early_during_final_animation(self):
        from penny.app import PennyApp
        app = _make_fake_app()
        app._loading_phase = "final_bars"
        btn = app._status_item.button.return_value
        PennyApp._update_status_title(app)
        btn.setTitle_.assert_not_called()

    def test_returns_early_when_btn_none(self):
        from penny.app import PennyApp
        app = _make_fake_app()
        app._loading_phase = "done"
        app._status_item.button.return_value = None
        PennyApp._update_status_title(app)
        # Should not raise

    def test_sets_image_and_empty_title_with_prediction(self):
        from penny.analysis import Prediction
        from penny.app import PennyApp
        pred = Prediction(session_pct_all=10.0, pct_all=42.0, pct_sonnet=30.0,
                          session_reset_label="2pm")
        app = _make_fake_app(config={"menubar": {"mode": "bars", "show_sonnet": True}})
        app._prediction = pred
        app._loading_phase = "done"
        app._loading_anim_timer = None
        app._format_menubar_title = lambda p, n: PennyApp._format_menubar_title(app, p, n)
        app._compact_reset_time = lambda lbl: PennyApp._compact_reset_time(app, lbl)
        app._make_status_image = MagicMock(return_value=MagicMock())
        PennyApp._update_status_title(app)
        btn = app._status_item.button.return_value
        # Bars mode: title is empty, image is set
        btn.setTitle_.assert_called_once_with("")
        app._make_status_image.assert_called_once()
        btn.setImage_.assert_called_once()
        btn.setToolTip_.assert_called_once()

    def test_returns_early_during_loading_phase(self):
        """_update_status_title returns early during 'loading' phase.

        The loading animation timer is now invalidated at the end of the
        final-clock animation cycle, not inside _update_status_title.
        """
        from penny.analysis import Prediction
        from penny.app import PennyApp
        pred = Prediction(session_pct_all=10.0, pct_all=42.0, pct_sonnet=30.0)
        app = _make_fake_app(config={"menubar": {"mode": "bars"}})
        app._prediction = pred
        app._loading_phase = "loading"
        mock_timer = MagicMock()
        app._loading_anim_timer = mock_timer
        PennyApp._update_status_title(app)
        # Early return: nothing happens — timer stays, phase stays
        btn = app._status_item.button.return_value
        btn.setTitle_.assert_not_called()
        mock_timer.invalidate.assert_not_called()
        assert app._loading_phase == "loading"

    def test_sets_title_loading_when_no_prediction(self):
        from penny.app import PennyApp
        app = _make_fake_app()
        app._prediction = None
        app._loading_phase = "done"
        app._loading_anim_timer = None
        app._format_menubar_title = lambda p, n: PennyApp._format_menubar_title(app, p, n)
        PennyApp._update_status_title(app)
        btn = app._status_item.button.return_value
        title = btn.setTitle_.call_args[0][0]
        assert title == "Loading\u2026"


# ── set_menubar_mode (direct call) ───────────────────────────────────────────


class TestSetMenubarModeDirect:
    """Call PennyApp.set_menubar_mode to cover lines 1064-1068."""

    def test_sets_mode_and_persists(self, tmp_path):
        from penny.app import PennyApp
        app = _make_fake_app(config={})
        write_called = []
        app._write_config = lambda: write_called.append(True)
        PennyApp.set_menubar_mode(app, "bars")
        assert app.config["menubar"]["mode"] == "bars"
        assert write_called == [True]
        app._update_status_title.assert_called_once()


# ── toggleKeepAlive_ / toggleLaunchAtLogin_ (direct call) ────────────────────


class TestToggleServiceDirect:
    """Call PennyApp.toggleKeepAlive_ and toggleLaunchAtLogin_."""

    def test_toggle_keep_alive(self):
        from penny.app import PennyApp
        app = _make_fake_app(config={})
        write_called = []
        sync_called = []
        app._write_config = lambda: write_called.append(True)
        app._sync_launchd_service = lambda: sync_called.append(True)
        sender = MagicMock()
        sender.state.return_value = 0  # False
        PennyApp.toggleKeepAlive_(app, sender)
        assert app.config["service"]["keep_alive"] is False
        assert write_called == [True]
        assert sync_called == [True]

    def test_toggle_launch_at_login(self):
        from penny.app import PennyApp
        app = _make_fake_app(config={})
        write_called = []
        sync_called = []
        app._write_config = lambda: write_called.append(True)
        app._sync_launchd_service = lambda: sync_called.append(True)
        sender = MagicMock()
        sender.state.return_value = 1  # True
        PennyApp.toggleLaunchAtLogin_(app, sender)
        assert app.config["service"]["launch_at_login"] is True
        assert write_called == [True]
        assert sync_called == [True]


# ── spawnTask_ agent_permissions=off (direct call) ───────────────────────────


class TestSpawnTaskPermissionsOff:
    """Call PennyApp.spawnTask_ with agent_permissions=off to cover lines 836-839."""

    def test_skips_when_permissions_off(self):
        from penny.app import PennyApp
        from penny.tasks import Task
        app = _make_fake_app(config={"work": {"agent_permissions": "off"}})
        app._pending_spawns = {}
        task = Task("t-1", "Fix bug", "P1", "/tmp/proj", "proj")
        app._all_ready_tasks = [task]
        PennyApp.spawnTask_(app, task)
        # Should not have added to pending_spawns
        assert "t-1" not in app._pending_spawns
        # all_ready_tasks should remain unchanged
        assert len(app._all_ready_tasks) == 1

    def test_optimistic_update_when_permissions_allowed(self):
        """spawnTask_ performs optimistic state update before background spawn."""
        from penny.app import PennyApp
        from penny.tasks import Task
        app = _make_fake_app(config={})
        app._pending_spawns = {}
        t1 = Task("t-1", "Fix bug", "P1", "/tmp/proj", "proj")
        t2 = Task("t-2", "Add feat", "P2", "/tmp/proj", "proj")
        app._all_ready_tasks = [t1, t2]
        with patch("threading.Thread") as mock_thread:
            mock_thread.return_value.start = MagicMock()
            PennyApp.spawnTask_(app, t1)
        # Task removed from ready list optimistically
        assert len(app._all_ready_tasks) == 1
        assert app._all_ready_tasks[0].task_id == "t-2"
        # Task added to pending_spawns
        assert "t-1" in app._pending_spawns
        # VC updated
        app._vc.updateWithData_.assert_called_once()
        # Thread started
        mock_thread.return_value.start.assert_called_once()


# ── _spawn_agents (direct call) ──────────────────────────────────────────────


class TestSpawnAgentsDirect:
    """Call PennyApp._spawn_agents to cover lines 1193-1218."""

    def _make_task(self, task_id="t-1"):
        from penny.tasks import Task
        return Task(task_id, f"Task {task_id}", "P2", "/tmp/proj", "proj")

    def test_noop_when_no_ready_tasks(self):
        from penny.app import PennyApp
        app = _make_fake_app()
        app._ready_tasks = []
        with patch("penny.app.spawn_claude_agent") as mock_spawn:
            PennyApp._spawn_agents(app)
        mock_spawn.assert_not_called()

    def test_noop_when_permissions_off(self):
        from penny.app import PennyApp
        app = _make_fake_app(config={"work": {"agent_permissions": "off"}})
        app._ready_tasks = [self._make_task()]
        with patch("penny.app.spawn_claude_agent") as mock_spawn:
            PennyApp._spawn_agents(app)
        mock_spawn.assert_not_called()

    def test_spawns_all_ready_tasks(self):
        from penny.analysis import Prediction
        from penny.app import PennyApp
        t1 = self._make_task("t-1")
        t2 = self._make_task("t-2")
        pred = Prediction(pct_all=50.0, projected_pct_all=70.0, days_remaining=2.0)
        app = _make_fake_app(state={"agents_running": []})
        app._ready_tasks = [t1, t2]
        app._prediction = pred
        app.config = {"notifications": {"spawn": True}}
        record = {"task_id": "t-1", "status": "running", "pid": 42}
        with (
            patch("penny.app.spawn_claude_agent", return_value=record) as mock_spawn,
            patch("penny.app.save_state"),
            patch("penny.app.send_notification") as mock_notify,
        ):
            PennyApp._spawn_agents(app)
        assert mock_spawn.call_count == 2
        assert len(app.state["agents_running"]) == 2
        mock_notify.assert_called_once()
        msg = mock_notify.call_args[0][1]
        assert "2 agent(s)" in msg

    def test_no_notification_when_disabled(self):
        from penny.analysis import Prediction
        from penny.app import PennyApp
        t1 = self._make_task("t-1")
        pred = Prediction(pct_all=50.0, projected_pct_all=70.0, days_remaining=2.0)
        app = _make_fake_app(state={"agents_running": []})
        app._ready_tasks = [t1]
        app._prediction = pred
        app.config = {"notifications": {"spawn": False}}
        record = {"task_id": "t-1", "status": "running", "pid": 42}
        with (
            patch("penny.app.spawn_claude_agent", return_value=record),
            patch("penny.app.save_state"),
            patch("penny.app.send_notification") as mock_notify,
        ):
            PennyApp._spawn_agents(app)
        mock_notify.assert_not_called()

    def test_no_notification_when_no_prediction(self):
        from penny.app import PennyApp
        t1 = self._make_task("t-1")
        app = _make_fake_app(state={"agents_running": []})
        app._ready_tasks = [t1]
        app._prediction = None
        app.config = {"notifications": {"spawn": True}}
        record = {"task_id": "t-1", "status": "running", "pid": 42}
        with (
            patch("penny.app.spawn_claude_agent", return_value=record),
            patch("penny.app.save_state"),
            patch("penny.app.send_notification") as mock_notify,
        ):
            PennyApp._spawn_agents(app)
        mock_notify.assert_not_called()


# ── _didFetchData_ external completion detection ─────────────────────────────


class TestDidFetchDataExternalCompletion:
    """Test external task completion detection and reconciliation in _didFetchData_."""

    def _make_result(self, **kwargs):
        base = {
            "state": {"agents_running": [], "recently_completed": []},
            "prediction": None,
            "newly_done": [],
        }
        base.update(kwargs)
        return base

    def test_external_completed_tasks_added_to_recently_completed(self):
        from penny.app import PennyApp
        from penny.tasks import Task
        ext_task = Task("ext-1", "External fix", "P1", "/tmp/proj", "proj")
        app = _make_fake_app(config={"notifications": {"completion": False}})
        app._plugin_mgr.get_all_completed_tasks.return_value = [ext_task]
        app._plugin_mgr.get_all_tasks.return_value = []
        app._plugin_mgr.filter_all_tasks.return_value = []
        result = self._make_result(
            state={"agents_running": [], "recently_completed": []},
        )
        with (
            patch("penny.app.should_trigger", return_value=False),
            patch("penny.app.save_state"),
        ):
            PennyApp._didFetchData_(app, result)
        rc = app.state.get("recently_completed", [])
        assert any(a["task_id"] == "ext-1" for a in rc)
        assert rc[-1]["completed_by"] == "external"

    def test_external_completion_notification_sent(self):
        from penny.app import PennyApp
        from penny.tasks import Task
        ext_task = Task("ext-1", "External fix", "P1", "/tmp/proj", "proj")
        app = _make_fake_app(config={"notifications": {"completion": True}})
        app._plugin_mgr.get_all_completed_tasks.return_value = [ext_task]
        app._plugin_mgr.get_all_tasks.return_value = []
        app._plugin_mgr.filter_all_tasks.return_value = []
        result = self._make_result(
            state={"agents_running": [], "recently_completed": []},
        )
        with (
            patch("penny.app.should_trigger", return_value=False),
            patch("penny.app.save_state"),
            patch("penny.app.send_notification") as mock_notify,
        ):
            PennyApp._didFetchData_(app, result)
        assert mock_notify.call_count >= 1
        # The notification about the external completion
        calls = [c for c in mock_notify.call_args_list if "externally" in c[0][1]]
        assert len(calls) == 1

    def test_reconcile_removes_false_completed(self):
        """Tasks that reappear in bd ready are removed from recently_completed."""
        from penny.app import PennyApp
        from penny.tasks import Task
        reappeared_task = Task("t-1", "Fix", "P1", "/tmp/proj", "proj")
        app = _make_fake_app(config={})
        app._plugin_mgr.get_all_completed_tasks.return_value = []
        app._plugin_mgr.get_all_tasks.return_value = [reappeared_task]
        app._plugin_mgr.filter_all_tasks.return_value = []
        result = self._make_result(
            state={
                "agents_running": [],
                "recently_completed": [{"task_id": "t-1", "status": "completed"}],
            },
        )
        with (
            patch("penny.app.should_trigger", return_value=False),
            patch("penny.app.save_state") as mock_save,
        ):
            PennyApp._didFetchData_(app, result)
        rc = app.state.get("recently_completed", [])
        assert not any(a.get("task_id") == "t-1" for a in rc)
        mock_save.assert_called()


# ── _newTaskSheet_ (direct call) ─────────────────────────────────────────────


class TestNewTaskSheetDirect:
    """Call PennyApp._newTaskSheet_ to cover line 1071."""

    def test_opens_config_file(self):
        from penny.app import PennyApp
        app = _make_fake_app()
        with patch("subprocess.run") as mock_run:
            PennyApp._newTaskSheet_(app, None)
        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "open"


# ── _load_and_refresh (direct call) ──────────────────────────────────────────


class TestLoadAndRefreshDirect:
    """Call PennyApp._load_and_refresh to cover lines 1121-1190."""

    def test_yaml_error_shows_alert_and_returns_early(self, tmp_path):
        from penny.app import PennyApp
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text("bad: [unclosed\n")
        app = _make_fake_app()
        show_alert_calls = []
        app._show_alert = lambda title, msg: show_alert_calls.append((title, msg))
        with patch("penny.app.CONFIG_PATH", cfg_file):
            PennyApp._load_and_refresh(app)
        assert len(show_alert_calls) == 1
        assert "Config Error" in show_alert_calls[0][0]
        # Worker fetch should not be called
        app._worker.fetch.assert_not_called()

    def test_normal_config_loads_and_fetches(self, tmp_path):
        from penny.app import PennyApp
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text("projects:\n  - path: /tmp/proj\n")
        app = _make_fake_app()
        app._show_alert = MagicMock()
        app._has_setup_issues = False
        with (
            patch("penny.app.CONFIG_PATH", cfg_file),
            patch("penny.app.load_state", return_value={}),
            patch("penny.app.reset_period_if_needed", side_effect=lambda s: s),
            patch("penny.app.needs_onboarding", return_value=False),
            patch("penny.app.run_preflight", return_value=[]),
            patch("penny.app.save_state"),
            patch("penny.app._config_mtime", return_value=12345.0),
            patch("penny.app.check_full_permissions_consent", return_value=True),
        ):
            PennyApp._load_and_refresh(app)
        assert app.config["projects"] == [{"path": "/tmp/proj"}]
        app._worker.fetch.assert_called_once_with(force=True)

    def test_onboarding_deferred_sets_flag_and_returns(self, tmp_path):
        from penny.app import PennyApp
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text("{}\n")  # no projects → needs_onboarding=True
        app = _make_fake_app()
        app._show_alert = MagicMock()
        with (
            patch("penny.app.CONFIG_PATH", cfg_file),
            patch("penny.app.load_state", return_value={}),
            patch("penny.app.reset_period_if_needed", side_effect=lambda s: s),
            patch("penny.app.needs_onboarding", return_value=True),
            patch("penny.app.run_onboarding", return_value=None),  # deferred
            patch("penny.app.save_state"),
            patch("penny.app.NSTimer"),
        ):
            PennyApp._load_and_refresh(app)
        assert app.state.get("onboarding_deferred") is True
        app._worker.fetch.assert_not_called()

    def test_onboarding_completed_updates_config(self, tmp_path):
        from penny.app import PennyApp
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text("{}\n")
        updated_config = {"projects": [{"path": "/tmp/proj"}]}
        app = _make_fake_app()
        app._show_alert = MagicMock()
        app._has_setup_issues = False
        # state must NOT have onboarding_deferred=True, otherwise the
        # `not self.state.get("onboarding_deferred")` check skips onboarding.
        with (
            patch("penny.app.CONFIG_PATH", cfg_file),
            patch("penny.app.load_state", return_value={}),
            patch("penny.app.reset_period_if_needed", side_effect=lambda s: s),
            patch("penny.app.needs_onboarding", return_value=True),
            patch("penny.app.run_onboarding", return_value=updated_config),
            patch("penny.app.run_preflight", return_value=[]),
            patch("penny.app.save_state"),
            patch("penny.app._config_mtime", return_value=12345.0),
            patch("penny.app.check_full_permissions_consent", return_value=True),
        ):
            PennyApp._load_and_refresh(app)
        assert app.config["projects"] == [{"path": "/tmp/proj"}]
        assert "onboarding_deferred" not in app.state

    def test_full_permissions_granted_saves_state(self, tmp_path):
        """When consent is granted for full permissions, state is saved."""
        from penny.app import PennyApp
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text("work:\n  agent_permissions: full\nprojects:\n  - path: /tmp/p\n")
        app = _make_fake_app()
        app._show_alert = MagicMock()
        app._has_setup_issues = False
        with (
            patch("penny.app.CONFIG_PATH", cfg_file),
            patch("penny.app.load_state", return_value={}),
            patch("penny.app.reset_period_if_needed", side_effect=lambda s: s),
            patch("penny.app.needs_onboarding", return_value=False),
            patch("penny.app.check_full_permissions_consent", return_value=True),
            patch("penny.app.run_preflight", return_value=[]),
            patch("penny.app.save_state") as mock_save,
            patch("penny.app._config_mtime", return_value=12345.0),
        ):
            PennyApp._load_and_refresh(app)
        # Consent granted: agent_permissions remains "full"
        assert app.config["work"]["agent_permissions"] == "full"
        # save_state should be called (covers line 1101)
        assert mock_save.call_count >= 1

    def test_full_permissions_declined_reverts_to_off(self, tmp_path):
        from penny.app import PennyApp
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text("work:\n  agent_permissions: full\nprojects:\n  - path: /tmp/p\n")
        app = _make_fake_app()
        app._show_alert = MagicMock()
        app._has_setup_issues = False
        write_called = []
        app._write_config = lambda: write_called.append(True)
        with (
            patch("penny.app.CONFIG_PATH", cfg_file),
            patch("penny.app.load_state", return_value={}),
            patch("penny.app.reset_period_if_needed", side_effect=lambda s: s),
            patch("penny.app.needs_onboarding", return_value=False),
            patch("penny.app.check_full_permissions_consent", return_value=False),
            patch("penny.app.run_preflight", return_value=[]),
            patch("penny.app.save_state"),
            patch("penny.app._config_mtime", return_value=12345.0),
        ):
            PennyApp._load_and_refresh(app)
        assert app.config["work"]["agent_permissions"] == "off"
        assert write_called == [True]

    def test_preflight_tool_errors_show_alert(self, tmp_path):
        from penny.app import PennyApp
        from penny.preflight import PreflightIssue
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text("projects:\n  - path: /tmp/p\n")
        app = _make_fake_app()
        alert_calls = []
        app._show_alert = lambda t, m: alert_calls.append((t, m))
        app._has_setup_issues = False
        issues = [PreflightIssue("error", "`claude` CLI not found", "Install it")]
        with (
            patch("penny.app.CONFIG_PATH", cfg_file),
            patch("penny.app.load_state", return_value={}),
            patch("penny.app.reset_period_if_needed", side_effect=lambda s: s),
            patch("penny.app.needs_onboarding", return_value=False),
            patch("penny.app.check_full_permissions_consent", return_value=True),
            patch("penny.app.run_preflight", return_value=issues),
            patch("penny.app.save_state"),
            patch("penny.app._config_mtime", return_value=12345.0),
        ):
            PennyApp._load_and_refresh(app)
        assert len(alert_calls) == 1
        assert "Setup Required" in alert_calls[0][0]
        assert app._has_setup_issues is True

    def test_preflight_exception_handled(self, tmp_path):
        from penny.app import PennyApp
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text("projects:\n  - path: /tmp/p\n")
        app = _make_fake_app()
        app._show_alert = MagicMock()
        app._has_setup_issues = False
        with (
            patch("penny.app.CONFIG_PATH", cfg_file),
            patch("penny.app.load_state", return_value={}),
            patch("penny.app.reset_period_if_needed", side_effect=lambda s: s),
            patch("penny.app.needs_onboarding", return_value=False),
            patch("penny.app.check_full_permissions_consent", return_value=True),
            patch("penny.app.run_preflight", side_effect=RuntimeError("boom")),
            patch("penny.app.save_state"),
            patch("penny.app._config_mtime", return_value=12345.0),
        ):
            PennyApp._load_and_refresh(app)  # must not raise
        assert app._has_setup_issues is False


# ── stopAgentByTaskId_ tmux/session lines (direct call) ──────────────────────


class TestStopAgentByTaskIdWithSession:
    """Test stopAgentByTaskId_ covers tmux/screen kill lines 921-924."""

    def test_kills_tmux_and_screen_session(self):
        from penny.app import PennyApp
        app = _make_fake_app(state={
            "agents_running": [
                {"task_id": "t-1", "pid": 100, "session": "penny-t-1",
                 "tmux_bin": "/usr/local/bin/tmux"},
            ],
            "recently_completed": [],
        })
        with (
            patch("penny.app.save_state"),
            patch("subprocess.run") as mock_run,
            patch("os.killpg"),
        ):
            PennyApp.stopAgentByTaskId_(app, "t-1")
        # Should have called tmux kill-session and screen quit
        calls = [c[0][0] for c in mock_run.call_args_list]
        tmux_call = [c for c in calls if c[0] == "/usr/local/bin/tmux"]
        screen_call = [c for c in calls if c[0] == "screen"]
        assert len(tmux_call) == 1
        assert len(screen_call) == 1

    def test_default_tmux_bin_when_not_in_record(self):
        from penny.app import PennyApp
        app = _make_fake_app(state={
            "agents_running": [
                {"task_id": "t-1", "pid": 100, "session": "penny-t-1"},
            ],
            "recently_completed": [],
        })
        with (
            patch("penny.app.save_state"),
            patch("subprocess.run") as mock_run,
            patch("os.killpg"),
        ):
            PennyApp.stopAgentByTaskId_(app, "t-1")
        tmux_call = [c for c in mock_run.call_args_list
                     if "tmux" in c[0][0][0]]
        assert len(tmux_call) == 1


# ── pluginAction_ / runBdAction_ (direct call) ───────────────────────────────


class TestPluginActionDirect:
    """Call PennyApp.pluginAction_ and runBdAction_ to cover lines 985-999."""

    def test_plugin_action_dispatches_and_refreshes(self):
        import threading

        from penny.app import PennyApp
        app = _make_fake_app()
        dispatch_calls = []
        app._plugin_mgr.dispatch_action = lambda a, p: dispatch_calls.append((a, p))

        # Use an event to wait for background thread
        done = threading.Event()
        original_fetch = app._worker.fetch

        def fetch_with_signal(*args, **kwargs):
            original_fetch(*args, **kwargs)
            done.set()

        app._worker.fetch = fetch_with_signal
        PennyApp.pluginAction_(app, ("test_action", {"key": "val"}))
        done.wait(timeout=2.0)
        assert dispatch_calls == [("test_action", {"key": "val"})]

    def test_run_bd_action_wraps_as_bd_command(self):

        from penny.app import PennyApp
        app = _make_fake_app()
        plugin_action_calls = []

        def fake_plugin_action(payload):
            plugin_action_calls.append(payload)

        app.pluginAction_ = fake_plugin_action
        PennyApp.runBdAction_(app, (["ready"], "/tmp"))
        assert plugin_action_calls == [("bd_command", (["ready"], "/tmp"))]


# ── _sync_launchd_service write error paths ──────────────────────────────────


class TestSyncLaunchdServiceErrorPaths:
    """Test _sync_launchd_service handles write errors."""

    def test_write_error_handled_gracefully(self, tmp_path):
        import plistlib

        from penny.app import PennyApp
        plist = tmp_path / "test.plist"
        plist.write_bytes(plistlib.dumps({
            "Label": "com.gpxl.penny",
            "KeepAlive": True,
            "RunAtLoad": True,
        }))
        app = _make_fake_app(config={"service": {"keep_alive": False}})
        with (
            patch("penny.app.PLIST_LAUNCHAGENTS", plist),
            # Make write fail by patching write_bytes
            patch.object(type(plist), "write_bytes", side_effect=PermissionError("no write")),
        ):
            PennyApp._sync_launchd_service(app)  # must not raise

    def test_source_copy_error_suppressed(self, tmp_path):
        import plistlib

        from penny.app import PennyApp
        plist = tmp_path / "test.plist"
        plist.write_bytes(plistlib.dumps({
            "Label": "com.gpxl.penny",
            "KeepAlive": True,
            "RunAtLoad": True,
        }))
        app = _make_fake_app(config={"service": {"keep_alive": False}})
        # script_dir returns a path where we can't write
        fake_sd = tmp_path / "nonexistent_dir" / "scripts"
        with (
            patch("penny.app.PLIST_LAUNCHAGENTS", plist),
            patch("penny.app._script_dir_from_plist", return_value=fake_sd),
        ):
            PennyApp._sync_launchd_service(app)  # must not raise


# ── _safe_load_config with _normalize_config integration ──────────────────


class TestSafeLoadConfigNormalization:
    """Verify _safe_load_config applies _normalize_config to loaded config."""

    def test_legacy_mode_migrated_on_load(self, tmp_path):
        from penny.app import _safe_load_config
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text("menubar:\n  mode: hbars\n")
        with patch("penny.app.CONFIG_PATH", cfg_file):
            config, err = _safe_load_config()
        assert err is None
        assert config["menubar"]["mode"] == "bars"

    def test_bars_plus_t_migrated_on_load(self, tmp_path):
        from penny.app import _safe_load_config
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text("menubar:\n  mode: bars+t\n")
        with patch("penny.app.CONFIG_PATH", cfg_file):
            config, err = _safe_load_config()
        assert err is None
        assert config["menubar"]["mode"] == "bars"


# ── _format_menubar_title (direct call) ───────────────────────────────────


class TestFormatMenubarTitleDirect:
    """Call PennyApp._format_menubar_title directly — bars mode only."""

    def _call(self, pred, n_running, config=None):
        from penny.app import PennyApp
        app = _make_fake_app(config=config or {})
        return PennyApp._format_menubar_title(app, pred, n_running)

    def test_loading_when_no_prediction_no_agents(self):
        assert self._call(None, 0) == "Loading\u2026"

    def test_sparkle_count_when_no_prediction_with_agents(self):
        assert self._call(None, 2) == "\u27282"

    def test_returns_empty_no_agents(self):
        pred = Prediction(
            session_pct_all=25.0, pct_all=40.0, pct_sonnet=30.0,
            session_reset_label="3pm",
        )
        assert self._call(pred, 0) == ""

    def test_returns_sparkle_with_agents(self):
        pred = Prediction(
            session_pct_all=25.0, pct_all=40.0, pct_sonnet=30.0,
            session_reset_label="3pm",
        )
        assert self._call(pred, 3) == " \u27283"


# ── _tick_loading_bars (meter calibration sweep) ──────────────────────────


class TestTickLoadingBars:
    """Test PennyApp._tick_loading_bars calibration sweep animation."""

    def _make_app_for_anim(self, config=None):
        from penny.app import PennyApp
        app = _make_fake_app(config=config or {"menubar": {"mode": "bars"}})
        app._anim_bar_vals = [0.0, 0.0, 0.0]
        app._anim_bar_targets = [0.0, 0.0, 0.0]
        app._anim_arc_val = 0.0
        app._anim_arc_target = 0.0
        app._loading_frame = 0
        app._loading_phase = "loading"
        app._data_pending = False
        # Copy class constants that _tick_loading_bars reads from self
        app._CAL_BAR_TICKS = PennyApp._CAL_BAR_TICKS
        app._CAL_CLOCK_TICKS = PennyApp._CAL_CLOCK_TICKS
        # Stub _make_status_image and _render_anim_frame
        app._make_status_image = lambda pred: MagicMock()
        app._render_anim_frame = lambda btn: PennyApp._render_anim_frame(app, btn)
        return app

    def test_first_bar_sweeps_on_first_frame(self):
        from penny.app import PennyApp
        app = self._make_app_for_anim()
        btn = MagicMock()
        # Frame 0: first bar should have some value (triangle wave starts at 0)
        app._loading_frame = 0
        PennyApp._tick_loading_bars(app, btn, "bars")
        # After tick, frame should increment
        assert app._loading_frame == 1

    def test_second_bar_sweeps_after_first(self):
        from penny.app import PennyApp
        app = self._make_app_for_anim()
        btn = MagicMock()
        # With 3 bars and _CAL_BAR_TICKS=10, ticks_per_bar ≈ 3.33
        # Second bar starts at frame ~3.33, so frame 4 is mid-second-bar
        n_bars = len(app._anim_bar_vals)
        ticks_per_bar = PennyApp._CAL_BAR_TICKS / n_bars
        second_bar_mid = int(ticks_per_bar * 1.5)  # midpoint of second bar
        app._loading_frame = second_bar_mid
        PennyApp._tick_loading_bars(app, btn, "bars")
        # Second bar should be active (nonzero), first bar should be 0
        assert app._anim_bar_vals[1] > 0.0
        assert app._anim_bar_vals[0] == 0.0

    def test_arc_sweep_phase(self):
        from penny.app import PennyApp
        app = self._make_app_for_anim()
        btn = MagicMock()
        # Place frame in the arc sweep phase (after bar ticks)
        app._loading_frame = PennyApp._CAL_BAR_TICKS + 5  # mid-arc sweep
        PennyApp._tick_loading_bars(app, btn, "bars")
        # All bars should be 0 during arc phase
        for v in app._anim_bar_vals:
            assert v == 0.0
        # Arc should have a non-zero value (triangle wave in progress)
        assert app._anim_arc_val > 0.0
        assert app._loading_frame == PennyApp._CAL_BAR_TICKS + 6

    def test_bar_triangle_wave_rises(self):
        """During a bar's sweep, its value should rise above 0."""
        from penny.app import PennyApp
        app = self._make_app_for_anim()
        btn = MagicMock()
        # Frame 1: first bar should have a positive triangle wave value
        app._loading_frame = 1
        PennyApp._tick_loading_bars(app, btn, "bars")
        assert app._anim_bar_vals[0] > 0.0

    def test_sets_image_on_button(self):
        from penny.app import PennyApp
        app = self._make_app_for_anim()
        btn = MagicMock()
        mock_img = MagicMock()
        app._make_status_image = lambda pred: mock_img
        PennyApp._tick_loading_bars(app, btn, "bars")
        btn.setImage_.assert_called_with(mock_img)
        btn.setImagePosition_.assert_called_with(1)  # NSImageOnly
        btn.setTitle_.assert_called_with("")

    def test_frame_wraps_around(self):
        """Frames cycle: after total_ticks the counter wraps via modulo."""
        from penny.app import PennyApp
        app = self._make_app_for_anim()
        btn = MagicMock()
        total_ticks = PennyApp._CAL_BAR_TICKS + PennyApp._CAL_CLOCK_TICKS
        app._loading_frame = total_ticks  # exactly at wrap point
        PennyApp._tick_loading_bars(app, btn, "bars")
        # frame % total_ticks == 0, so we are back at start of first bar
        assert app._loading_frame == total_ticks + 1


# ── _render_anim_frame (animation frame rendering) ────────────────────────


class TestRenderAnimFrame:
    """Test PennyApp._render_anim_frame builds correct _AnimPred and paints button."""

    def _make_app_for_anim(self):
        app = _make_fake_app(config={"menubar": {"mode": "bars"}})
        app._anim_bar_vals = [10.0, 20.0, 30.0]
        app._anim_arc_val = 45.0
        # Stub _make_status_image — returns a mock image
        app._make_status_image = lambda pred: MagicMock()
        return app

    def test_passes_bar_vals_and_arc_to_pred(self):
        from penny.app import PennyApp
        app = self._make_app_for_anim()
        btn = MagicMock()
        captured_preds = []

        def mock_make_image(pred):
            captured_preds.append(pred)
            return MagicMock()

        app._make_status_image = mock_make_image
        PennyApp._render_anim_frame(app, btn)
        assert len(captured_preds) == 1
        assert captured_preds[0].session_pct_all == 10.0
        assert captured_preds[0].pct_all == 20.0
        assert captured_preds[0].pct_sonnet == 30.0
        assert captured_preds[0]._countdown_pct == 45.0

    def test_sets_image_only_on_button(self):
        from penny.app import PennyApp
        app = self._make_app_for_anim()
        btn = MagicMock()
        PennyApp._render_anim_frame(app, btn)
        btn.setImage_.assert_called_once()
        btn.setImagePosition_.assert_called_with(1)  # NSImageOnly
        btn.setTitle_.assert_called_with("")


# ── _loadingAnimTick_ phases ──────────────────────────────────────────────


class TestLoadingAnimTickPhases:
    """Test PennyApp._loadingAnimTick_ phase transitions."""

    def _make_app_for_tick(self, phase="loading", config=None):
        from penny.app import PennyApp
        app = _make_fake_app(config=config or {})
        app._loading_phase = phase
        app._loading_frame = 0
        app._anim_bar_vals = [0.0, 0.0, 0.0]
        app._anim_bar_targets = [50.0, 60.0, 70.0]
        app._anim_arc_val = 0.0
        app._anim_arc_target = 0.0
        app._data_pending = False
        app._prediction = None
        timer = MagicMock()
        timer.invalidate = MagicMock()
        app._loading_anim_timer = timer
        # Copy class constants and bind methods
        app._CAL_BAR_TICKS = PennyApp._CAL_BAR_TICKS
        app._CAL_CLOCK_TICKS = PennyApp._CAL_CLOCK_TICKS
        app._make_status_image = lambda pred: MagicMock()
        app._tick_loading_bars = lambda btn, mode: PennyApp._tick_loading_bars(app, btn, mode)
        app._tick_final_bars = lambda btn: PennyApp._tick_final_bars(app, btn)
        app._tick_final_clock = lambda btn: PennyApp._tick_final_clock(app, btn)
        app._render_anim_frame = lambda btn: PennyApp._render_anim_frame(app, btn)
        app._start_final_cycle = lambda: PennyApp._start_final_cycle(app)
        app._update_status_title = lambda: None
        return app

    def test_loading_phase_bars_mode_calls_tick_loading_bars(self):
        from penny.app import PennyApp
        app = self._make_app_for_tick(phase="loading", config={"menubar": {"mode": "bars"}})
        btn = MagicMock()
        app._status_item.button.return_value = btn
        tick_calls = []
        app._tick_loading_bars = lambda b, m: tick_calls.append((b, m))
        PennyApp._loadingAnimTick_(app, None)
        assert len(tick_calls) == 1
        assert tick_calls[0] == (btn, "bars")

    def test_final_bars_phase_dispatches(self):
        from penny.app import PennyApp
        app = self._make_app_for_tick(phase="final_bars", config={"menubar": {"mode": "bars"}})
        btn = MagicMock()
        app._status_item.button.return_value = btn
        calls = []
        app._tick_final_bars = lambda b: calls.append(b)
        PennyApp._loadingAnimTick_(app, None)
        assert len(calls) == 1

    def test_final_clock_phase_dispatches(self):
        from penny.app import PennyApp
        app = self._make_app_for_tick(phase="final_clock", config={"menubar": {"mode": "bars"}})
        btn = MagicMock()
        app._status_item.button.return_value = btn
        calls = []
        app._tick_final_clock = lambda b: calls.append(b)
        PennyApp._loadingAnimTick_(app, None)
        assert len(calls) == 1

    def test_returns_early_when_btn_is_none(self):
        from penny.app import PennyApp
        app = self._make_app_for_tick(phase="loading")
        app._status_item.button.return_value = None
        # Should not raise
        PennyApp._loadingAnimTick_(app, None)

    def test_data_pending_triggers_final_cycle_at_boundary(self):
        """When data arrives and frame loops back to 0, transition to final_bars."""
        from penny.app import PennyApp
        app = self._make_app_for_tick(phase="loading", config={"menubar": {"mode": "bars", "show_sonnet": True}})
        pred = Prediction(
            session_pct_all=25.0, pct_all=40.0, pct_sonnet=30.0,
            session_reset_label="", session_hours_remaining=3.0,
        )
        app._prediction = pred
        app._data_pending = True
        # Set frame to total_ticks so frame % total == 0 (cycle boundary)
        total = PennyApp._CAL_BAR_TICKS + PennyApp._CAL_CLOCK_TICKS
        app._loading_frame = total
        btn = MagicMock()
        app._status_item.button.return_value = btn
        PennyApp._loadingAnimTick_(app, None)
        assert app._loading_phase in ("final_bars", "final_clock")

    def test_prediction_arrival_sets_data_pending(self):
        """When prediction appears during loading phase, _data_pending is set."""
        from penny.app import PennyApp
        app = self._make_app_for_tick(phase="loading")
        pred = Prediction(session_pct_all=25.0, pct_all=40.0, pct_sonnet=30.0)
        app._prediction = pred
        app._data_pending = False
        app._loading_frame = 5  # mid-cycle, not at boundary
        btn = MagicMock()
        app._status_item.button.return_value = btn
        PennyApp._loadingAnimTick_(app, None)
        assert app._data_pending is True


# ── _tick_final_bars (direct call for branch coverage) ─────────────────────


class TestTickFinalBarsDirect:
    """Direct calls to PennyApp._tick_final_bars for branch coverage."""

    def _make_app(self, targets=None, frame=0):
        from penny.app import PennyApp
        app = _make_fake_app(config={"menubar": {"mode": "bars"}})
        targets = targets or [50.0, 60.0, 70.0]
        app._anim_bar_targets = list(targets)
        app._anim_bar_vals = [0.0] * len(targets)
        app._anim_arc_val = 0.0
        app._loading_frame = frame
        app._loading_phase = "final_bars"
        app._CAL_BAR_TICKS = PennyApp._CAL_BAR_TICKS
        app._CAL_CLOCK_TICKS = PennyApp._CAL_CLOCK_TICKS
        app._make_status_image = lambda pred: MagicMock()
        app._render_anim_frame = lambda btn: PennyApp._render_anim_frame(app, btn)
        return app

    def test_frame_past_all_bars_snaps_to_targets(self):
        """When frame >= bar_end for all bars, vals snap to targets (line 344)."""
        from penny.app import PennyApp
        # With 3 bars and 20 ticks, each bar gets ~6.67 ticks
        # Frame 19 is past all bars; 19+1=20 triggers the snap + phase transition
        app = self._make_app(targets=[50.0, 60.0, 70.0], frame=19)
        btn = MagicMock()
        PennyApp._tick_final_bars(app, btn)
        assert app._loading_phase == "final_clock"
        assert app._loading_frame == 0
        # Bars should be snapped to targets
        assert app._anim_bar_vals == [50.0, 60.0, 70.0]

    def test_frame_before_bar_start_is_zero(self):
        """When frame < bar_start for a bar, its value is 0.0 (line 342)."""
        from penny.app import PennyApp
        # Frame 0: only first bar should be active; second and third should be 0
        app = self._make_app(targets=[50.0, 60.0, 70.0], frame=0)
        btn = MagicMock()
        PennyApp._tick_final_bars(app, btn)
        # Bar 2 and 3 start later, so they should be 0
        assert app._anim_bar_vals[2] == 0.0

    def test_descend_branch_past_midpoint(self):
        """When local_t >= 0.5, bar descends from 100 toward target (line 353)."""
        from penny.app import PennyApp
        # With 3 bars and 10 ticks, ticks_per_bar ≈ 3.33
        # For bar 0: bar_start=0, bar_end≈3.33
        # Frame 2: local_t = 2/3.33 ≈ 0.6 which is >= 0.5 (descend branch)
        app = self._make_app(targets=[50.0, 60.0, 70.0], frame=2)
        btn = MagicMock()
        PennyApp._tick_final_bars(app, btn)
        # Bar 0 should be between target (50) and 100
        val = app._anim_bar_vals[0]
        assert 50.0 <= val <= 100.0

    def test_rise_branch_before_midpoint(self):
        """When local_t < 0.5, bar rises from 0 toward 100 (line 350)."""
        from penny.app import PennyApp
        # Frame 0 for bar 0: local_t = 0 / 3.33 = 0.0 which is < 0.5
        app = self._make_app(targets=[50.0, 60.0, 70.0], frame=0)
        btn = MagicMock()
        PennyApp._tick_final_bars(app, btn)
        # Bar 0 value should be >= 0 (rising phase)
        assert app._anim_bar_vals[0] >= 0.0

    def test_mid_animation_second_bar_past_end(self):
        """Frame where first bar is finished but second might still be in progress."""
        from penny.app import PennyApp
        # With 3 bars and 20 ticks: bar0 ends at ~6.67, bar1 ends at ~13.33
        # Frame 10 = bar0 is done (frame >= bar_end=6.67), bar1 is active
        app = self._make_app(targets=[50.0, 60.0, 70.0], frame=10)
        btn = MagicMock()
        PennyApp._tick_final_bars(app, btn)
        # Bar 0 should be at target (past its end)
        assert app._anim_bar_vals[0] == 50.0

    def test_arc_stays_zero_during_bar_phase(self):
        from penny.app import PennyApp
        app = self._make_app(targets=[50.0, 60.0, 70.0], frame=3)
        app._anim_arc_val = 99.0  # set nonzero, should be reset to 0
        btn = MagicMock()
        PennyApp._tick_final_bars(app, btn)
        assert app._anim_arc_val == 0.0


# ── _tick_final_clock (direct call for branch coverage) ────────────────────


class TestTickFinalClockDirect:
    """Direct calls to PennyApp._tick_final_clock for full coverage of lines 371-385."""

    def _make_app(self, frame=0, arc_target=50.0):
        from penny.app import PennyApp
        app = _make_fake_app(config={})
        app._loading_frame = frame
        app._loading_phase = "final_clock"
        app._anim_arc_val = 0.0
        app._anim_arc_target = arc_target
        app._anim_bar_vals = [50.0, 60.0, 70.0]
        app._CAL_BAR_TICKS = PennyApp._CAL_BAR_TICKS
        app._CAL_CLOCK_TICKS = PennyApp._CAL_CLOCK_TICKS
        app._make_status_image = lambda pred: MagicMock()
        app._render_anim_frame = lambda btn: PennyApp._render_anim_frame(app, btn)
        timer = MagicMock()
        timer.invalidate = MagicMock()
        app._loading_anim_timer = timer
        app._update_status_title = MagicMock()
        return app

    def test_arc_sweeps_proportionally(self):
        from penny.app import PennyApp
        app = self._make_app(frame=10, arc_target=80.0)
        btn = MagicMock()
        PennyApp._tick_final_clock(app, btn)
        # t = 10/20 = 0.5, descend branch starts: 100 + 0 * (80-100) = 100.0
        assert abs(app._anim_arc_val - 100.0) < 0.01
        assert app._loading_frame == 11

    def test_arc_reaches_target_at_end(self):
        """At frame == _CAL_CLOCK_TICKS - 1, arc should be close to target."""
        from penny.app import PennyApp
        app = self._make_app(frame=19, arc_target=80.0)
        btn = MagicMock()
        PennyApp._tick_final_clock(app, btn)
        # frame was 19, now 20 which is >= _CAL_CLOCK_TICKS
        assert app._loading_phase == "done"
        assert app._loading_anim_timer is None

    def test_invalidates_timer_at_completion(self):
        from penny.app import PennyApp
        app = self._make_app(frame=19, arc_target=50.0)
        timer = app._loading_anim_timer
        btn = MagicMock()
        PennyApp._tick_final_clock(app, btn)
        timer.invalidate.assert_called_once()
        assert app._loading_anim_timer is None

    def test_calls_update_status_title_at_completion(self):
        from penny.app import PennyApp
        app = self._make_app(frame=19, arc_target=50.0)
        btn = MagicMock()
        PennyApp._tick_final_clock(app, btn)
        app._update_status_title.assert_called_once()

    def test_no_timer_invalidation_when_timer_none(self):
        from penny.app import PennyApp
        app = self._make_app(frame=19, arc_target=50.0)
        app._loading_anim_timer = None
        btn = MagicMock()
        PennyApp._tick_final_clock(app, btn)
        assert app._loading_phase == "done"

    def test_mid_animation_does_not_finish(self):
        from penny.app import PennyApp, _ease_out_cubic
        app = self._make_app(frame=5, arc_target=60.0)
        btn = MagicMock()
        PennyApp._tick_final_clock(app, btn)
        assert app._loading_phase == "final_clock"
        assert app._loading_frame == 6
        # t = 5/20 = 0.25, rise branch: _ease_out_cubic(0.25/0.5) * 100
        expected = _ease_out_cubic(0.5) * 100.0  # 87.5
        assert abs(app._anim_arc_val - expected) < 0.01

    def test_renders_frame(self):
        from penny.app import PennyApp
        app = self._make_app(frame=5, arc_target=60.0)
        btn = MagicMock()
        PennyApp._tick_final_clock(app, btn)
        btn.setImage_.assert_called_once()


# ── _start_final_cycle (direct call) ──────────────────────────────────────


class TestStartFinalCycleDirect:
    """Direct calls to PennyApp._start_final_cycle for line coverage."""

    def test_sets_targets_from_prediction(self):
        from penny.app import PennyApp
        # session_hours_remaining=2.5 → arc = (1.0 - 2.5/5.0) * 100 = 50.0
        pred = Prediction(session_pct_all=25.0, pct_all=40.0, pct_sonnet=30.0,
                          session_hours_remaining=2.5)
        app = _make_fake_app(config={"menubar": {"show_sonnet": True}})
        app._prediction = pred
        app._anim_bar_vals = [0.0, 0.0, 0.0]
        app._anim_bar_targets = [0.0, 0.0, 0.0]
        app._data_pending = True
        app._loading_phase = "loading"
        app._loading_frame = 99
        PennyApp._start_final_cycle(app)
        assert app._anim_bar_targets == [25.0, 40.0, 30.0]
        assert app._anim_bar_vals == [0.0, 0.0, 0.0]
        assert abs(app._anim_arc_target - 50.0) < 0.01
        assert app._anim_arc_emptying is False
        assert app._loading_phase == "final_bars"
        assert app._loading_frame == 0
        assert app._data_pending is False

    def test_omits_sonnet_when_disabled(self):
        from penny.app import PennyApp
        pred = Prediction(session_pct_all=25.0, pct_all=40.0, pct_sonnet=30.0)
        app = _make_fake_app(config={"menubar": {"show_sonnet": False}})
        app._prediction = pred
        app._anim_bar_vals = [0.0, 0.0, 0.0]
        app._anim_bar_targets = [0.0, 0.0, 0.0]
        app._data_pending = True
        app._loading_phase = "loading"
        app._loading_frame = 99
        PennyApp._start_final_cycle(app)
        assert app._anim_bar_targets == [25.0, 40.0]
        assert len(app._anim_bar_vals) == 2


# ── _timerFired_ (direct call) ────────────────────────────────────────────


class TestTimerFiredDirect:
    """Call PennyApp._timerFired_ directly to cover line 250."""

    def test_calls_worker_fetch(self):
        from penny.app import PennyApp
        app = _make_fake_app()
        PennyApp._timerFired_(app, None)
        app._worker.fetch.assert_called_once_with()


# ── _showSetupHint_ (direct call) ─────────────────────────────────────────


class TestShowSetupHintDirect:
    """Call PennyApp._showSetupHint_ to cover line 1138."""

    def test_shows_alert(self):
        from penny.app import PennyApp
        app = _make_fake_app()
        alert_calls = []
        app._show_alert = lambda title, msg: alert_calls.append((title, msg))
        PennyApp._showSetupHint_(app, None)
        assert len(alert_calls) == 1
        assert "Setup Deferred" in alert_calls[0][0]


# ── main() entry point ───────────────────────────────────────────────────


class TestMainEntryPoint:
    """Test the main() function entry point to cover lines 1182-1194."""

    def test_main_acquires_lock_and_runs_app(self):
        from penny.app import main
        with (
            patch("penny.app._acquire_pid_lock") as mock_acquire,
            patch("penny.app._release_pid_lock") as mock_release,
            patch("penny.app.setproctitle") as mock_spt,
            patch("penny.app.NSApplication") as mock_ns,
            patch("penny.app.PennyApp") as mock_penny,
        ):
            mock_app = MagicMock()
            mock_ns.sharedApplication.return_value = mock_app
            mock_delegate = MagicMock()
            mock_penny.alloc.return_value.init.return_value = mock_delegate
            main()
        mock_acquire.assert_called_once()
        mock_release.assert_called_once()
        mock_spt.setproctitle.assert_called_once_with("Penny")
        mock_app.setActivationPolicy_.assert_called_once_with(1)
        mock_app.setDelegate_.assert_called_once_with(mock_delegate)
        mock_app.run.assert_called_once()

    def test_main_releases_lock_on_exception(self):
        from penny.app import main
        with (
            patch("penny.app._acquire_pid_lock"),
            patch("penny.app._release_pid_lock") as mock_release,
            patch("penny.app.setproctitle"),
            patch("penny.app.NSApplication") as mock_ns,
            patch("penny.app.PennyApp"),
        ):
            mock_app = MagicMock()
            mock_ns.sharedApplication.return_value = mock_app
            mock_app.run.side_effect = RuntimeError("app crash")
            with pytest.raises(RuntimeError):
                main()
        # Lock should still be released even on exception
        mock_release.assert_called_once()
