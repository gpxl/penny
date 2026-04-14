"""State persistence for Penny."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from typing import Any

from .paths import data_dir

STATE_PATH = data_dir() / "state.json"


def load_state() -> dict[str, Any]:
    """Load runtime state from disk. Returns defaults if missing."""
    if not STATE_PATH.exists():
        return _default_state()
    try:
        with STATE_PATH.open() as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return _default_state()


def save_state(state: dict[str, Any]) -> None:
    """Persist state to disk atomically.

    Uses ``tempfile.mkstemp`` for a unique per-call temp file. A shared
    ``state.tmp`` is a silent landmine when bg_worker's fetch and
    health-check threads save concurrently: both write the same tmp, the
    first ``replace`` wins, and the second raises ``FileNotFoundError``.
    That exception propagated out of ``_fetch_data`` before
    ``fetch_live_status`` could run, leaving the /status cache stale for
    hours.
    """
    state_path = STATE_PATH
    fd, tmp_path = tempfile.mkstemp(
        prefix=".state.", suffix=".tmp", dir=str(state_path.parent)
    )
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(state, f, indent=2, default=str)
        os.replace(tmp_path, state_path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _default_state() -> dict[str, Any]:
    return {
        "last_check": None,
        "current_period_start": None,
        "predictions": {},
        "agents_running": [],
        "recently_completed": [],  # last-20 agent completions; user-clearable
        "period_history": [],    # past completed periods for budget calibration
        "session_history": [],   # past completed sub-sessions for budget calibration
        "last_session_scan": None,
        "plugin_state": {},      # namespaced dict for plugin-owned state; never reset by core
        "rich_metrics": {},
        "intraday_samples": [],  # [{ts, pct_all, pct_sonnet}, …] last 48h of /status polls
    }


def archive_completed_session(
    state: dict[str, Any],
    session_start: datetime,
    session_end: datetime,
    tokens_all: int,
    tokens_sonnet: int,
) -> None:
    """Append a completed sub-session to session_history (keeps last 200)."""
    history = state.setdefault("session_history", [])
    history.append({
        "start": session_start.isoformat(),
        "end": session_end.isoformat(),
        "output_all": tokens_all,
        "output_sonnet": tokens_sonnet,
    })
    state["session_history"] = history[-200:]


def detect_new_sessions(state: dict[str, Any], period_start: datetime) -> dict[str, Any]:
    """
    Detect completed sub-sessions across recent billing periods and archive them.
    Called at the start of each analysis cycle so estimate_session_budget() improves
    over time as real rate-limit data accumulates.

    Performance: only scans the current billing period when session_history already
    has data.  Falls back to 12-week scan on the very first run (empty history).
    Token counting for un-archived sessions uses a single JSONL pass via
    count_tokens_by_window() instead of one scan per session.
    """
    from .analysis import (
        count_tokens_by_window,
        find_session_boundaries,
        past_billing_periods,
    )

    now = datetime.now(timezone.utc)

    # Only scan 12 weeks on the very first run; after that, just the current period.
    has_history = bool(state.get("session_history"))
    periods = past_billing_periods(1) if has_history else past_billing_periods(12)
    oldest_start = periods[0][0]
    all_boundaries = find_session_boundaries(oldest_start)

    already_archived = {s["start"] for s in state.get("session_history", [])}

    # Collect all un-archived (start, end) pairs first
    pending: list[tuple[datetime, datetime]] = []
    for p_start, p_end in periods:
        period_boundaries = [b for b in all_boundaries if p_start < b <= p_end]
        if not period_boundaries:
            continue

        sess_starts = [p_start] + period_boundaries[:-1]
        sess_ends = period_boundaries

        for sess_start, sess_end in zip(sess_starts, sess_ends):
            if sess_end > now:
                break
            if sess_start.isoformat() in already_archived:
                continue
            pending.append((sess_start, sess_end))

    # Batch token counting: one JSONL pass for all pending sessions
    if pending:
        windows = {s.isoformat(): (s, e) for s, e in pending}
        usage_map = count_tokens_by_window(windows)
        for sess_start, sess_end in pending:
            usage = usage_map.get(sess_start.isoformat())
            if usage is not None:
                archive_completed_session(
                    state, sess_start, sess_end,
                    usage.output_all, usage.output_sonnet,
                )

    state["last_session_scan"] = now.isoformat()
    return state, all_boundaries


def reset_period_if_needed(state: dict[str, Any]) -> dict[str, Any]:
    """
    If we've crossed into a new billing period (Friday 20:00 UTC),
    archive the old period's token counts and reset weekly tracking.
    """
    from .analysis import current_billing_period
    start, _ = current_billing_period()
    period_start_str = start.isoformat()

    if state.get("current_period_start") == period_start_str:
        return state  # Same period, nothing to do

    # Archive the completed period for dashboard/report history
    old_pred = state.get("predictions", {})
    if old_pred.get("output_all", 0) > 0 and state.get("current_period_start"):
        history = state.get("period_history", [])
        history.append({
            "period_start": state["current_period_start"],
            "output_all": old_pred.get("output_all", 0),
            "output_sonnet": old_pred.get("output_sonnet", 0),
        })
        state["period_history"] = history[-12:]

    # Reset for new period
    state["current_period_start"] = period_start_str
    state["agents_running"] = []
    return state
