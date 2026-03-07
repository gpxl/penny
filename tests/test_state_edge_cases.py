"""Edge-case tests for penny/state.py — persistence, corruption, session archiving."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import patch

from penny.state import (
    _default_state,
    archive_completed_session,
    load_state,
    save_state,
)

# ── load_state ───────────────────────────────────────────────────────────────


class TestLoadState:
    def test_returns_default_on_missing_file(self, tmp_path):
        with patch("penny.state.STATE_PATH", tmp_path / "nonexistent.json"):
            state = load_state()
        assert state == _default_state()

    def test_returns_default_on_corrupt_json(self, tmp_path):
        bad = tmp_path / "state.json"
        bad.write_text("not valid json{{{")
        with patch("penny.state.STATE_PATH", bad):
            state = load_state()
        assert state == _default_state()

    def test_returns_default_on_empty_file(self, tmp_path):
        empty = tmp_path / "state.json"
        empty.write_text("")
        with patch("penny.state.STATE_PATH", empty):
            state = load_state()
        assert state == _default_state()

    def test_loads_valid_state(self, tmp_path):
        state_file = tmp_path / "state.json"
        data = {"agents_running": [{"task_id": "t-1"}], "custom_key": 42}
        state_file.write_text(json.dumps(data))
        with patch("penny.state.STATE_PATH", state_file):
            state = load_state()
        assert state["agents_running"][0]["task_id"] == "t-1"
        assert state["custom_key"] == 42

    def test_loads_preserves_all_keys(self, tmp_path):
        state_file = tmp_path / "state.json"
        original = _default_state()
        original["agents_running"] = [{"task_id": "t-1"}]
        original["current_period_start"] = "2025-03-01T00:00:00+00:00"
        state_file.write_text(json.dumps(original))
        with patch("penny.state.STATE_PATH", state_file):
            state = load_state()
        assert state == original


# ── save_state ───────────────────────────────────────────────────────────────


class TestSaveState:
    def test_writes_valid_json(self, tmp_path):
        state_file = tmp_path / "state.json"
        with patch("penny.state.STATE_PATH", state_file):
            save_state({"key": "value", "agents_running": []})
        loaded = json.loads(state_file.read_text())
        assert loaded["key"] == "value"

    def test_overwrites_existing(self, tmp_path):
        state_file = tmp_path / "state.json"
        state_file.write_text('{"old": true}')
        with patch("penny.state.STATE_PATH", state_file):
            save_state({"new": True})
        loaded = json.loads(state_file.read_text())
        assert "new" in loaded
        assert "old" not in loaded

    def test_round_trip_preserves_data(self, tmp_path):
        state_file = tmp_path / "state.json"
        original = {
            "agents_running": [{"task_id": "t-1", "pid": 123}],
            "recently_completed": [{"task_id": "t-2", "status": "completed"}],
            "plugin_state": {"beads": {"spawned_task_ids": ["t-2"]}},
        }
        with patch("penny.state.STATE_PATH", state_file):
            save_state(original)
            loaded = load_state()
        assert loaded["agents_running"][0]["task_id"] == "t-1"
        assert loaded["recently_completed"][0]["status"] == "completed"
        assert loaded["plugin_state"]["beads"]["spawned_task_ids"] == ["t-2"]

    def test_handles_datetime_serialization(self, tmp_path):
        state_file = tmp_path / "state.json"
        state = {"timestamp": datetime.now(timezone.utc)}
        with patch("penny.state.STATE_PATH", state_file):
            save_state(state)  # default=str handles datetime
        loaded = json.loads(state_file.read_text())
        assert "timestamp" in loaded

    def test_atomic_write_no_partial(self, tmp_path):
        """save_state uses a temp file + rename, so even on crash the file is valid."""
        state_file = tmp_path / "state.json"
        with patch("penny.state.STATE_PATH", state_file):
            save_state({"step": 1})
            save_state({"step": 2})
        loaded = json.loads(state_file.read_text())
        assert loaded["step"] == 2


# ── _default_state ───────────────────────────────────────────────────────────


class TestDefaultState:
    def test_has_all_required_keys(self):
        state = _default_state()
        expected_keys = {
            "last_check",
            "current_period_start",
            "predictions",
            "agents_running",
            "recently_completed",
            "period_history",
            "session_history",
            "last_session_scan",
            "plugin_state",
        }
        assert set(state.keys()) == expected_keys

    def test_lists_are_empty(self):
        state = _default_state()
        for key in ("agents_running", "recently_completed", "period_history", "session_history"):
            assert state[key] == [], f"{key} should be empty"

    def test_plugin_state_is_empty_dict(self):
        state = _default_state()
        assert state["plugin_state"] == {}

    def test_returns_fresh_copy(self):
        s1 = _default_state()
        s2 = _default_state()
        s1["agents_running"].append({"task_id": "mutated"})
        assert s2["agents_running"] == []


# ── archive_completed_session ────────────────────────────────────────────────


class TestArchiveCompletedSession:
    def test_appends_session(self):
        state = {"session_history": []}
        start = datetime(2025, 3, 7, 10, 0, 0, tzinfo=timezone.utc)
        end = datetime(2025, 3, 7, 16, 0, 0, tzinfo=timezone.utc)
        archive_completed_session(state, start, end, 5000, 3000)
        assert len(state["session_history"]) == 1
        entry = state["session_history"][0]
        assert entry["output_all"] == 5000
        assert entry["output_sonnet"] == 3000
        assert entry["start"] == start.isoformat()
        assert entry["end"] == end.isoformat()

    def test_caps_at_200_entries(self):
        state = {
            "session_history": [
                {
                    "start": f"2025-01-01T{i:02d}:00:00+00:00",
                    "end": f"2025-01-01T{i:02d}:30:00+00:00",
                    "output_all": 1000,
                    "output_sonnet": 500,
                }
                for i in range(200)
            ]
        }
        start = datetime(2025, 3, 7, 10, 0, 0, tzinfo=timezone.utc)
        end = datetime(2025, 3, 7, 16, 0, 0, tzinfo=timezone.utc)
        archive_completed_session(state, start, end, 9999, 8888)
        assert len(state["session_history"]) == 200
        assert state["session_history"][-1]["output_all"] == 9999

    def test_creates_session_history_key_if_missing(self):
        state = {}
        start = datetime(2025, 3, 7, 10, 0, 0, tzinfo=timezone.utc)
        end = datetime(2025, 3, 7, 16, 0, 0, tzinfo=timezone.utc)
        archive_completed_session(state, start, end, 1000, 500)
        assert "session_history" in state
        assert len(state["session_history"]) == 1

    def test_multiple_sessions_in_order(self):
        state = {"session_history": []}
        for i in range(5):
            start = datetime(2025, 3, i + 1, 10, 0, 0, tzinfo=timezone.utc)
            end = datetime(2025, 3, i + 1, 16, 0, 0, tzinfo=timezone.utc)
            archive_completed_session(state, start, end, 1000 * (i + 1), 500 * (i + 1))
        assert len(state["session_history"]) == 5
        assert state["session_history"][0]["output_all"] == 1000
        assert state["session_history"][4]["output_all"] == 5000

    def test_preserves_existing_entries(self):
        state = {
            "session_history": [
                {"start": "old", "end": "old", "output_all": 111, "output_sonnet": 222}
            ]
        }
        start = datetime(2025, 3, 7, 10, 0, 0, tzinfo=timezone.utc)
        end = datetime(2025, 3, 7, 16, 0, 0, tzinfo=timezone.utc)
        archive_completed_session(state, start, end, 333, 444)
        assert len(state["session_history"]) == 2
        assert state["session_history"][0]["output_all"] == 111
        assert state["session_history"][1]["output_all"] == 333
