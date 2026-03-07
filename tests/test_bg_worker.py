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
