"""State persistence for Nae Nae."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
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
    """Persist state to disk atomically."""
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, default=str))
    tmp.replace(STATE_PATH)


def _default_state() -> dict[str, Any]:
    return {
        "last_check": None,
        "current_period_start": None,
        "predictions": {},
        "agents_running": [],
        "spawned_this_week": [],
        "recently_completed": [],  # agents completed this session; user-clearable
        "period_history": [],    # past completed periods for budget calibration
        "session_history": [],   # past completed sub-sessions for budget calibration
        "last_session_scan": None,
    }


def archive_completed_session(
    state: dict[str, Any],
    session_start: datetime,
    session_end: datetime,
    tokens_all: int,
    tokens_sonnet: int,
) -> None:
    """Append a completed sub-session to session_history (keeps last 20)."""
    history = state.setdefault("session_history", [])
    history.append({
        "start": session_start.isoformat(),
        "end": session_end.isoformat(),
        "output_all": tokens_all,
        "output_sonnet": tokens_sonnet,
    })
    state["session_history"] = history[-20:]


def detect_new_sessions(state: dict[str, Any], period_start: datetime) -> dict[str, Any]:
    """
    Detect completed sub-sessions since period_start and archive their usage.
    Called at the start of each analysis cycle so estimate_session_budget() improves
    over time as real rate-limit data accumulates.
    """
    from .analysis import count_tokens_since, find_session_boundaries

    now = datetime.now(timezone.utc)
    boundaries = find_session_boundaries(period_start)

    already_archived = {s["start"] for s in state.get("session_history", [])}

    # Each boundary is the START of a new session (= END of the previous one).
    # Build completed session pairs: (session_start, session_end).
    sess_starts = [period_start] + boundaries[:-1] if boundaries else []
    sess_ends = boundaries

    for sess_start, sess_end in zip(sess_starts, sess_ends):
        if sess_end > now:
            break  # Future boundary — session not completed yet
        start_str = sess_start.isoformat()
        if start_str in already_archived:
            continue
        usage = count_tokens_since(sess_start, until=sess_end)
        archive_completed_session(state, sess_start, sess_end, usage.output_all, usage.output_sonnet)

    state["last_session_scan"] = now.isoformat()
    return state


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

    # Archive the completed period if we have data
    old_pred = state.get("predictions", {})
    if old_pred.get("output_all", 0) > 0 and state.get("current_period_start"):
        history = state.get("period_history", [])
        history.append({
            "period_start": state["current_period_start"],
            "output_all": old_pred.get("output_all", 0),
            "output_sonnet": old_pred.get("output_sonnet", 0),
        })
        # Keep last 12 weeks
        state["period_history"] = history[-12:]

    # Reset for new period
    state["current_period_start"] = period_start_str
    state["spawned_this_week"] = []
    state["agents_running"] = []
    return state
