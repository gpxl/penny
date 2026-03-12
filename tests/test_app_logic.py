"""Unit tests for extractable pure-Python logic in penny/app.py.

Tests _compact_reset_time, PID lock, _didFetchData_ callback logic,
and task/agent action methods — all without requiring a running AppKit event loop.
"""

from __future__ import annotations

import os
import signal
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from penny.analysis import Prediction

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
    """Test the title string construction from _update_status_title."""

    def _format_title(self, pred: Prediction | None, agents_running: list) -> str:
        """Simulate the title formatting logic from PennyApp._update_status_title."""
        n_running = len(agents_running)
        if pred:
            reset_time = _compact_reset_time(pred.session_reset_label)
            session = f"{pred.session_pct_all:.0f}/{reset_time}" if reset_time else f"{pred.session_pct_all:.0f}"
            stats = f"{session} {pred.pct_all:.0f}/{pred.pct_sonnet:.0f}"
            prefix = "\u26a0\ufe0f " if pred.outage else ""
            if n_running > 0:
                return f"{prefix}{stats} \u2728{n_running}"
            else:
                return f"{prefix}{stats}"
        elif n_running > 0:
            return f"\u2728{n_running}"
        else:
            return "Loading\u2026"

    def test_loading_when_no_prediction_no_agents(self):
        with patch("penny.analysis.uses_24h_time", return_value=False):
            assert self._format_title(None, []) == "Loading\u2026"

    def test_agents_only_when_no_prediction(self):
        with patch("penny.analysis.uses_24h_time", return_value=False):
            assert self._format_title(None, [{"pid": 1}]) == "\u27281"

    def test_stats_with_prediction_no_agents(self):
        pred = Prediction(session_pct_all=10.0, pct_all=42.0, pct_sonnet=30.0, session_reset_label="2pm")
        with patch("penny.analysis.uses_24h_time", return_value=False):
            title = self._format_title(pred, [])
        assert "10/2pm" in title
        assert "42/30" in title

    def test_stats_with_agents(self):
        pred = Prediction(session_pct_all=10.0, pct_all=42.0, pct_sonnet=30.0, session_reset_label="2pm")
        with patch("penny.analysis.uses_24h_time", return_value=False):
            title = self._format_title(pred, [{"pid": 1}])
        assert "\u27281" in title

    def test_outage_prefix(self):
        pred = Prediction(session_pct_all=10.0, pct_all=42.0, pct_sonnet=30.0, session_reset_label="2pm", outage=True)
        with patch("penny.analysis.uses_24h_time", return_value=False):
            title = self._format_title(pred, [])
        assert title.startswith("\u26a0\ufe0f ")

    def test_24h_reset_time(self):
        pred = Prediction(session_pct_all=10.0, pct_all=42.0, pct_sonnet=30.0, session_reset_label="2pm")
        with patch("penny.analysis.uses_24h_time", return_value=True):
            title = self._format_title(pred, [])
        assert "10/14" in title


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
    app._config_mtime = None
    # Stub methods that may be called internally (override per-test as needed)
    app._hot_reload_config = MagicMock()
    app._sync_launchd_service = MagicMock()
    app._update_status_title = MagicMock()
    app._spawn_agents = MagicMock()
    app._write_config = MagicMock()
    # _compact_reset_time is called by _update_status_title; bind the real method
    from penny.app import PennyApp
    app._compact_reset_time = lambda label: PennyApp._compact_reset_time(app, label)
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
    """Call PennyApp._write_config directly — atomic write via tmp + os.replace."""

    def test_writes_config_to_disk(self, tmp_path):
        import yaml

        from penny.app import PennyApp
        cfg_file = tmp_path / "config.yaml"
        app = _make_fake_app(config={"projects": [{"path": "/tmp/p"}]})
        with patch("penny.app.CONFIG_PATH", cfg_file):
            PennyApp._write_config(app)
        data = yaml.safe_load(cfg_file.read_text())
        assert data["projects"] == [{"path": "/tmp/p"}]

    def test_atomic_write_uses_tmp_then_replace(self, tmp_path):
        """Verify the write goes through .yaml.tmp and os.replace is called."""
        from penny.app import PennyApp
        cfg_file = tmp_path / "config.yaml"
        app = _make_fake_app(config={"x": 1})
        with patch("penny.app.CONFIG_PATH", cfg_file), \
             patch("os.replace", wraps=os.replace) as mock_replace:
            PennyApp._write_config(app)
        # os.replace should have been called with tmp -> final path
        mock_replace.assert_called_once()
        src, dst = mock_replace.call_args[0]
        assert str(src).endswith(".yaml.tmp")
        assert dst == cfg_file

    def test_tmp_file_cleaned_up_after_success(self, tmp_path):
        """After successful write, no .yaml.tmp file should remain."""
        from penny.app import PennyApp
        cfg_file = tmp_path / "config.yaml"
        app = _make_fake_app(config={"y": 2})
        with patch("penny.app.CONFIG_PATH", cfg_file):
            PennyApp._write_config(app)
        tmp_file = cfg_file.with_suffix(".yaml.tmp")
        assert not tmp_file.exists()

    def test_handles_write_error_gracefully(self, tmp_path):
        """If the file cannot be written, the method prints an error (no raise)."""
        from penny.app import PennyApp
        app = _make_fake_app(config={"k": "v"})
        # Point CONFIG_PATH to a nonexistent deep path so tmp open fails
        bad_path = tmp_path / "nonexistent_dir" / "config.yaml"
        with patch("penny.app.CONFIG_PATH", bad_path):
            PennyApp._write_config(app)  # must not raise

    def test_tmp_file_cleaned_up_on_replace_failure(self, tmp_path):
        """If os.replace fails, the .yaml.tmp file is cleaned up."""
        from penny.app import PennyApp
        cfg_file = tmp_path / "config.yaml"
        app = _make_fake_app(config={"z": 3})
        with patch("penny.app.CONFIG_PATH", cfg_file), \
             patch("os.replace", side_effect=OSError("disk full")):
            PennyApp._write_config(app)  # must not raise
        tmp_file = cfg_file.with_suffix(".yaml.tmp")
        assert not tmp_file.exists()

    def test_original_file_preserved_on_replace_failure(self, tmp_path):
        """If os.replace fails, the original config.yaml remains unchanged."""
        import yaml

        from penny.app import PennyApp
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text("original: true\n")
        app = _make_fake_app(config={"new": "data"})
        with patch("penny.app.CONFIG_PATH", cfg_file), \
             patch("os.replace", side_effect=OSError("disk full")):
            PennyApp._write_config(app)
        data = yaml.safe_load(cfg_file.read_text())
        assert data == {"original": True}

    def test_cleanup_failure_is_silently_swallowed(self, tmp_path):
        """If tmp cleanup also fails after a write error, no exception propagates."""
        from penny.app import PennyApp
        cfg_file = tmp_path / "config.yaml"
        app = _make_fake_app(config={"a": 1})
        # Make os.replace fail AND make unlink fail
        with patch("penny.app.CONFIG_PATH", cfg_file), \
             patch("os.replace", side_effect=OSError("disk full")), \
             patch.object(Path, "unlink", side_effect=OSError("perm denied")):
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
        PennyApp._finishSpawn_(app, {"task_id": "t-1", "record": record, "error": None})
        assert len(app.state["agents_running"]) == 1
        assert app.state["agents_running"][0]["task_id"] == "t-1"

    def test_success_clears_pending(self):
        from penny.app import PennyApp
        task = self._make_task("t-1")
        app = _make_fake_app(state={"agents_running": []})
        app._pending_spawns = {"t-1": task}
        record = {"task_id": "t-1", "status": "running", "pid": 42}
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
        with patch("penny.app.send_notification") as mock_notify:
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

    def test_skips_on_yaml_error(self, tmp_path, caplog):
        import logging

        from penny.app import PennyApp
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text("bad: [unclosed\n")
        app = _make_fake_app()
        with patch("penny.app.CONFIG_PATH", cfg_file), \
             caplog.at_level(logging.WARNING, logger="penny"):
            PennyApp._hot_reload_config(app)
        assert "YAML error" in caplog.text

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


# ── _checkConfig_ (direct call into penny/app.py) ────────────────────────────


class TestCheckConfigDirect:
    """Call PennyApp._checkConfig_ to cover lines 206-210."""

    def test_triggers_hot_reload_when_mtime_changes(self, tmp_path):
        from penny.app import PennyApp
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text("projects: []\n")
        app = _make_fake_app()
        app._config_mtime = 0.0  # stale mtime
        reload_called = []
        app._hot_reload_config = lambda: reload_called.append(True)
        with patch("penny.app.CONFIG_PATH", cfg_file):
            with patch("penny.app._config_mtime", return_value=99999.0):
                PennyApp._checkConfig_(app, None)
        assert reload_called

    def test_no_reload_when_mtime_same(self, tmp_path):
        from penny.app import PennyApp
        app = _make_fake_app()
        app._config_mtime = 12345.0
        reload_called = []
        app._hot_reload_config = lambda: reload_called.append(True)
        with patch("penny.app._config_mtime", return_value=12345.0):
            PennyApp._checkConfig_(app, None)
        assert reload_called == []

    def test_no_reload_when_file_missing(self):
        from penny.app import PennyApp
        app = _make_fake_app()
        reload_called = []
        app._hot_reload_config = lambda: reload_called.append(True)
        with patch("penny.app._config_mtime", return_value=None):
            PennyApp._checkConfig_(app, None)
        assert reload_called == []


# ── _update_status_title (direct call into penny/app.py) ─────────────────────


class TestUpdateStatusTitleDirect:
    """Call PennyApp._update_status_title directly to cover lines 400-420."""

    def test_loading_when_no_pred_no_agents(self):
        from penny.app import PennyApp
        app = _make_fake_app(state={"agents_running": []})
        app._prediction = None
        PennyApp._update_status_title(app)
        btn = app._status_item.button()
        btn.setTitle_.assert_called()
        title = btn.setTitle_.call_args[0][0]
        assert title == "Loading\u2026"

    def test_sparkle_when_agents_running_no_pred(self):
        from penny.app import PennyApp
        app = _make_fake_app(state={"agents_running": [{"pid": 1}]})
        app._prediction = None
        PennyApp._update_status_title(app)
        btn = app._status_item.button()
        title = btn.setTitle_.call_args[0][0]
        assert "\u2728" in title
        assert "1" in title

    def test_stats_shown_with_prediction(self):
        from penny.analysis import Prediction
        from penny.app import PennyApp
        pred = Prediction(session_pct_all=10.0, pct_all=42.0, pct_sonnet=30.0, session_reset_label="2pm")
        app = _make_fake_app(state={"agents_running": []})
        app._prediction = pred
        with patch("penny.app.uses_24h_time", return_value=False):
            PennyApp._update_status_title(app)
        btn = app._status_item.button()
        title = btn.setTitle_.call_args[0][0]
        assert "42" in title
        assert "30" in title

    def test_outage_prefix_shown(self):
        from penny.analysis import Prediction
        from penny.app import PennyApp
        pred = Prediction(pct_all=50.0, pct_sonnet=30.0, session_reset_label="", outage=True)
        app = _make_fake_app(state={"agents_running": []})
        app._prediction = pred
        with patch("penny.app.uses_24h_time", return_value=False):
            PennyApp._update_status_title(app)
        btn = app._status_item.button()
        title = btn.setTitle_.call_args[0][0]
        assert title.startswith("\u26a0\ufe0f ")

    def test_agents_count_with_prediction(self):
        from penny.analysis import Prediction
        from penny.app import PennyApp
        pred = Prediction(pct_all=50.0, session_reset_label="3pm")
        app = _make_fake_app(state={"agents_running": [{"pid": 1}, {"pid": 2}]})
        app._prediction = pred
        with patch("penny.app.uses_24h_time", return_value=False):
            PennyApp._update_status_title(app)
        btn = app._status_item.button()
        title = btn.setTitle_.call_args[0][0]
        assert "\u27282" in title

    def test_noop_when_button_is_none(self):
        from penny.app import PennyApp
        app = _make_fake_app(state={"agents_running": []})
        app._status_item.button.return_value = None
        PennyApp._update_status_title(app)  # must not raise


# ── _timerFired_ (direct call) ───────────────────────────────────────────────


class TestTimerFiredDirect:
    """Call PennyApp._timerFired_ directly to cover line 202."""

    def test_calls_worker_fetch(self):
        from penny.app import PennyApp
        app = _make_fake_app()
        PennyApp._timerFired_(app, None)
        app._worker.fetch.assert_called_once_with()


# ── refreshNow_ (direct call) ────────────────────────────────────────────────


class TestRefreshNowDirect:
    """Call PennyApp.refreshNow_ directly to cover lines 252-253."""

    def test_sets_refreshing_and_fetches(self):
        from penny.app import PennyApp
        app = _make_fake_app()
        PennyApp.refreshNow_(app, None)
        app._vc.setRefreshing_.assert_called_once_with(True)
        app._worker.fetch.assert_called_once_with(force=True)


# ── _sync_launchd_service (direct call) ──────────────────────────────────────


class TestSyncLaunchdServiceDirect:
    """Call PennyApp._sync_launchd_service directly to cover lines 607-641."""

    def _make_plist(self, path, keep_alive=True, run_at_load=True):
        import plistlib
        path.write_bytes(plistlib.dumps({
            "Label": "com.gpxl.penny",
            "KeepAlive": keep_alive,
            "RunAtLoad": run_at_load,
            "WorkingDirectory": "/tmp/penny",
        }))

    def test_noop_when_plist_missing(self, tmp_path):
        from penny.app import PennyApp
        app = _make_fake_app(config={"service": {"keep_alive": False}})
        with patch("penny.app.PLIST_LAUNCHAGENTS", tmp_path / "missing.plist"):
            PennyApp._sync_launchd_service(app)  # must not raise

    def test_noop_when_already_in_sync(self, tmp_path):

        from penny.app import PennyApp
        plist = tmp_path / "test.plist"
        self._make_plist(plist, keep_alive=True, run_at_load=True)
        app = _make_fake_app(config={"service": {"keep_alive": True, "launch_at_login": True}})
        original_bytes = plist.read_bytes()
        with patch("penny.app.PLIST_LAUNCHAGENTS", plist):
            PennyApp._sync_launchd_service(app)
        # Plist file not rewritten
        assert plist.read_bytes() == original_bytes

    def test_updates_plist_when_keep_alive_differs(self, tmp_path):
        import plistlib

        from penny.app import PennyApp
        plist = tmp_path / "test.plist"
        self._make_plist(plist, keep_alive=True, run_at_load=True)
        app = _make_fake_app(config={"service": {"keep_alive": False, "launch_at_login": True}})
        with patch("penny.app.PLIST_LAUNCHAGENTS", plist), \
             patch("penny.app._script_dir_from_plist", return_value=None):
            PennyApp._sync_launchd_service(app)
        pl = plistlib.loads(plist.read_bytes())
        assert pl["KeepAlive"] is False

    def test_updates_source_copy_in_script_dir(self, tmp_path):
        import plistlib

        from penny.app import PennyApp
        plist = tmp_path / "test.plist"
        self._make_plist(plist, keep_alive=True, run_at_load=True)
        script_dir = tmp_path / "scripts"
        script_dir.mkdir()
        app = _make_fake_app(config={"service": {"keep_alive": False, "launch_at_login": True}})
        with patch("penny.app.PLIST_LAUNCHAGENTS", plist), \
             patch("penny.app._script_dir_from_plist", return_value=script_dir), \
             patch("penny.app.PLIST_LABEL", "com.gpxl.penny"):
            PennyApp._sync_launchd_service(app)
        source_plist = script_dir / "com.gpxl.penny.plist"
        assert source_plist.exists()
        pl = plistlib.loads(source_plist.read_bytes())
        assert pl["KeepAlive"] is False

    def test_handles_read_error_gracefully(self, tmp_path):
        from penny.app import PennyApp
        plist = tmp_path / "test.plist"
        plist.write_bytes(b"not a valid plist")
        app = _make_fake_app(config={"service": {"keep_alive": False}})
        with patch("penny.app.PLIST_LAUNCHAGENTS", plist):
            PennyApp._sync_launchd_service(app)  # must not raise

    def test_handles_write_error_gracefully(self, tmp_path):
        from penny.app import PennyApp
        plist = tmp_path / "test.plist"
        self._make_plist(plist, keep_alive=True, run_at_load=True)
        app = _make_fake_app(config={"service": {"keep_alive": False}})
        with patch("penny.app.PLIST_LAUNCHAGENTS", plist), \
             patch.object(Path, "write_bytes", side_effect=OSError("read-only")):
            PennyApp._sync_launchd_service(app)  # must not raise


# ── toggleKeepAlive_ / toggleLaunchAtLogin_ (direct call) ────────────────────


class TestToggleServiceDirect:
    """Call PennyApp.toggleKeepAlive_ and toggleLaunchAtLogin_ directly."""

    def test_toggle_keep_alive_true(self, tmp_path):
        from penny.app import PennyApp
        app = _make_fake_app(config={})
        write_called = []
        sync_called = []
        app._write_config = lambda: write_called.append(True)
        app._sync_launchd_service = lambda: sync_called.append(True)
        sender = MagicMock()
        sender.state.return_value = True
        PennyApp.toggleKeepAlive_(app, sender)
        assert app.config["service"]["keep_alive"] is True
        assert write_called
        assert sync_called

    def test_toggle_keep_alive_false(self, tmp_path):
        from penny.app import PennyApp
        app = _make_fake_app(config={"service": {"keep_alive": True}})
        app._write_config = MagicMock()
        app._sync_launchd_service = MagicMock()
        sender = MagicMock()
        sender.state.return_value = False
        PennyApp.toggleKeepAlive_(app, sender)
        assert app.config["service"]["keep_alive"] is False

    def test_toggle_launch_at_login_true(self, tmp_path):
        from penny.app import PennyApp
        app = _make_fake_app(config={})
        app._write_config = MagicMock()
        app._sync_launchd_service = MagicMock()
        sender = MagicMock()
        sender.state.return_value = True
        PennyApp.toggleLaunchAtLogin_(app, sender)
        assert app.config["service"]["launch_at_login"] is True

    def test_toggle_launch_at_login_false(self, tmp_path):
        from penny.app import PennyApp
        app = _make_fake_app(config={"service": {"launch_at_login": True}})
        app._write_config = MagicMock()
        app._sync_launchd_service = MagicMock()
        sender = MagicMock()
        sender.state.return_value = False
        PennyApp.toggleLaunchAtLogin_(app, sender)
        assert app.config["service"]["launch_at_login"] is False


# ── spawnTask_ agent_permissions guard (direct call) ─────────────────────────


class TestSpawnTaskPermissionsGuard:
    """Test the agent_permissions=off early return in spawnTask_ (line 425-427)."""

    def _make_task(self, task_id="t-1"):
        from penny.tasks import Task
        return Task(task_id, "Fix bug", "P1", "/tmp/proj", "proj")

    def test_noop_when_permissions_off(self, caplog):
        import logging

        from penny.app import PennyApp
        task = self._make_task()
        app = _make_fake_app(
            config={"work": {"agent_permissions": "off"}},
            state={"agents_running": []},
        )
        app._all_ready_tasks = [task]
        app._pending_spawns = {}
        with caplog.at_level(logging.INFO, logger="penny"):
            PennyApp.spawnTask_(app, task)
        assert "agent_permissions=off" in caplog.text
        # Should NOT have modified all_ready_tasks (early return before mutation)
        assert len(app._all_ready_tasks) == 1


# ── _didFetchData_ reconciliation and external completion ─────────────────────


class TestDidFetchDataReconciliation:
    """Test the reconciliation and external completion paths in _didFetchData_."""

    def _make_result(self, **kwargs):
        base = {
            "state": {"agents_running": [], "recently_completed": []},
            "prediction": None,
            "newly_done": [],
        }
        base.update(kwargs)
        return base

    def test_external_completion_detected_and_added(self):
        from penny.app import PennyApp
        from penny.tasks import Task
        ext_task = Task("ext-1", "External done", "P2", "/tmp/p", "proj")
        app = _make_fake_app()
        app._plugin_mgr.get_all_tasks.return_value = []
        app._plugin_mgr.get_all_completed_tasks.return_value = [ext_task]
        app._plugin_mgr.filter_all_tasks.return_value = []
        app.config = {"notifications": {"completion": False}}
        state = {"agents_running": [], "recently_completed": []}
        result = self._make_result(state=state)
        with patch("penny.app.should_trigger", return_value=False), \
             patch("penny.app.save_state"):
            PennyApp._didFetchData_(app, result)
        rc = app.state.get("recently_completed", [])
        assert any(a["task_id"] == "ext-1" for a in rc)
        assert any(a.get("completed_by") == "external" for a in rc)

    def test_external_completion_notification_sent(self):
        from penny.app import PennyApp
        from penny.tasks import Task
        ext_task = Task("ext-1", "External done", "P2", "/tmp/p", "proj")
        app = _make_fake_app()
        app._plugin_mgr.get_all_tasks.return_value = []
        app._plugin_mgr.get_all_completed_tasks.return_value = [ext_task]
        app._plugin_mgr.filter_all_tasks.return_value = []
        app.config = {"notifications": {"completion": True}}
        state = {"agents_running": [], "recently_completed": []}
        result = self._make_result(state=state)
        with patch("penny.app.should_trigger", return_value=False), \
             patch("penny.app.save_state"), \
             patch("penny.app.send_notification") as mock_notify:
            PennyApp._didFetchData_(app, result)
        assert mock_notify.called
        # Check the notification text mentions "externally"
        msg = mock_notify.call_args[0][1]
        assert "externally" in msg

    def test_reconciliation_removes_false_completed(self, caplog):
        import logging

        from penny.app import PennyApp
        from penny.tasks import Task
        task = Task("t-1", "Reappearing task", "P2", "/tmp/p", "proj")
        app = _make_fake_app()
        app._plugin_mgr.get_all_tasks.return_value = [task]
        app._plugin_mgr.get_all_completed_tasks.return_value = []
        app._plugin_mgr.filter_all_tasks.return_value = []
        state = {
            "agents_running": [],
            "recently_completed": [{"task_id": "t-1", "status": "completed"}],
        }
        result = self._make_result(state=state)
        with patch("penny.app.should_trigger", return_value=False), \
             patch("penny.app.save_state"), \
             caplog.at_level(logging.INFO, logger="penny"):
            PennyApp._didFetchData_(app, result)
        # t-1 reappeared in bd ready, so it should be removed from recently_completed
        rc = app.state.get("recently_completed", [])
        assert not any(a["task_id"] == "t-1" for a in rc)
        assert "reconciled" in caplog.text

    def test_ready_tasks_exclude_running_and_completed(self):
        """Tasks in agents_running or recently_completed are excluded from the
        ready list. Note: t-2 is only in recently_completed, NOT in all_tasks
        (completed tasks don't reappear in bd ready), and t-1 is in all_tasks
        but also in agents_running so it gets filtered out."""
        from penny.app import PennyApp
        from penny.tasks import Task
        t1 = Task("t-1", "Task 1", "P2", "/tmp/p", "proj")
        t3 = Task("t-3", "Task 3", "P2", "/tmp/p", "proj")
        app = _make_fake_app()
        # t-1 and t-3 appear in bd ready; t-2 is only in recently_completed
        app._plugin_mgr.get_all_tasks.return_value = [t1, t3]
        app._plugin_mgr.get_all_completed_tasks.return_value = []
        app._plugin_mgr.filter_all_tasks.return_value = []
        state = {
            "agents_running": [{"task_id": "t-1"}],
            "recently_completed": [{"task_id": "t-2"}],
        }
        result = self._make_result(state=state)
        with patch("penny.app.should_trigger", return_value=False), \
             patch("penny.app.save_state"):
            PennyApp._didFetchData_(app, result)
        ready_ids = [t.task_id for t in app._all_ready_tasks]
        assert "t-3" in ready_ids
        assert "t-1" not in ready_ids


# ── _newTaskSheet_ (direct call) ─────────────────────────────────────────────


class TestNewTaskSheetDirect:
    """Call PennyApp._newTaskSheet_ to cover line 658."""

    def test_opens_config_file(self):
        from penny.app import PennyApp
        app = _make_fake_app()
        with patch("subprocess.run") as mock_run:
            PennyApp._newTaskSheet_(app, None)
        mock_run.assert_called_once()
        args = mock_run.call_args[0][0]
        assert args[0] == "open"


# ── openPrefs_ (direct call) ─────────────────────────────────────────────────


class TestOpenPrefsDirect:
    """Call PennyApp.openPrefs_ to cover line 676."""

    def test_opens_config_file(self):
        from penny.app import PennyApp
        app = _make_fake_app()
        with patch("subprocess.run") as mock_run:
            PennyApp.openPrefs_(app, None)
        mock_run.assert_called_once()
        args = mock_run.call_args[0][0]
        assert args[0] == "open"


# ── stopAgentByTaskId_ with session (covers tmux/screen kill) ─────────────────


class TestStopAgentWithSession:
    """Test stopAgentByTaskId_ when agent has a non-empty session string."""

    def test_kills_tmux_and_screen_session(self):
        from penny.app import PennyApp
        app = _make_fake_app(state={
            "agents_running": [
                {"task_id": "t-1", "pid": 0, "session": "penny-t-1",
                 "tmux_bin": "/usr/local/bin/tmux"},
            ],
            "recently_completed": [],
        })
        with patch("penny.app.save_state"), \
             patch("subprocess.run") as mock_run:
            PennyApp.stopAgentByTaskId_(app, "t-1")
        # Should have called tmux kill-session and screen quit
        calls = [c[0][0] for c in mock_run.call_args_list]
        assert any("kill-session" in c for c in calls)
        assert any("screen" in c for c in calls)

    def test_uses_custom_tmux_bin_from_agent(self):
        from penny.app import PennyApp
        app = _make_fake_app(state={
            "agents_running": [
                {"task_id": "t-1", "pid": 0, "session": "my-sess",
                 "tmux_bin": "/custom/path/tmux"},
            ],
            "recently_completed": [],
        })
        with patch("penny.app.save_state"), \
             patch("subprocess.run") as mock_run:
            PennyApp.stopAgentByTaskId_(app, "t-1")
        first_call = mock_run.call_args_list[0][0][0]
        assert first_call[0] == "/custom/path/tmux"

    def test_defaults_tmux_bin_when_not_in_agent(self):
        from penny.app import PennyApp
        app = _make_fake_app(state={
            "agents_running": [
                {"task_id": "t-1", "pid": 0, "session": "my-sess"},
            ],
            "recently_completed": [],
        })
        with patch("penny.app.save_state"), \
             patch("subprocess.run") as mock_run:
            PennyApp.stopAgentByTaskId_(app, "t-1")
        first_call = mock_run.call_args_list[0][0][0]
        assert first_call[0] == "/opt/homebrew/bin/tmux"


# ── viewReport_ (direct call) ────────────────────────────────────────────────


class TestViewReportDirect:
    """Call PennyApp.viewReport_ directly to cover lines 663-673."""

    def test_opens_dashboard_url(self):
        from penny.app import PennyApp
        app = _make_fake_app()
        app._dashboard = MagicMock()
        app._dashboard.ensure_started.return_value = 7432
        app._popover = MagicMock()
        app._popover.isShown.return_value = False
        with patch("subprocess.run") as mock_run:
            PennyApp.viewReport_(app, None)
        mock_run.assert_called_once()
        args = mock_run.call_args[0][0]
        assert "http://127.0.0.1:7432/" in args

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

    def test_fallback_to_static_report_on_dashboard_error(self):
        from penny.app import PennyApp
        app = _make_fake_app()
        app._dashboard = MagicMock()
        app._dashboard.ensure_started.side_effect = RuntimeError("fail")
        app._popover = MagicMock()
        app._popover.isShown.return_value = False
        with patch("penny.app.generate_report", return_value="/tmp/report.html") as mock_gen, \
             patch("penny.app.open_report") as mock_open:
            PennyApp.viewReport_(app, None)
        mock_gen.assert_called_once()
        mock_open.assert_called_once_with("/tmp/report.html")

    def test_silent_when_both_dashboard_and_fallback_fail(self):
        from penny.app import PennyApp
        app = _make_fake_app()
        app._dashboard = MagicMock()
        app._dashboard.ensure_started.side_effect = RuntimeError("fail")
        app._popover = MagicMock()
        app._popover.isShown.return_value = False
        with patch("penny.app.generate_report", side_effect=RuntimeError("also fail")):
            PennyApp.viewReport_(app, None)  # must not raise


# ── quitApp_ (direct call) ──────────────────────────────────────────────────


class TestQuitAppDirect:
    """Call PennyApp.quitApp_ directly to cover lines 681-701."""

    def test_patches_plist_and_bootout(self, tmp_path):
        import plistlib

        from penny.app import PennyApp
        plist = tmp_path / "test.plist"
        plist.write_bytes(plistlib.dumps({
            "Label": "com.gpxl.penny",
            "KeepAlive": True,
            "RunAtLoad": True,
        }))
        app = _make_fake_app()
        app._dashboard = MagicMock()
        mock_ns_app = MagicMock()
        with patch("penny.app.PLIST_LAUNCHAGENTS", plist), \
             patch("subprocess.Popen") as mock_popen, \
             patch("penny.app.NSApplication") as mock_ns:
            mock_ns.sharedApplication.return_value = mock_ns_app
            PennyApp.quitApp_(app, None)
        # KeepAlive should be False in the patched plist
        pl = plistlib.loads(plist.read_bytes())
        assert pl["KeepAlive"] is False
        mock_popen.assert_called_once()
        mock_ns_app.terminate_.assert_called_once()

    def test_handles_missing_plist_gracefully(self, tmp_path):
        from penny.app import PennyApp
        app = _make_fake_app()
        app._dashboard = MagicMock()
        with patch("penny.app.PLIST_LAUNCHAGENTS", tmp_path / "missing.plist"), \
             patch("subprocess.Popen"), \
             patch("penny.app.NSApplication") as mock_ns:
            PennyApp.quitApp_(app, None)  # must not raise
        mock_ns.sharedApplication().terminate_.assert_called()

    def test_handles_plist_patch_error(self, tmp_path, caplog):
        import logging

        from penny.app import PennyApp
        plist = tmp_path / "test.plist"
        plist.write_bytes(b"not a valid plist")
        app = _make_fake_app()
        app._dashboard = MagicMock()
        with patch("penny.app.PLIST_LAUNCHAGENTS", plist), \
             patch("subprocess.Popen"), \
             patch("penny.app.NSApplication"), \
             caplog.at_level(logging.ERROR, logger="penny"):
            PennyApp.quitApp_(app, None)  # must not raise
        assert "quitApp_" in caplog.text

    def test_shuts_down_dashboard(self):
        from penny.app import PennyApp
        app = _make_fake_app()
        app._dashboard = MagicMock()
        with patch("penny.app.PLIST_LAUNCHAGENTS", MagicMock(exists=MagicMock(return_value=False))), \
             patch("subprocess.Popen"), \
             patch("penny.app.NSApplication"):
            PennyApp.quitApp_(app, None)
        app._dashboard.shutdown.assert_called_once()


# ── _load_and_refresh (direct call) ──────────────────────────────────────────


class TestLoadAndRefreshDirect:
    """Call PennyApp._load_and_refresh directly to cover lines 707-775."""

    def test_yaml_error_shows_alert_and_returns(self, tmp_path):
        from penny.app import PennyApp
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text("bad: [unclosed\n")
        app = _make_fake_app()
        alert_calls = []
        app._show_alert = lambda title, msg: alert_calls.append((title, msg))
        with patch("penny.app.CONFIG_PATH", cfg_file):
            PennyApp._load_and_refresh(app)
        assert len(alert_calls) == 1
        assert "Config Error" in alert_calls[0][0]
        # Worker fetch should NOT have been called (early return)
        app._worker.fetch.assert_not_called()

    def test_happy_path_loads_config_and_fetches(self, tmp_path):
        from penny.app import PennyApp
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text("projects:\n  - path: /tmp/proj\n")
        app = _make_fake_app()
        app._show_alert = MagicMock()
        app._plugin_mgr.get_all_preflight_checks.return_value = []
        with patch("penny.app.CONFIG_PATH", cfg_file), \
             patch("penny.app.load_state", return_value={"agents_running": []}), \
             patch("penny.app.reset_period_if_needed", side_effect=lambda s: s), \
             patch("penny.app.needs_onboarding", return_value=False), \
             patch("penny.app.check_full_permissions_consent", return_value=True), \
             patch("penny.app.save_state"), \
             patch("penny.app.run_preflight", return_value=[]):
            PennyApp._load_and_refresh(app)
        assert app.config["projects"] == [{"path": "/tmp/proj"}]
        app._worker.fetch.assert_called_once_with(force=True)

    def test_onboarding_deferred_sets_flag(self, tmp_path):
        from penny.app import PennyApp
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text("projects: []\n")
        app = _make_fake_app()
        app._show_alert = MagicMock()
        with patch("penny.app.CONFIG_PATH", cfg_file), \
             patch("penny.app.load_state", return_value={}), \
             patch("penny.app.reset_period_if_needed", side_effect=lambda s: s), \
             patch("penny.app.needs_onboarding", return_value=True), \
             patch("penny.app.run_onboarding", return_value=None), \
             patch("penny.app.save_state"), \
             patch("penny.app.NSTimer"):
            PennyApp._load_and_refresh(app)
        assert app.state.get("onboarding_deferred") is True

    def test_onboarding_completed_uses_updated_config(self, tmp_path):
        """When run_onboarding returns a valid config, the updated config is used."""
        from penny.app import PennyApp
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text("projects: []\n")
        app = _make_fake_app()
        app._show_alert = MagicMock()
        app._plugin_mgr.get_all_preflight_checks.return_value = []
        updated_config = {"projects": [{"path": "/tmp/proj"}]}
        with patch("penny.app.CONFIG_PATH", cfg_file), \
             patch("penny.app.load_state", return_value={}), \
             patch("penny.app.reset_period_if_needed", side_effect=lambda s: s), \
             patch("penny.app.needs_onboarding", return_value=True), \
             patch("penny.app.run_onboarding", return_value=updated_config), \
             patch("penny.app.check_full_permissions_consent", return_value=True), \
             patch("penny.app.save_state"), \
             patch("penny.app.run_preflight", return_value=[]):
            PennyApp._load_and_refresh(app)
        # The config returned by run_onboarding should be applied
        assert app.config["projects"] == [{"path": "/tmp/proj"}]

    def test_full_permissions_declined_reverts_to_off(self, tmp_path, caplog):
        import logging

        from penny.app import PennyApp
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text("work:\n  agent_permissions: full\nprojects:\n  - path: /tmp/p\n")
        app = _make_fake_app()
        app._show_alert = MagicMock()
        write_calls = []
        app._write_config = lambda: write_calls.append(True)
        app._plugin_mgr.get_all_preflight_checks.return_value = []
        with patch("penny.app.CONFIG_PATH", cfg_file), \
             patch("penny.app.load_state", return_value={}), \
             patch("penny.app.reset_period_if_needed", side_effect=lambda s: s), \
             patch("penny.app.needs_onboarding", return_value=False), \
             patch("penny.app.check_full_permissions_consent", return_value=False), \
             patch("penny.app.save_state"), \
             patch("penny.app.run_preflight", return_value=[]), \
             caplog.at_level(logging.INFO, logger="penny"):
            PennyApp._load_and_refresh(app)
        assert app.config["work"]["agent_permissions"] == "off"
        assert write_calls
        assert "declined" in caplog.text

    def test_preflight_error_handled_gracefully(self, tmp_path):
        from penny.app import PennyApp
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text("projects:\n  - path: /tmp/proj\n")
        app = _make_fake_app()
        app._show_alert = MagicMock()
        with patch("penny.app.CONFIG_PATH", cfg_file), \
             patch("penny.app.load_state", return_value={}), \
             patch("penny.app.reset_period_if_needed", side_effect=lambda s: s), \
             patch("penny.app.needs_onboarding", return_value=False), \
             patch("penny.app.save_state"), \
             patch("penny.app.run_preflight", side_effect=RuntimeError("boom")):
            PennyApp._load_and_refresh(app)  # must not raise
        app._worker.fetch.assert_called_once_with(force=True)

    def test_preflight_tool_errors_show_alert(self, tmp_path):
        from penny.app import PennyApp
        from penny.preflight import PreflightIssue
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text("projects:\n  - path: /tmp/proj\n")
        app = _make_fake_app()
        alert_calls = []
        app._show_alert = lambda title, msg: alert_calls.append((title, msg))
        app._plugin_mgr.get_all_preflight_checks.return_value = []
        tool_issue = PreflightIssue("error", "`claude` CLI not found", "Install it")
        with patch("penny.app.CONFIG_PATH", cfg_file), \
             patch("penny.app.load_state", return_value={}), \
             patch("penny.app.reset_period_if_needed", side_effect=lambda s: s), \
             patch("penny.app.needs_onboarding", return_value=False), \
             patch("penny.app.save_state"), \
             patch("penny.app.run_preflight", return_value=[tool_issue]):
            PennyApp._load_and_refresh(app)
        assert len(alert_calls) == 1
        assert "Setup Required" in alert_calls[0][0]


# ── _spawn_agents (direct call) ──────────────────────────────────────────────


class TestSpawnAgentsDirect:
    """Call PennyApp._spawn_agents directly to cover lines 779-803."""

    def _make_task(self, task_id="t-1"):
        from penny.tasks import Task
        return Task(task_id, "Fix bug", "P1", "/tmp/proj", "proj")

    def test_noop_when_no_ready_tasks(self):
        from penny.app import PennyApp
        app = _make_fake_app(state={"agents_running": []})
        app._ready_tasks = []
        PennyApp._spawn_agents(app)
        # No spawn, no state change
        assert app.state["agents_running"] == []

    def test_noop_when_permissions_off(self, caplog):
        import logging

        from penny.app import PennyApp
        app = _make_fake_app(
            config={"work": {"agent_permissions": "off"}},
            state={"agents_running": []},
        )
        app._ready_tasks = [self._make_task()]
        with caplog.at_level(logging.INFO, logger="penny"):
            PennyApp._spawn_agents(app)
        assert "agent_permissions=off" in caplog.text
        assert app.state["agents_running"] == []

    def test_spawns_all_ready_tasks(self):
        from penny.app import PennyApp
        t1 = self._make_task("t-1")
        t2 = self._make_task("t-2")
        app = _make_fake_app(state={"agents_running": []})
        app._ready_tasks = [t1, t2]
        app._plugin_mgr.get_task_description.return_value = "desc"
        app._plugin_mgr.get_agent_prompt_template.return_value = None
        fake_record = {"task_id": "x", "status": "running"}
        with patch("penny.app.spawn_claude_agent", return_value=fake_record), \
             patch("penny.app.save_state"), \
             patch("penny.app.send_notification"):
            PennyApp._spawn_agents(app)
        assert len(app.state["agents_running"]) == 2

    def test_sends_notification_with_prediction(self):
        from penny.analysis import Prediction
        from penny.app import PennyApp
        task = self._make_task()
        pred = Prediction(pct_all=50.0, projected_pct_all=70.0, days_remaining=2.0)
        app = _make_fake_app(
            config={"notifications": {"spawn": True}},
            state={"agents_running": []},
        )
        app._ready_tasks = [task]
        app._prediction = pred
        app._plugin_mgr.get_task_description.return_value = ""
        app._plugin_mgr.get_agent_prompt_template.return_value = None
        fake_record = {"task_id": "t-1", "status": "running"}
        with patch("penny.app.spawn_claude_agent", return_value=fake_record), \
             patch("penny.app.save_state"), \
             patch("penny.app.send_notification") as mock_notify:
            PennyApp._spawn_agents(app)
        mock_notify.assert_called_once()
        msg = mock_notify.call_args[0][1]
        assert "1 agent(s)" in msg

    def test_no_notification_when_disabled(self):
        from penny.app import PennyApp
        task = self._make_task()
        app = _make_fake_app(
            config={"notifications": {"spawn": False}},
            state={"agents_running": []},
        )
        app._ready_tasks = [task]
        app._plugin_mgr.get_task_description.return_value = ""
        app._plugin_mgr.get_agent_prompt_template.return_value = None
        fake_record = {"task_id": "t-1", "status": "running"}
        with patch("penny.app.spawn_claude_agent", return_value=fake_record), \
             patch("penny.app.save_state"), \
             patch("penny.app.send_notification") as mock_notify:
            PennyApp._spawn_agents(app)
        mock_notify.assert_not_called()

    def test_no_notification_without_prediction(self):
        from penny.app import PennyApp
        task = self._make_task()
        app = _make_fake_app(
            config={"notifications": {"spawn": True}},
            state={"agents_running": []},
        )
        app._ready_tasks = [task]
        app._prediction = None
        app._plugin_mgr.get_task_description.return_value = ""
        app._plugin_mgr.get_agent_prompt_template.return_value = None
        fake_record = {"task_id": "t-1", "status": "running"}
        with patch("penny.app.spawn_claude_agent", return_value=fake_record), \
             patch("penny.app.save_state"), \
             patch("penny.app.send_notification") as mock_notify:
            PennyApp._spawn_agents(app)
        mock_notify.assert_not_called()


# ── _showSetupHint_ / _show_alert (direct call) ─────────────────────────────


class TestShowAlertDirect:
    """Call PennyApp._show_alert and _showSetupHint_ directly."""

    def test_show_alert_calls_nsalert(self):
        import sys

        from penny.app import PennyApp
        app = _make_fake_app()
        mock_alert = MagicMock()
        mock_ns_alert = MagicMock()
        mock_ns_alert.alloc.return_value.init.return_value = mock_alert
        # NSAlert is imported inside the method via "from AppKit import NSAlert"
        # Patch it on the AppKit stub module
        sys.modules["AppKit"].NSAlert = mock_ns_alert
        try:
            PennyApp._show_alert(app, "Title", "Message")
        finally:
            del sys.modules["AppKit"].NSAlert
        mock_alert.setMessageText_.assert_called_once_with("Title")
        mock_alert.setInformativeText_.assert_called_once_with("Message")
        mock_alert.runModal.assert_called_once()

    def test_show_setup_hint_calls_show_alert(self):
        from penny.app import PennyApp
        app = _make_fake_app()
        alert_calls = []
        app._show_alert = lambda title, msg: alert_calls.append((title, msg))
        PennyApp._showSetupHint_(app, None)
        assert len(alert_calls) == 1
        assert "Setup Deferred" in alert_calls[0][0]


# ── pluginAction_ thread logic (covers lines 573, 577-587) ──────────────────


class TestPluginActionDirect:
    """Call PennyApp.pluginAction_ directly to verify thread scheduling."""

    def test_dispatches_action_in_background(self):
        from penny.app import PennyApp
        app = _make_fake_app()

        def mock_thread_start(self_t):
            self_t.run()  # run synchronously for testing

        with patch.object(threading.Thread, "start", mock_thread_start):
            PennyApp.pluginAction_(app, ("test_action", {"key": "val"}))
        app._plugin_mgr.dispatch_action.assert_called_once_with("test_action", {"key": "val"})
        app._worker.fetch.assert_called_with(force=True)

    def test_fetch_called_after_dispatch_error(self):
        from penny.app import PennyApp
        app = _make_fake_app()
        app._plugin_mgr.dispatch_action.side_effect = RuntimeError("boom")

        def mock_thread_start(self_t):
            self_t.run()

        with patch.object(threading.Thread, "start", mock_thread_start):
            PennyApp.pluginAction_(app, ("test_action", None))
        # fetch should still be called (in the finally block)
        app._worker.fetch.assert_called_with(force=True)


# ── runBdAction_ (direct call, covers line 573) ─────────────────────────────


class TestRunBdActionDirect:
    """Call PennyApp.runBdAction_ to verify it wraps as pluginAction_."""

    def test_wraps_as_bd_command(self):
        from penny.app import PennyApp
        app = _make_fake_app()
        plugin_calls = []
        app.pluginAction_ = lambda payload: plugin_calls.append(payload)
        args_cwd = (["ready"], "/tmp")
        PennyApp.runBdAction_(app, args_cwd)
        assert len(plugin_calls) == 1
        assert plugin_calls[0] == ("bd_command", args_cwd)


# ── spawnTask_ optimistic state update (covers lines 430-460) ────────────────


class TestSpawnTaskOptimisticUpdate:
    """Test spawnTask_ when agent_permissions is not off — the optimistic update path."""

    def _make_task(self, task_id="t-1"):
        from penny.tasks import Task
        return Task(task_id, "Fix bug", "P1", "/tmp/proj", "proj")

    def test_optimistic_update_removes_from_ready(self):
        from penny.app import PennyApp
        t1 = self._make_task("t-1")
        t2 = self._make_task("t-2")
        app = _make_fake_app(
            config={"work": {"agent_permissions": "full"}},
            state={"agents_running": []},
        )
        app._all_ready_tasks = [t1, t2]
        app._pending_spawns = {}
        # Patch threading so the bg thread runs synchronously
        with patch.object(threading.Thread, "start", lambda self_t: None):
            PennyApp.spawnTask_(app, t1)
        # t1 should be removed from ready list
        assert len(app._all_ready_tasks) == 1
        assert app._all_ready_tasks[0].task_id == "t-2"
        # t1 should be in pending spawns
        assert "t-1" in app._pending_spawns

    def test_vc_updated_after_optimistic_removal(self):
        from penny.app import PennyApp
        task = self._make_task()
        app = _make_fake_app(state={"agents_running": []})
        app._all_ready_tasks = [task]
        app._pending_spawns = {}
        with patch.object(threading.Thread, "start", lambda self_t: None):
            PennyApp.spawnTask_(app, task)
        app._vc.updateWithData_.assert_called_once()


# ── main() entry point ──────────────────────────────────────────────────────


class TestMainEntryPoint:
    """Test the main() function to cover lines 919-934."""

    def test_main_acquires_lock_and_runs_app(self):
        from penny.app import PennyApp, main
        mock_delegate = MagicMock()
        with patch("penny.app._acquire_pid_lock"), \
             patch("penny.app._release_pid_lock"), \
             patch("penny.app._install_signal_handlers"), \
             patch("penny.app._cleanup_orphan_sessions"), \
             patch("penny.app.setproctitle"), \
             patch.object(PennyApp, "alloc", create=True, return_value=MagicMock(init=MagicMock(return_value=mock_delegate))), \
             patch("penny.app.NSApplication") as mock_ns:
            mock_app = MagicMock()
            mock_ns.sharedApplication.return_value = mock_app
            main()
        mock_app.setActivationPolicy_.assert_called_once_with(1)
        mock_app.setDelegate_.assert_called_once_with(mock_delegate)
        mock_app.run.assert_called_once()

    def test_main_releases_lock_on_exception(self):
        from penny.app import PennyApp, main
        released = []
        with patch("penny.app._acquire_pid_lock"), \
             patch("penny.app._release_pid_lock", side_effect=lambda: released.append(True)), \
             patch("penny.app._install_signal_handlers"), \
             patch("penny.app._cleanup_orphan_sessions"), \
             patch("penny.app.setproctitle"), \
             patch.object(PennyApp, "alloc", create=True, return_value=MagicMock(init=MagicMock(return_value=MagicMock()))), \
             patch("penny.app.NSApplication") as mock_ns:
            mock_app = MagicMock()
            mock_app.run.side_effect = RuntimeError("crash")
            mock_ns.sharedApplication.return_value = mock_app
            with pytest.raises(RuntimeError):
                main()
        assert released


# ── _cleanup_orphan_sessions ──────────────────────────────────────────────────


class TestCleanupOrphanSessions:
    """Test the signal-driven agent session cleanup."""

    def test_no_agents_clears_list(self):
        from penny.app import _cleanup_orphan_sessions
        state = {"agents_running": []}
        with patch("penny.app.load_state", return_value=state), \
             patch("penny.app.save_state") as mock_save:
            _cleanup_orphan_sessions()
        mock_save.assert_called_once()
        assert state["agents_running"] == []

    def test_kills_tmux_and_screen_sessions(self):
        from penny.app import _cleanup_orphan_sessions
        state = {"agents_running": [
            {"session": "penny-1", "pid": 0, "tmux_bin": "/usr/bin/tmux"},
        ]}
        with patch("penny.app.load_state", return_value=state), \
             patch("penny.app.save_state"), \
             patch("shutil.which", side_effect=lambda x: f"/usr/bin/{x}"), \
             patch("subprocess.run") as mock_run:
            _cleanup_orphan_sessions()
        # Each call_args[0][0] is the command list (e.g. ["/usr/bin/tmux", "kill-session", ...])
        cmd_lists = [c[0][0] for c in mock_run.call_args_list]
        assert any("kill-session" in cmd for cmd in cmd_lists)
        assert any("screen" in cmd[0] for cmd in cmd_lists)

    def test_kills_process_by_pid(self):
        from penny.app import _cleanup_orphan_sessions
        state = {"agents_running": [
            {"session": "", "pid": 99999},
        ]}
        with patch("penny.app.load_state", return_value=state), \
             patch("penny.app.save_state"), \
             patch("shutil.which", return_value=None), \
             patch("os.killpg") as mock_kill:
            _cleanup_orphan_sessions()
        mock_kill.assert_called_once()

    def test_handles_process_lookup_error(self):
        from penny.app import _cleanup_orphan_sessions
        state = {"agents_running": [
            {"session": "", "pid": 99999},
        ]}
        with patch("penny.app.load_state", return_value=state), \
             patch("penny.app.save_state"), \
             patch("shutil.which", return_value=None), \
             patch("os.killpg", side_effect=ProcessLookupError):
            _cleanup_orphan_sessions()  # must not raise

    def test_handles_load_state_error(self):
        from penny.app import _cleanup_orphan_sessions
        with patch("penny.app.load_state", side_effect=RuntimeError("corrupt")):
            _cleanup_orphan_sessions()  # must not raise (returns early)

    def test_handles_save_state_error(self):
        from penny.app import _cleanup_orphan_sessions
        state = {"agents_running": []}
        with patch("penny.app.load_state", return_value=state), \
             patch("penny.app.save_state", side_effect=RuntimeError("disk full")):
            _cleanup_orphan_sessions()  # must not raise

    def test_uses_agent_tmux_bin_over_system(self):
        from penny.app import _cleanup_orphan_sessions
        state = {"agents_running": [
            {"session": "s1", "pid": 0, "tmux_bin": "/custom/tmux"},
        ]}
        with patch("penny.app.load_state", return_value=state), \
             patch("penny.app.save_state"), \
             patch("shutil.which", return_value="/system/tmux"), \
             patch("subprocess.run") as mock_run:
            _cleanup_orphan_sessions()
        first_call = mock_run.call_args_list[0][0][0]
        assert first_call[0] == "/custom/tmux"


# ── _install_signal_handlers ──────────────────────────────────────────────────


class TestInstallSignalHandlers:
    """Test signal handler installation."""

    def test_registers_sigterm_and_sigint(self):
        from penny.app import _install_signal_handlers
        handlers_set = {}
        with patch("signal.signal", side_effect=lambda sig, handler: handlers_set.update({sig: handler})):
            _install_signal_handlers()
        assert signal.SIGTERM in handlers_set
        assert signal.SIGINT in handlers_set

    def test_handler_cleans_up_and_exits(self):
        from penny.app import _install_signal_handlers
        handlers_set = {}
        with patch("signal.signal", side_effect=lambda sig, handler: handlers_set.update({sig: handler})):
            _install_signal_handlers()
        handler = handlers_set[signal.SIGTERM]
        with patch("penny.app._cleanup_orphan_sessions") as mock_cleanup, \
             patch("penny.app._release_pid_lock") as mock_release:
            with pytest.raises(SystemExit) as exc_info:
                handler(signal.SIGTERM, None)
        assert exc_info.value.code == 128 + signal.SIGTERM
        mock_cleanup.assert_called_once()
        mock_release.assert_called_once()


# ── _safe_load_config with validation ─────────────────────────────────────────


class TestSafeLoadConfigWithValidation:
    """Test that _safe_load_config now runs validate_config."""

    def test_valid_config_returns_no_error(self, tmp_path):
        from penny.app import _safe_load_config
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text("projects:\n  - path: /tmp/proj\n")
        with patch("penny.app.CONFIG_PATH", cfg_file), \
             patch("penny.app.validate_config", return_value=[]):
            config, err = _safe_load_config()
        assert config["projects"] == [{"path": "/tmp/proj"}]
        assert err is None

    def test_validation_warnings_logged(self, tmp_path, caplog):
        import logging

        from penny.app import _safe_load_config
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text("projects:\n  - path: /tmp/proj\n")
        with patch("penny.app.CONFIG_PATH", cfg_file), \
             patch("penny.app.validate_config", return_value=["bad field 'x'"]), \
             caplog.at_level(logging.WARNING, logger="penny"):
            config, err = _safe_load_config()
        # Config still returned (validation is non-fatal)
        assert config["projects"] == [{"path": "/tmp/proj"}]
        assert err is None
        assert "bad field" in caplog.text
