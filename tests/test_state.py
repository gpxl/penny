"""Unit tests for penny/state.py."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest


# We need to patch STATE_PATH before importing state to avoid touching the real filesystem.
# The module-level STATE_PATH = data_dir() / "state.json" runs at import time.

class TestLoadState:
    def test_returns_defaults_when_file_missing(self, tmp_path):
        state_file = tmp_path / "state.json"
        with patch("penny.state.STATE_PATH", state_file):
            from penny.state import load_state
            state = load_state()
        assert "agents_running" in state
        assert state["agents_running"] == []
        assert "period_history" in state

    def test_returns_defaults_on_invalid_json(self, tmp_path):
        state_file = tmp_path / "state.json"
        state_file.write_text("not json", encoding="utf-8")
        with patch("penny.state.STATE_PATH", state_file):
            from penny.state import load_state
            state = load_state()
        assert state["agents_running"] == []


class TestSaveState:
    def test_round_trip_preserves_all_keys(self, tmp_path):
        state_file = tmp_path / "state.json"
        with patch("penny.state.STATE_PATH", state_file):
            from penny.state import load_state, save_state
            original = load_state()
            original["agents_running"] = [{"task_id": "abc", "pid": 123}]
            original["period_history"] = [{"output_all": 500}]
            save_state(original)
            loaded = load_state()

        assert loaded["agents_running"] == [{"task_id": "abc", "pid": 123}]
        assert loaded["period_history"] == [{"output_all": 500}]

    def test_atomic_write_no_tmp_left_behind(self, tmp_path):
        state_file = tmp_path / "state.json"
        tmp_file = state_file.with_suffix(".tmp")
        with patch("penny.state.STATE_PATH", state_file):
            from penny.state import load_state, save_state
            state = load_state()
            save_state(state)
        # .tmp file should be gone (renamed to state.json)
        assert not tmp_file.exists()
        assert state_file.exists()

    def test_persists_custom_field(self, tmp_path):
        state_file = tmp_path / "state.json"
        with patch("penny.state.STATE_PATH", state_file):
            from penny.state import load_state, save_state
            state = load_state()
            state["last_check"] = "2025-01-01T00:00:00+00:00"
            save_state(state)
            loaded = load_state()
        assert loaded["last_check"] == "2025-01-01T00:00:00+00:00"
