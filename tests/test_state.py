"""Unit tests for penny/state.py."""

from __future__ import annotations

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
        with patch("penny.state.STATE_PATH", state_file):
            from penny.state import load_state, save_state
            state = load_state()
            save_state(state)
        # No ".state.*.tmp" files (the unique-per-call pattern) left behind
        stragglers = list(tmp_path.glob(".state.*.tmp"))
        assert stragglers == []
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

    def test_replace_failure_propagates_and_cleans_up_tmp(self, tmp_path):
        """When os.replace raises, the original exception must propagate and the
        orphaned temp file must be removed by the except branch."""
        state_file = tmp_path / "state.json"
        with patch("penny.state.STATE_PATH", state_file):
            from penny.state import load_state, save_state

            state = load_state()
            with patch("penny.state.os.replace", side_effect=PermissionError("denied")):
                with pytest.raises(PermissionError, match="denied"):
                    save_state(state)

        # The except branch must have called os.unlink — no orphaned tmp files.
        stragglers = list(tmp_path.glob(".state.*.tmp"))
        assert stragglers == [], f"orphaned tmp files not cleaned up: {stragglers}"

    def test_replace_failure_when_unlink_also_raises_still_propagates_original(self, tmp_path):
        """When os.replace raises AND os.unlink raises OSError, the original
        exception (not the cleanup error) must propagate to the caller."""
        state_file = tmp_path / "state.json"
        with patch("penny.state.STATE_PATH", state_file):
            from penny.state import load_state, save_state

            state = load_state()
            with (
                patch("penny.state.os.replace", side_effect=PermissionError("no write")),
                patch("penny.state.os.unlink", side_effect=OSError("already gone")),
            ):
                with pytest.raises(PermissionError, match="no write"):
                    save_state(state)

    def test_concurrent_saves_never_raise(self, tmp_path):
        """Regression: bg_worker fetch and health_check race on save_state.

        Previously both threads wrote to a shared ``state.tmp`` and raced on
        ``tmp.replace(state.json)`` — the losing thread raised
        ``FileNotFoundError``, which bubbled out of ``_fetch_data`` before
        ``fetch_live_status`` could run and left the /status cache stale for
        hours. With unique temp files per call, concurrent saves must all
        succeed without raising.
        """
        import threading

        state_file = tmp_path / "state.json"
        errors: list[BaseException] = []

        def writer(idx: int) -> None:
            with patch("penny.state.STATE_PATH", state_file):
                from penny.state import save_state
                try:
                    for _ in range(20):
                        save_state({"writer": idx, "iteration": _})
                except BaseException as exc:  # pragma: no cover — only on regression
                    errors.append(exc)

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == [], f"save_state raised under concurrency: {errors!r}"
        assert state_file.exists()
        # And no ".state.*.tmp" stragglers — every temp got renamed or cleaned
        assert list(tmp_path.glob(".state.*.tmp")) == []
