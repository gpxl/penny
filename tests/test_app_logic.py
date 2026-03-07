"""Unit tests for extractable pure-Python logic in penny/app.py.

Tests _compact_reset_time, PID lock, _didFetchData_ callback logic,
and task/agent action methods — all without requiring a running AppKit event loop.
"""

from __future__ import annotations

import json
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
            sw = state.setdefault("spawned_this_week", [])
            if not any(a.get("task_id") == agent.get("task_id") for a in sw):
                sw.append(agent)
            rc = state.setdefault("recently_completed", [])
            if not any(a.get("task_id") == agent.get("task_id") for a in rc):
                rc.append(agent)
            state["recently_completed"] = rc[-20:]
            if agent.get("status") != "unknown" and notify_completion:
                notifications.append(agent["task_id"])
        return notifications

    def test_newly_done_added_to_spawned_this_week(self):
        state = {"spawned_this_week": [], "recently_completed": []}
        agent = {"task_id": "t-1", "title": "Fix", "project": "proj", "status": "completed"}
        self._process_newly_done(state, [agent])
        assert len(state["spawned_this_week"]) == 1
        assert state["spawned_this_week"][0]["task_id"] == "t-1"

    def test_newly_done_deduplicates(self):
        agent = {"task_id": "t-1", "title": "Fix", "project": "proj", "status": "completed"}
        state = {"spawned_this_week": [agent], "recently_completed": [agent]}
        self._process_newly_done(state, [agent])
        assert len(state["spawned_this_week"]) == 1
        assert len(state["recently_completed"]) == 1

    def test_recently_completed_capped_at_20(self):
        state = {
            "spawned_this_week": [],
            "recently_completed": [
                {"task_id": f"old-{i}", "status": "completed"} for i in range(20)
            ],
        }
        new_agent = {"task_id": "new-1", "status": "completed"}
        self._process_newly_done(state, [new_agent])
        assert len(state["recently_completed"]) == 20
        assert state["recently_completed"][-1]["task_id"] == "new-1"

    def test_unknown_status_skips_notification(self):
        state = {"spawned_this_week": [], "recently_completed": []}
        agent = {"task_id": "t-1", "status": "unknown"}
        notifs = self._process_newly_done(state, [agent])
        assert notifs == []

    def test_completed_status_sends_notification(self):
        state = {"spawned_this_week": [], "recently_completed": []}
        agent = {"task_id": "t-1", "status": "completed", "title": "Fix", "project": "proj"}
        notifs = self._process_newly_done(state, [agent])
        assert notifs == ["t-1"]

    def test_notifications_disabled(self):
        state = {"spawned_this_week": [], "recently_completed": []}
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
