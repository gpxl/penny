"""Background worker thread for data fetching in Nae Nae.

Runs analysis off the main thread and posts results back via
performSelectorOnMainThread_withObject_waitUntilDone_ so the UI
can update safely.
"""

from __future__ import annotations

import threading
from typing import Any


class BackgroundWorker:
    """Fetch usage data off the main thread; post result to the app delegate."""

    def __init__(self, app: Any) -> None:
        self._app = app          # NaeNaeApp NSObject instance
        self._lock = threading.Lock()
        self._running = False

    def fetch(self, force: bool = False) -> None:
        """Trigger a background fetch. No-ops if a fetch is already in progress."""
        with self._lock:
            if self._running:
                return
            self._running = True
        t = threading.Thread(target=self._run, args=(force,), daemon=True)
        t.start()

    def _run(self, force: bool) -> None:
        try:
            result = self._fetch_data(force)
        except Exception as exc:
            print(f"[naenae] _fetch_data exception: {exc}", flush=True)
            result = {"error": str(exc)}
        finally:
            with self._lock:
                self._running = False

        # Post back to main thread
        self._app.performSelectorOnMainThread_withObject_waitUntilDone_(
            "_didFetchData:", result, False
        )

    @staticmethod
    def _fetch_data(force: bool) -> dict[str, Any]:
        """Gather all data needed by the UI. Called on a background thread."""
        from .analysis import build_prediction
        from .spawner import check_running_agents
        from .state import load_state, reset_period_if_needed
        from .tasks import filter_tasks, get_ready_tasks

        # Reload state each cycle so we pick up changes from spawner callbacks
        state = load_state()
        state = reset_period_if_needed(state)

        # Check for newly completed agents before building prediction
        newly_done = check_running_agents(state)

        prediction = build_prediction(state, force=force)
        projects = []  # populated by app delegate from config

        return {
            "state": state,
            "prediction": prediction,
            "newly_done": newly_done,
        }
