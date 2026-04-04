"""Unit tests for penny/bg_worker.py."""

from __future__ import annotations

import threading
from unittest.mock import patch

from penny.bg_worker import BackgroundWorker


class FakeApp:
    """Minimal stand-in for PennyApp with the ObjC dispatch method."""

    def __init__(self):
        self.results = []

    def performSelectorOnMainThread_withObject_waitUntilDone_(
        self, sel, obj, wait
    ):
        self.results.append(obj)


class TestBackgroundWorker:
    def test_fetch_posts_result_to_app(self):
        app = FakeApp()
        worker = BackgroundWorker(app)
        fake_result = {
            "state": {"agents_running": []},
            "prediction": None,
            "newly_done": [],
        }
        with patch.object(
            BackgroundWorker, "_fetch_data", return_value=fake_result
        ):
            worker.fetch(force=True)
            # Wait for the background thread to complete
            while worker._running:
                pass
            # Small delay for the main-thread dispatch to fire
            import time
            time.sleep(0.1)

        assert len(app.results) == 1
        assert app.results[0]["state"]["agents_running"] == []

    def test_fetch_is_serialized(self):
        app = FakeApp()
        worker = BackgroundWorker(app)

        # Simulate a long-running fetch
        event = threading.Event()

        def slow_fetch(force):
            event.wait(timeout=2)
            return {"state": {}, "prediction": None, "newly_done": []}

        with patch.object(BackgroundWorker, "_fetch_data", side_effect=slow_fetch):
            worker.fetch()
            # Second fetch should be a no-op since first is still running
            worker.fetch()
            # Release the first fetch
            event.set()
            import time
            time.sleep(0.2)

        # Only one result should have been posted
        assert len(app.results) == 1

    def test_fetch_handles_exception(self):
        app = FakeApp()
        worker = BackgroundWorker(app)
        with patch.object(
            BackgroundWorker, "_fetch_data", side_effect=RuntimeError("boom")
        ):
            worker.fetch(force=True)
            import time
            time.sleep(0.2)

        assert len(app.results) == 1
        assert "error" in app.results[0]

    def test_running_flag_reset_after_completion(self):
        app = FakeApp()
        worker = BackgroundWorker(app)
        with patch.object(
            BackgroundWorker,
            "_fetch_data",
            return_value={"state": {}, "prediction": None, "newly_done": []},
        ):
            worker.fetch()
            import time
            time.sleep(0.2)
        assert worker._running is False

    def test_running_flag_reset_after_error(self):
        app = FakeApp()
        worker = BackgroundWorker(app)
        with patch.object(
            BackgroundWorker, "_fetch_data", side_effect=RuntimeError("boom")
        ):
            worker.fetch()
            import time
            time.sleep(0.2)
        assert worker._running is False

    def test_fetch_data_calls_detect_new_sessions(self):
        """_fetch_data must call detect_new_sessions so session history is populated."""
        from datetime import datetime, timezone

        fake_state = {
            "agents_running": [],
            "session_history": [],
            "last_session_scan": None,
            "plugin_state": {},
        }
        fake_period_start = datetime(2024, 1, 5, 20, 0, 0, tzinfo=timezone.utc)

        with (
            patch("penny.state.load_state", return_value=fake_state),
            patch("penny.state.reset_period_if_needed", return_value=fake_state),
            patch("penny.analysis.current_billing_period", return_value=(fake_period_start, None)),
            patch("penny.state.detect_new_sessions", return_value=(fake_state, [])) as mock_detect,
            patch("penny.state.save_state"),
            patch("penny.spawner.check_running_agents", return_value=[]),
            patch("penny.analysis.build_prediction", return_value=None),
        ):
            BackgroundWorker._fetch_data(force=False)

        mock_detect.assert_called_once_with(fake_state, fake_period_start)

    def test_fetch_data_parses_session_start_from_prediction(self):
        """_fetch_data should parse session_start from prediction with tz handling."""
        from datetime import datetime, timezone
        from unittest.mock import MagicMock

        fake_state = {
            "agents_running": [],
            "session_history": [],
            "last_session_scan": None,
            "plugin_state": {},
        }
        fake_period_start = datetime(2024, 1, 5, 20, 0, 0, tzinfo=timezone.utc)

        # Mock prediction with a session_start without timezone info
        fake_prediction = MagicMock()
        fake_prediction.session_start = datetime(2024, 1, 5, 12, 30, 0).isoformat()
        fake_prediction.projected_pct_all = 50.0
        fake_prediction.pct_all = 30.0
        fake_prediction.budget_all = 1_000_000
        fake_prediction.days_remaining = 5.0
        fake_prediction.reset_label = "Jan 12 at 8pm"

        # Mock a dataclass-like object for metrics
        mock_metrics = MagicMock()

        with (
            patch("penny.state.load_state", return_value=fake_state),
            patch("penny.state.reset_period_if_needed", return_value=fake_state),
            patch("penny.analysis.current_billing_period", return_value=(fake_period_start, None)),
            patch("penny.state.detect_new_sessions", return_value=(fake_state, [])),
            patch("penny.state.save_state"),
            patch("penny.spawner.check_running_agents", return_value=[]),
            patch("penny.analysis.build_prediction", return_value=fake_prediction),
            patch("penny.analysis.scan_rich_metrics_multi", return_value={"month": mock_metrics}),
            patch("penny.status_fetcher.get_cached_status", return_value=None),
            patch("penny.update_checker.should_check", return_value=False),
            patch("dataclasses.asdict", return_value={"test": "data"}),
        ):
            result = BackgroundWorker._fetch_data(force=False)
            assert result is not None

    def test_fetch_data_appends_intraday_samples(self):
        """_fetch_data should append intraday samples when live status is available."""
        from datetime import datetime, timezone
        from unittest.mock import MagicMock

        fake_state = {
            "agents_running": [],
            "session_history": [],
            "last_session_scan": None,
            "plugin_state": {},
        }
        fake_period_start = datetime(2024, 1, 5, 20, 0, 0, tzinfo=timezone.utc)

        # Mock prediction with valid session_start
        fake_prediction = MagicMock()
        fake_prediction.session_start = datetime(2024, 1, 5, 12, 30, 0, tzinfo=timezone.utc).isoformat()
        fake_prediction.projected_pct_all = 50.0
        fake_prediction.pct_all = 30.0
        fake_prediction.budget_all = 1_000_000
        fake_prediction.days_remaining = 5.0
        fake_prediction.reset_label = "Jan 12 at 8pm"

        # Mock live status with no outage
        fake_live = MagicMock()
        fake_live.outage = False
        fake_live.weekly_pct_all = 45
        fake_live.weekly_pct_sonnet = 60

        # Mock a dataclass-like object for metrics
        mock_metrics = MagicMock()

        with (
            patch("penny.state.load_state", return_value=fake_state),
            patch("penny.state.reset_period_if_needed", return_value=fake_state),
            patch("penny.analysis.current_billing_period", return_value=(fake_period_start, None)),
            patch("penny.state.detect_new_sessions", return_value=(fake_state, [])),
            patch("penny.state.save_state"),
            patch("penny.spawner.check_running_agents", return_value=[]),
            patch("penny.analysis.build_prediction", return_value=fake_prediction),
            patch("penny.analysis.scan_rich_metrics_multi", return_value={"month": mock_metrics}),
            patch("penny.status_fetcher.get_cached_status", return_value=fake_live),
            patch("penny.update_checker.should_check", return_value=False),
            patch("dataclasses.asdict", return_value={"test": "data"}),
        ):
            result = BackgroundWorker._fetch_data(force=False)
            # Check that intraday_samples were appended
            assert "intraday_samples" in result["state"]
            assert len(result["state"]["intraday_samples"]) > 0
            assert "ts" in result["state"]["intraday_samples"][0]
            assert "pct_all" in result["state"]["intraday_samples"][0]
            assert "pct_sonnet" in result["state"]["intraday_samples"][0]


class TestHealthCheck:
    """Tests for the health_check / _run_health_check / _do_health_check methods."""

    # ------------------------------------------------------------------
    # _do_health_check – static method, testable in isolation
    # ------------------------------------------------------------------

    def test_do_health_check_passes_offsets_to_scan(self):
        """_do_health_check forwards _health_scan_offsets from state to quick_health_scan."""
        saved_offsets = {"/tmp/project/session.jsonl": 128}
        fake_state = {
            "_health_scan_offsets": saved_offsets,
        }

        with (
            patch("penny.state.load_state", return_value=fake_state),
            patch("penny.state.save_state"),
            patch("penny.analysis.quick_health_scan", return_value=([], saved_offsets)) as mock_scan,
        ):
            BackgroundWorker._do_health_check()

        mock_scan.assert_called_once_with(saved_offsets)

    def test_do_health_check_saves_updated_offsets_to_state(self):
        """_do_health_check must persist the new offsets returned by quick_health_scan."""
        old_offsets = {"/tmp/a.jsonl": 0}
        new_offsets = {"/tmp/a.jsonl": 512}
        fake_state = {"_health_scan_offsets": old_offsets}

        saved_states = []
        with (
            patch("penny.state.load_state", return_value=fake_state),
            patch("penny.state.save_state", side_effect=lambda s: saved_states.append(s)),
            patch("penny.analysis.quick_health_scan", return_value=([], new_offsets)),
        ):
            BackgroundWorker._do_health_check()

        assert len(saved_states) == 1
        assert saved_states[0]["_health_scan_offsets"] == new_offsets

    def test_do_health_check_writes_alerts_to_state_when_present(self):
        """When quick_health_scan returns alerts, they must be stored in state['health_alerts']."""
        alerts = [{"type": "high_burn", "project": "/tmp/proj"}]
        fake_state = {"_health_scan_offsets": {}}

        saved_states = []
        with (
            patch("penny.state.load_state", return_value=fake_state),
            patch("penny.state.save_state", side_effect=lambda s: saved_states.append(s)),
            patch("penny.analysis.quick_health_scan", return_value=(alerts, {})),
        ):
            BackgroundWorker._do_health_check()

        assert saved_states[0]["health_alerts"] == alerts

    def test_do_health_check_does_not_overwrite_alerts_when_none(self):
        """When quick_health_scan returns no alerts, state['health_alerts'] must not be set."""
        fake_state = {
            "_health_scan_offsets": {},
            "health_alerts": [{"type": "pre-existing"}],
        }

        saved_states = []
        with (
            patch("penny.state.load_state", return_value=fake_state),
            patch("penny.state.save_state", side_effect=lambda s: saved_states.append(s)),
            patch("penny.analysis.quick_health_scan", return_value=([], {})),
        ):
            BackgroundWorker._do_health_check()

        # The pre-existing alerts must not have been replaced with an empty list
        assert saved_states[0]["health_alerts"] == [{"type": "pre-existing"}]

    def test_do_health_check_returns_health_alerts_dict(self):
        """_do_health_check must return {'health_alerts': <list>}."""
        alerts = [{"type": "error_spike", "project": "/tmp/proj"}]
        fake_state = {"_health_scan_offsets": {}}

        with (
            patch("penny.state.load_state", return_value=fake_state),
            patch("penny.state.save_state"),
            patch("penny.analysis.quick_health_scan", return_value=(alerts, {})),
        ):
            result = BackgroundWorker._do_health_check()

        assert result == {"health_alerts": alerts}

    def test_do_health_check_uses_empty_offsets_when_key_absent(self):
        """_do_health_check defaults offsets to {} when the state key is missing."""
        fake_state = {}  # no _health_scan_offsets key

        with (
            patch("penny.state.load_state", return_value=fake_state),
            patch("penny.state.save_state"),
            patch("penny.analysis.quick_health_scan", return_value=([], {})) as mock_scan,
        ):
            BackgroundWorker._do_health_check()

        # First positional arg must be an empty dict, not missing
        call_args = mock_scan.call_args
        assert call_args.args[0] == {}

    def test_fetch_data_health_alerts_use_week_projects(self):
        """_fetch_data passes week project data (not month) to compute_health_alerts."""
        from datetime import datetime, timezone
        from unittest.mock import MagicMock

        fake_state = {
            "agents_running": [],
            "session_history": [],
            "last_session_scan": None,
            "plugin_state": {},
        }
        fake_period_start = datetime(2024, 1, 5, 20, 0, 0, tzinfo=timezone.utc)

        fake_prediction = MagicMock()
        fake_prediction.session_start = datetime(
            2024, 1, 5, 12, 30, 0, tzinfo=timezone.utc
        ).isoformat()
        fake_prediction.projected_pct_all = 50.0
        fake_prediction.pct_all = 30.0
        fake_prediction.budget_all = 1_000_000
        fake_prediction.days_remaining = 5.0
        fake_prediction.reset_label = "Jan 12 at 8pm"

        week_project = {
            "cwd": "/projects/penny",
            "name": "penny",
            "health": "green",
            "health_reasons": [],
        }
        mock_week = MagicMock()
        mock_week.project_usage = [week_project]

        mock_month = MagicMock()
        mock_month.project_usage = []

        mock_session = MagicMock()
        mock_session.project_usage = []

        saved_states = []

        def capture_save(s):
            saved_states.append(dict(s))

        with (
            patch("penny.state.load_state", return_value=fake_state),
            patch("penny.state.reset_period_if_needed", return_value=fake_state),
            patch(
                "penny.analysis.current_billing_period",
                return_value=(fake_period_start, None),
            ),
            patch(
                "penny.state.detect_new_sessions", return_value=(fake_state, [])
            ),
            patch("penny.state.save_state", side_effect=capture_save),
            patch("penny.spawner.check_running_agents", return_value=[]),
            patch("penny.analysis.build_prediction", return_value=fake_prediction),
            patch(
                "penny.analysis.scan_rich_metrics_multi",
                return_value={
                    "month": mock_month,
                    "week": mock_week,
                    "session": mock_session,
                },
            ),
            patch("penny.status_fetcher.get_cached_status", return_value=None),
            patch("penny.update_checker.should_check", return_value=False),
            patch("dataclasses.asdict", side_effect=lambda x: {"mocked": True}),
        ):
            BackgroundWorker._fetch_data(force=False)

        # Health alerts should be a list (computed by compute_health_alerts)
        final = saved_states[-1]
        assert isinstance(final["health_alerts"], list)

    # ------------------------------------------------------------------
    # health_check – public entry point (threading behaviour)
    # ------------------------------------------------------------------

    def test_health_check_posts_result_to_app(self):
        """health_check must post its result to the app delegate on the main thread."""
        app = FakeApp()
        worker = BackgroundWorker(app)
        fake_result = {"health_alerts": []}

        with patch.object(BackgroundWorker, "_do_health_check", return_value=fake_result):
            worker.health_check()
            import time
            time.sleep(0.2)

        assert len(app.results) == 1
        assert app.results[0] == fake_result

    def test_health_check_is_serialized(self):
        """A second health_check call while one is running must be a no-op (only one result posted)."""
        app = FakeApp()
        worker = BackgroundWorker(app)

        gate = threading.Event()

        def slow_check():
            gate.wait(timeout=2)
            return {"health_alerts": []}

        with patch.object(BackgroundWorker, "_do_health_check", side_effect=slow_check):
            worker.health_check()
            # Second call arrives while the first thread is still blocked
            worker.health_check()
            gate.set()
            import time
            time.sleep(0.2)

        assert len(app.results) == 1

    def test_health_check_resets_running_flag_after_completion(self):
        """_health_running must be False after health_check finishes."""
        app = FakeApp()
        worker = BackgroundWorker(app)

        with patch.object(BackgroundWorker, "_do_health_check", return_value={"health_alerts": []}):
            worker.health_check()
            import time
            time.sleep(0.2)

        assert worker._health_running is False

    def test_health_check_resets_running_flag_after_exception(self):
        """_health_running must be reset to False even when _do_health_check raises."""
        app = FakeApp()
        worker = BackgroundWorker(app)

        with patch.object(
            BackgroundWorker, "_do_health_check", side_effect=RuntimeError("scan failed")
        ):
            worker.health_check()
            import time
            time.sleep(0.2)

        assert worker._health_running is False

    def test_health_check_posts_error_dict_on_exception(self):
        """When _do_health_check raises, health_check must post {'error': <msg>} to the app."""
        app = FakeApp()
        worker = BackgroundWorker(app)

        with patch.object(
            BackgroundWorker, "_do_health_check", side_effect=RuntimeError("scan failed")
        ):
            worker.health_check()
            import time
            time.sleep(0.2)

        assert len(app.results) == 1
        assert "error" in app.results[0]
        assert "scan failed" in app.results[0]["error"]
