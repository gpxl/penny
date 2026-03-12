"""Background worker thread for data fetching in Penny.

Runs analysis off the main thread and posts results back via
performSelectorOnMainThread_withObject_waitUntilDone_ so the UI
can update safely.
"""

from __future__ import annotations

import threading
from typing import Any

from .log import logger


class BackgroundWorker:
    """Fetch usage data off the main thread; post result to the app delegate."""

    def __init__(self, app: Any) -> None:
        self._app = app          # PennyApp NSObject instance
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
            logger.error("_fetch_data exception: %s", exc)
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
        from .analysis import build_prediction, current_billing_period
        from .spawner import check_running_agents
        from .state import detect_new_sessions, load_state, reset_period_if_needed, save_state

        # Reload state each cycle so we pick up changes from spawner callbacks
        state = load_state()
        state = reset_period_if_needed(state)
        start, _ = current_billing_period()
        state = detect_new_sessions(state, start)
        save_state(state)

        # Check for newly completed agents before building prediction
        newly_done = check_running_agents(state)

        prediction = build_prediction(state, force=force)
        _projects = []  # populated by app delegate from config

        # Check for updates (at most once per 24 hours)
        from .update_checker import should_check, update_state_with_check
        if should_check(state):
            state = update_state_with_check(state)
            save_state(state)

        return {
            "state": state,
            "prediction": prediction,
            "newly_done": newly_done,
            "update_check": state.get("update_check"),
        }
