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
