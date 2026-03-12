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
