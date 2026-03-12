"""Background worker thread for data fetching in Penny.

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
            print(f"[penny] _fetch_data exception: {exc}", flush=True)
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
        import dataclasses
        from datetime import timedelta, timezone

        from .analysis import build_prediction, current_billing_period, scan_rich_metrics_multi
        from .spawner import check_running_agents
        from .state import detect_new_sessions, load_state, reset_period_if_needed, save_state
        from .status_fetcher import get_cached_status

        # Reload state each cycle so we pick up changes from spawner callbacks
        state = load_state()
        state = reset_period_if_needed(state)
        start, _ = current_billing_period()
        state, precomputed_boundaries = detect_new_sessions(state, start)
        save_state(state)

        # Check for newly completed agents before building prediction
        newly_done = check_running_agents(state)

        prediction = build_prediction(state, force=force, precomputed_boundaries=precomputed_boundaries)

        # Rich metrics scan — single JSONL pass for all time windows
        from datetime import datetime
        now_utc = datetime.now(timezone.utc)
        session_start_str = (
            prediction.session_start if hasattr(prediction, "session_start") else ""
        )
        if session_start_str:
            try:
                session_dt = datetime.fromisoformat(session_start_str)
                if session_dt.tzinfo is None:
                    session_dt = session_dt.replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                session_dt = now_utc - timedelta(hours=5)
        else:
            session_dt = now_utc - timedelta(hours=5)

        windows = {
            "session": session_dt,
            "week": now_utc - timedelta(days=7),
            "month": now_utc - timedelta(days=28),
            "all": datetime(2024, 1, 1, tzinfo=timezone.utc),
        }
        multi = scan_rich_metrics_multi(windows)
        state["rich_metrics"] = dataclasses.asdict(multi.get("month", multi.get("all")))
        state["rich_metrics_by_window"] = {
            k: dataclasses.asdict(v) for k, v in multi.items()
        }

        # Intraday sample — append current /status percentages for burn-rate sparkline
        live = get_cached_status()
        if live is not None and not live.outage:
            samples = state.setdefault("intraday_samples", [])
            samples.append({
                "ts": datetime.now(timezone.utc).isoformat(),
                "pct_all": live.weekly_pct_all,
                "pct_sonnet": live.weekly_pct_sonnet,
            })
            cutoff = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
            state["intraday_samples"] = [s for s in samples if s["ts"] >= cutoff]

        save_state(state)

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
