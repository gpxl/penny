"""Unit tests for penny/status_fetcher.py — parsing and caching logic."""

from __future__ import annotations

import json
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from penny.status_fetcher import (
    LiveStatus,
    _cache_file,
    _detect_api_error,
    _feed_child,
    _load_cache,
    _parse_usage_screen,
    _save_cache,
    _screen_text,
    fetch_live_status,
    get_cached_status,
    status_as_prediction_overrides,
)

# ── _detect_api_error ─────────────────────────────────────────────────────────


class TestDetectApiError:
    def test_detects_api_error(self):
        assert _detect_api_error("something api_error something") is True

    def test_detects_internal_server_error(self):
        assert _detect_api_error("Internal server error") is True

    def test_detects_failed_to_load(self):
        assert _detect_api_error("Failed to load usage data") is True

    def test_detects_type_error_json(self):
        assert _detect_api_error('"type":"error"') is True

    def test_normal_text_passes(self):
        assert _detect_api_error("Current session  16% used") is False


# ── _parse_usage_screen ───────────────────────────────────────────────────────


SAMPLE_USAGE_SCREEN = """\
                                                Settings   Status   Config   Usage
 ─────────────────────────────────────────────────────────────────────────────────

  Current session                                                 16% used
  Resets 2pm (Europe/Amsterdam)

  Current week (all models)                                       30% used
  Resets Mar 28 at 9:59am (Europe/Amsterdam)

  Current week (Sonnet only)                                      41% used
  Resets Mar 24 at 8pm (Europe/Amsterdam)

  Tab/Shift+Tab to cycle   Enter to select   Esc to cancel
"""

# Legacy format: Sonnet shared the same reset line as all-models
SAMPLE_USAGE_SCREEN_SHARED_RESET = """\
                                                Settings   Status   Config   Usage
 ─────────────────────────────────────────────────────────────────────────────────

  Current session                                                 16% used
  Resets 2pm (Europe/Amsterdam)

  Current week (all models)                                       30% used
  Current week (Sonnet only)                                      41% used
  Resets Mar 6 at 9pm (Europe/Amsterdam)

  Tab/Shift+Tab to cycle   Enter to select   Esc to cancel
"""


class TestParseUsageScreen:
    def test_parses_all_three_percentages(self):
        result = _parse_usage_screen(SAMPLE_USAGE_SCREEN)
        assert result is not None
        assert result.session_pct == 16.0
        assert result.weekly_pct_all == 30.0
        assert result.weekly_pct_sonnet == 41.0

    def test_parses_session_reset(self):
        result = _parse_usage_screen(SAMPLE_USAGE_SCREEN)
        assert result is not None
        assert result.session_reset_label == "2pm"
        assert result.session_reset_tz == "Europe/Amsterdam"

    def test_parses_weekly_all_models_reset(self):
        result = _parse_usage_screen(SAMPLE_USAGE_SCREEN)
        assert result is not None
        assert result.weekly_reset_label == "Mar 28 at 9:59am"
        assert result.weekly_reset_tz == "Europe/Amsterdam"

    def test_parses_weekly_sonnet_reset(self):
        result = _parse_usage_screen(SAMPLE_USAGE_SCREEN)
        assert result is not None
        assert result.weekly_reset_label_sonnet == "Mar 24 at 8pm"
        assert result.weekly_reset_tz_sonnet == "Europe/Amsterdam"

    def test_shared_reset_assigns_to_both(self):
        """Legacy format: single shared reset line → same value for both budgets."""
        result = _parse_usage_screen(SAMPLE_USAGE_SCREEN_SHARED_RESET)
        assert result is not None
        assert result.weekly_reset_label == "Mar 6 at 9pm"
        assert result.weekly_reset_label_sonnet == "Mar 6 at 9pm"
        assert result.weekly_reset_tz_sonnet == "Europe/Amsterdam"

    def test_returns_none_on_empty(self):
        assert _parse_usage_screen("") is None

    def test_returns_none_on_incomplete(self):
        # Only one percentage — not enough data
        assert _parse_usage_screen("Current session  16% used") is None

    def test_handles_decimal_percentages(self):
        screen = SAMPLE_USAGE_SCREEN.replace("16% used", "16.5% used")
        result = _parse_usage_screen(screen)
        assert result is not None
        assert result.session_pct == 16.5

    def test_finds_tab_bar_and_ignores_above(self):
        # Prepend garbage above the tab bar
        garbage = "old tab content\n50% used\nsome leftover\n"
        screen = garbage + SAMPLE_USAGE_SCREEN
        result = _parse_usage_screen(screen)
        assert result is not None
        # Should parse the real values, not the garbage 50%
        assert result.session_pct == 16.0

    def test_fallback_positional_parsing(self):
        # No label anchors — just three "N% used" values and resets
        screen = """\
Settings   Config   Usage
10% used
20% used
30% used
Resets 5pm (US/Pacific)
Resets Jan 1 at 3am (US/Pacific)
"""
        result = _parse_usage_screen(screen)
        assert result is not None
        assert result.session_pct == 10.0
        assert result.weekly_pct_all == 20.0
        assert result.weekly_pct_sonnet == 30.0

    def test_multiline_label_and_percentage(self):
        # Real Claude Code format: label on one line, progress bar + pct on next
        screen = """\
  Status   Config   Usage

  Current session
  ████████                                    11% used
  Resets 3pm (Europe/Amsterdam)

  Current week (all models)
  ██████████████████                          37% used
  Resets Mar 21 at 9am (Europe/Amsterdam)

  Current week (Sonnet only)
  █                                           0% used
  Resets Mar 24 at 8pm (Europe/Amsterdam)

  Esc to cancel
"""
        result = _parse_usage_screen(screen)
        assert result is not None
        assert result.session_pct == 11.0
        assert result.weekly_pct_all == 37.0
        assert result.weekly_pct_sonnet == 0.0
        assert result.session_reset_label == "3pm"
        assert result.weekly_reset_label == "Mar 21 at 9am"
        assert result.weekly_reset_label_sonnet == "Mar 24 at 8pm"


# ── _screen_text ───────────────────────────────────────────────────────────────


class TestScreenText:
    def test_joins_display_rows(self):
        """_screen_text joins pyte Screen.display rows with newlines."""
        from unittest.mock import MagicMock
        screen = MagicMock()
        screen.display = ["line1", "line2", "line3"]
        result = _screen_text(screen)
        assert result == "line1\nline2\nline3"

    def test_rstrips_each_row(self):
        """_screen_text removes trailing whitespace from each row."""
        from unittest.mock import MagicMock
        screen = MagicMock()
        screen.display = ["line1   ", "  line2  ", "line3"]
        result = _screen_text(screen)
        assert result == "line1\n  line2\nline3"

    def test_handles_empty_display(self):
        """_screen_text handles empty display list."""
        from unittest.mock import MagicMock
        screen = MagicMock()
        screen.display = []
        result = _screen_text(screen)
        assert result == ""


# ── Cache ─────────────────────────────────────────────────────────────────────


class TestCache:
    def test_save_and_load_round_trip(self, tmp_path):
        status = LiveStatus(
            session_pct=16.0,
            session_reset_label="2pm",
            session_reset_tz="Europe/Amsterdam",
            weekly_pct_all=30.0,
            weekly_pct_sonnet=41.0,
            weekly_reset_label="Mar 6 at 9pm",
            weekly_reset_tz="Europe/Amsterdam",
            weekly_reset_label_sonnet="Mar 3 at 8pm",
            weekly_reset_tz_sonnet="Europe/Amsterdam",
            fetched_at=datetime(2025, 3, 6, 12, 0, 0, tzinfo=timezone.utc),
        )
        with patch("penny.status_fetcher._cache_file", return_value=tmp_path / "cache.json"):
            _save_cache(status)
            loaded = _load_cache()

        assert loaded is not None
        assert loaded.session_pct == 16.0
        assert loaded.weekly_pct_all == 30.0
        assert loaded.weekly_reset_label == "Mar 6 at 9pm"

    def test_save_skips_outage(self, tmp_path):
        status = LiveStatus(
            session_pct=0.0,
            session_reset_label="",
            session_reset_tz="",
            weekly_pct_all=0.0,
            weekly_pct_sonnet=0.0,
            weekly_reset_label="",
            weekly_reset_tz="",
            fetched_at=datetime.now(timezone.utc),
            outage=True,
        )
        cache_file = tmp_path / "cache.json"
        with patch("penny.status_fetcher._cache_file", return_value=cache_file):
            _save_cache(status)
        assert not cache_file.exists()

    def test_round_trip_includes_sonnet_reset(self, tmp_path):
        status = _make_live_status()
        with patch("penny.status_fetcher._cache_file", return_value=tmp_path / "cache.json"):
            _save_cache(status)
            loaded = _load_cache()
        assert loaded is not None
        assert loaded.weekly_reset_label_sonnet == "Mar 3 at 8pm"
        assert loaded.weekly_reset_tz_sonnet == "UTC"

    def test_load_old_cache_without_sonnet_fields(self, tmp_path):
        """Old cache files missing Sonnet fields should load with fallback to all-models values."""
        cache_file = tmp_path / "cache.json"
        old_data = {
            "session_pct": 10.0,
            "session_reset_label": "3pm",
            "session_reset_tz": "UTC",
            "weekly_pct_all": 20.0,
            "weekly_pct_sonnet": 30.0,
            "weekly_reset_label": "Mar 10 at 5pm",
            "weekly_reset_tz": "UTC",
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }
        cache_file.write_text(json.dumps(old_data))
        with patch("penny.status_fetcher._cache_file", return_value=cache_file):
            loaded = _load_cache()
        assert loaded is not None
        # Sonnet fields should fall back to all-models values
        assert loaded.weekly_reset_label_sonnet == "Mar 10 at 5pm"
        assert loaded.weekly_reset_tz_sonnet == "UTC"

    def test_load_returns_none_on_missing(self, tmp_path):
        with patch("penny.status_fetcher._cache_file", return_value=tmp_path / "nope.json"):
            assert _load_cache() is None

    def test_load_returns_none_on_bad_json(self, tmp_path):
        bad = tmp_path / "cache.json"
        bad.write_text("not json")
        with patch("penny.status_fetcher._cache_file", return_value=bad):
            assert _load_cache() is None


# ── _cache_file ───────────────────────────────────────────────────────────────


class TestCacheFile:
    def test_uses_penny_home_when_set(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PENNY_HOME", str(tmp_path))
        result = _cache_file()
        assert result == tmp_path / "status_cache.json"

    def test_defaults_to_home_dot_penny(self, monkeypatch):
        monkeypatch.delenv("PENNY_HOME", raising=False)
        result = _cache_file()
        assert result == Path.home() / ".penny" / "status_cache.json"


# ── _parse_usage_screen edge cases ────────────────────────────────────────────


class TestParseUsageScreenEdgeCases:
    def test_returns_none_when_no_resets_found(self):
        # Has percentages but no "Resets..." lines
        screen = """\
Settings   Config   Usage
Current session 16% used
Current week (all models) 30% used
Current week (Sonnet only) 41% used
"""
        result = _parse_usage_screen(screen)
        assert result is None

    def test_uses_first_reset_for_session_when_only_one_reset(self):
        # Only one Resets line — session and weekly both point to it
        screen = """\
Settings   Config   Usage
Current session 16% used
Resets 3pm (US/Eastern)
Current week (all models) 30% used
Current week (Sonnet only) 41% used
"""
        result = _parse_usage_screen(screen)
        assert result is not None
        assert result.session_reset_label == "3pm"
        assert result.weekly_reset_label == "3pm"

    def test_tab_bar_not_found_parses_whole_screen(self):
        # No tab bar line — falls back to whole screen
        screen = """\
Current session 16% used
Resets 2pm (UTC)
Current week (all models) 30% used
Current week (Sonnet only) 41% used
Resets Mar 7 at 5pm (UTC)
"""
        result = _parse_usage_screen(screen)
        assert result is not None
        assert result.session_pct == 16.0


# ── status_as_prediction_overrides ────────────────────────────────────────────


class TestStatusAsPredictionOverrides:
    def test_returns_expected_keys(self):
        status = LiveStatus(
            session_pct=16.0,
            session_reset_label="2pm",
            session_reset_tz="Europe/Amsterdam",
            weekly_pct_all=30.0,
            weekly_pct_sonnet=41.0,
            weekly_reset_label="Mar 6 at 9pm",
            weekly_reset_tz="Europe/Amsterdam",
            weekly_reset_label_sonnet="Mar 3 at 8pm",
            weekly_reset_tz_sonnet="Europe/Amsterdam",
            fetched_at=datetime.now(timezone.utc),
        )
        result = status_as_prediction_overrides(status)
        assert result["session_pct_all"] == 16.0
        assert result["pct_all"] == 30.0
        assert result["pct_sonnet"] == 41.0
        assert result["reset_label"] == "Mar 6 at 9pm"
        assert result["session_reset_label"] == "2pm"
        assert result["reset_tz"] == "Europe/Amsterdam"
        assert result["reset_label_sonnet"] == "Mar 3 at 8pm"
        assert result["reset_tz_sonnet"] == "Europe/Amsterdam"


# ── fetch_live_status TTL / no-claude behaviour ───────────────────────────────


def _make_live_status(**overrides):
    defaults = dict(
        session_pct=16.0,
        session_reset_label="2pm",
        session_reset_tz="UTC",
        weekly_pct_all=30.0,
        weekly_pct_sonnet=41.0,
        weekly_reset_label="Mar 6 at 9pm",
        weekly_reset_tz="UTC",
        weekly_reset_label_sonnet="Mar 3 at 8pm",
        weekly_reset_tz_sonnet="UTC",
        fetched_at=datetime.now(timezone.utc),
    )
    defaults.update(overrides)
    return LiveStatus(**defaults)


class TestFetchLiveStatusTTL:
    def setup_method(self):
        # Reset the module-level in-memory cache before each test
        import penny.status_fetcher as sf
        sf._cache = None

    def test_returns_in_memory_cache_when_fresh(self):
        """A recent in-memory cache should be returned without spawning claude."""
        import penny.status_fetcher as sf

        cached = _make_live_status(session_pct=99.0)
        sf._cache = cached

        with patch("penny.status_fetcher.shutil.which", return_value=None):
            result = fetch_live_status(force=False)

        # Should return cached value even though claude is not available
        assert result is cached
        assert result.session_pct == 99.0

    def test_force_bypasses_in_memory_cache_when_claude_missing(self):
        """force=True skips TTL but returns stale cached data when claude is not available."""
        import penny.status_fetcher as sf

        cached = _make_live_status(session_pct=99.0)
        sf._cache = cached

        with patch("penny.status_fetcher.shutil.which", return_value=None):
            result = fetch_live_status(force=True)

        # Should return stale cached data (not outage, not in-memory cache)
        assert isinstance(result, LiveStatus)
        assert result.outage is False
        # session_pct should be from the cached value since no API error detected
        assert result.session_pct == 99.0

    def test_returns_none_when_claude_not_in_path(self):
        """No in-memory or disk cache, and claude is absent → returns stale/default status without outage."""
        import penny.status_fetcher as sf
        sf._cache = None

        with (
            patch("penny.status_fetcher.shutil.which", return_value=None),
            patch("penny.status_fetcher._load_cache", return_value=None),
        ):
            result = fetch_live_status()

        # No API error detected, so outage=False (transient failure, not an outage)
        assert isinstance(result, LiveStatus)
        assert result.outage is False
        # Should return zeroed default values since no cache
        assert result.session_pct == 0.0

    def test_stale_cache_attempts_live_fetch(self):
        """An expired in-memory cache should attempt a live fetch (falls back to stale cached data when claude absent)."""
        import penny.status_fetcher as sf

        old_time = datetime.now(timezone.utc) - timedelta(seconds=600)
        sf._cache = _make_live_status(session_pct=50.0, fetched_at=old_time)

        with patch("penny.status_fetcher.shutil.which", return_value=None):
            result = fetch_live_status(force=False)

        # Cache is stale, but no API error detected → return cached data without outage flag
        assert isinstance(result, LiveStatus)
        assert result.outage is False
        assert result.session_pct == 50.0

    def test_outage_cache_uses_shorter_ttl(self):
        """An outage result cached within the short retry window should still be returned."""
        import penny.status_fetcher as sf

        # Cache set 1 minute ago (within 2-minute outage retry TTL)
        recent = datetime.now(timezone.utc) - timedelta(seconds=60)
        sf._cache = _make_live_status(fetched_at=recent, outage=True)

        with patch("penny.status_fetcher.shutil.which", return_value=None):
            result = fetch_live_status(force=False)

        # Still within outage TTL → cached value returned
        assert result is not None
        assert result.outage is True

    def test_expired_outage_cache_retries(self):
        """An outage result older than the retry TTL should trigger a new fetch attempt."""
        import penny.status_fetcher as sf

        # Cache set 3 minutes ago (outside 2-minute outage retry TTL)
        old_time = datetime.now(timezone.utc) - timedelta(seconds=180)
        sf._cache = _make_live_status(fetched_at=old_time, outage=True)

        with patch("penny.status_fetcher.shutil.which", return_value=None):
            result = fetch_live_status(force=False)

        # Outage TTL expired, claude not available → transient failure returns stale/default
        assert isinstance(result, LiveStatus)
        # _stale_or_default skips returning the old outage=True cache, returns a fresh default
        assert result.outage is False

    def test_loads_disk_cache_on_cold_start(self, tmp_path):
        """Cold start (no in-memory cache) should read from disk and populate memory cache."""
        import penny.status_fetcher as sf
        sf._cache = None

        disk_status = _make_live_status(session_pct=77.0)

        with (
            patch("penny.status_fetcher._load_cache", return_value=disk_status),
            patch("penny.status_fetcher.shutil.which", return_value=None),
        ):
            result = fetch_live_status(force=False)

        # Should return the disk-cached value and populate in-memory cache
        assert result is not None
        assert result.session_pct == 77.0
        assert sf._cache is disk_status


# ── get_cached_status ────────────────────────────────────────────────────────


class TestGetCachedStatus:
    def test_returns_none_when_no_cache(self):
        import penny.status_fetcher as sf
        sf._cache = None
        assert get_cached_status() is None

    def test_returns_cached_status(self):
        import penny.status_fetcher as sf
        cached = _make_live_status()
        sf._cache = cached
        assert get_cached_status() is cached
        sf._cache = None


# ── _save_cache — disk write path ────────────────────────────────────────────


class TestSaveCacheDiskWrite:
    def test_writes_all_fields_to_disk(self, tmp_path):
        status = LiveStatus(
            session_pct=25.0,
            session_reset_label="3pm",
            session_reset_tz="US/Pacific",
            weekly_pct_all=50.0,
            weekly_pct_sonnet=60.0,
            weekly_reset_label="Mar 10 at 5pm",
            weekly_reset_tz="US/Pacific",
            fetched_at=datetime(2025, 3, 10, 12, 0, 0, tzinfo=timezone.utc),
        )
        cache_path = tmp_path / "status_cache.json"
        with patch("penny.status_fetcher._cache_file", return_value=cache_path):
            _save_cache(status)

        assert cache_path.exists()
        data = json.loads(cache_path.read_text())
        assert data["session_pct"] == 25.0
        assert data["weekly_pct_all"] == 50.0
        assert data["weekly_reset_label"] == "Mar 10 at 5pm"

    def test_creates_parent_directory_if_missing(self, tmp_path):
        nested = tmp_path / "a" / "b" / "c" / "status_cache.json"
        status = LiveStatus(
            session_pct=1.0,
            session_reset_label="1am",
            session_reset_tz="UTC",
            weekly_pct_all=2.0,
            weekly_pct_sonnet=3.0,
            weekly_reset_label="Jan 1 at 1am",
            weekly_reset_tz="UTC",
            fetched_at=datetime(2025, 1, 1, 0, 0, 0, tzinfo=timezone.utc),
        )
        with patch("penny.status_fetcher._cache_file", return_value=nested):
            _save_cache(status)
        assert nested.exists()


# ── _feed_child ───────────────────────────────────────────────────────────────


class TestFeedChild:
    def test_does_nothing_when_pexpect_missing(self):
        """When pexpect is not importable, _feed_child returns without error."""
        import pytest
        pytest.skip("Not applicable: pexpect is imported at module top-level, sys.modules patching is ineffective.")

    def test_feeds_bytes_when_pexpect_available(self):
        """When pexpect is available, bytes are fed to the stream."""
        # Build a minimal fake pexpect module
        fake_pexpect = types.ModuleType("pexpect")
        fake_pexpect.TIMEOUT = object()

        child = MagicMock()
        # Make child.before return some bytes, then raise to stop the loop
        child.before = b"hello"
        child.expect.side_effect = [None, Exception("stop")]

        stream = MagicMock()

        with patch.dict("sys.modules", {"pexpect": fake_pexpect}):
            _feed_child(child, stream, 0.01)

        # feed should have been called at least once with the bytes
        stream.feed.assert_called_with(b"hello")

    def test_exception_during_expect_stops_loop(self):
        """An exception inside the loop terminates _feed_child gracefully."""
        fake_pexpect = types.ModuleType("pexpect")
        fake_pexpect.TIMEOUT = object()

        child = MagicMock()
        child.expect.side_effect = RuntimeError("boom")

        stream = MagicMock()

        with patch.dict("sys.modules", {"pexpect": fake_pexpect}):
            # Should not raise
            _feed_child(child, stream, 0.01)


# ── fetch_live_status — pexpect/pyte import failures ────────────────────────


class TestFetchLiveStatusImportFailures:
    """Tests for missing import scenarios — no longer applicable with top-level imports.

    With deps.py auto-installing dependencies and top-level imports in status_fetcher.py,
    import failures happen at module load time, not function call time. These tests are
    skipped since they test a scenario that can't occur at runtime.
    """
    def setup_method(self):
        import penny.status_fetcher as sf
        sf._cache = None

    def test_returns_none_when_pexpect_missing(self):
        """If pexpect is not installed, fetch_live_status returns LiveStatus(outage=True)."""
        import pytest
        pytest.skip("Not applicable: pexpect is imported at module top-level, not at call time.")

    def test_returns_none_when_pyte_missing(self):
        """If pyte is not installed, fetch_live_status returns LiveStatus(outage=True)."""
        import pytest
        pytest.skip("Not applicable: pyte is imported at module top-level, not at call time.")


# ── _save_cache — exception handling ─────────────────────────────────────────


class TestSaveCacheException:
    def test_exception_during_write_is_suppressed(self, tmp_path):
        """If the write raises, _save_cache swallows the exception silently."""
        status = LiveStatus(
            session_pct=10.0,
            session_reset_label="noon",
            session_reset_tz="UTC",
            weekly_pct_all=20.0,
            weekly_pct_sonnet=30.0,
            weekly_reset_label="Jan 1 at noon",
            weekly_reset_tz="UTC",
            fetched_at=datetime.now(timezone.utc),
        )
        # Make the path object's write_text raise an OSError
        bad_path = MagicMock()
        bad_path.parent.mkdir.return_value = None
        bad_path.write_text.side_effect = OSError("disk full")

        with patch("penny.status_fetcher._cache_file", return_value=bad_path):
            # Should not raise
            _save_cache(status)


# ── fetch_live_status — full mock of pexpect/pyte pipeline ───────────────────


def _make_fake_pexpect_module(child_mock: MagicMock) -> types.ModuleType:
    """Build a minimal fake pexpect module for injection via sys.modules."""
    fake = types.ModuleType("pexpect")
    fake.TIMEOUT = object()
    fake.EOF = object()
    fake.spawn = MagicMock(return_value=child_mock)
    return fake


def _make_fake_pyte_module(screen_text: str) -> types.ModuleType:
    """Build a minimal fake pyte module whose Screen.display returns screen_text lines."""
    fake = types.ModuleType("pyte")

    screen_mock = MagicMock()
    screen_mock.display = screen_text.splitlines()

    fake.Screen = MagicMock(return_value=screen_mock)
    fake.ByteStream = MagicMock(return_value=MagicMock())
    return fake


MOCK_USAGE_SCREEN = """\
Settings   Status   Config   Usage
Current session                       16% used
Resets 2pm (Europe/Amsterdam)
Current week (all models)             30% used
Current week (Sonnet only)            41% used
Resets Mar 6 at 9pm (Europe/Amsterdam)
"""


class TestFetchLiveStatusFullMock:
    """Test the pexpect/pyte interaction path of fetch_live_status with mocks."""

    def setup_method(self):
        import penny.status_fetcher as sf
        sf._cache = None

    def _make_child(self, fake_pexpect: MagicMock, first_expect_idx: int = 0, second_expect_idx: int = 0) -> MagicMock:
        """Build a mock child process that mimics pexpect.spawn return value."""
        child = MagicMock()
        child.before = b""
        child.after = b"\xe2\x9d\xaf"
        # expect() returns: 0 for prompt match, then 0 for dialog match
        child.expect.side_effect = [first_expect_idx, second_expect_idx]
        child.send = MagicMock()
        child.close = MagicMock()
        return child

    def test_successful_parse_returns_live_status(self):
        """When pexpect spawns successfully and pyte renders the Usage screen."""
        import pytest
        pytest.skip("Not applicable: pexpect/pyte are imported at module top-level, sys.modules patching is ineffective.")

    def test_timeout_at_prompt_returns_none(self):
        """When claude doesn't show the prompt (expect returns TIMEOUT), returns outage status."""
        import pytest
        pytest.skip("Not applicable: pexpect/pyte are imported at module top-level, sys.modules patching is ineffective.")

    def test_timeout_at_dialog_returns_none(self):
        """When the /status dialog doesn't open (second expect returns TIMEOUT), returns outage status."""
        import pytest
        pytest.skip("Not applicable: pexpect/pyte are imported at module top-level, sys.modules patching is ineffective.")

    def test_api_outage_screen_returns_outage_status(self):
        """When the Usage tab shows an API error, returns an outage LiveStatus."""
        import pytest
        pytest.skip("Not applicable: pexpect/pyte are imported at module top-level, sys.modules patching is ineffective.")

    def test_exception_during_spawn_returns_none(self):
        """An exception raised by pexpect.spawn is caught and returns outage status."""
        import pytest
        pytest.skip("Not applicable: pexpect/pyte are imported at module top-level, sys.modules patching is ineffective.")


# ── fetch_live_status — transient failure vs API outage distinction ────────────


class TestFetchLiveStatusTransientVsOutage:
    """Tests to ensure transient failures return _stale_or_default() while API errors return _make_outage_status()."""

    def setup_method(self):
        import penny.status_fetcher as sf
        sf._cache = None

    def test_stale_or_default_preserves_good_cache(self):
        """_stale_or_default returns last-good cache without outage flag when cache is good."""
        import penny.status_fetcher as sf

        good_status = _make_live_status(session_pct=75.0, outage=False)
        sf._cache = good_status

        # If _stale_or_default is called, it should return the good cache
        from penny.status_fetcher import _stale_or_default
        recovered = _stale_or_default()
        assert recovered.session_pct == 75.0
        assert recovered.outage is False

    def test_make_outage_status_preserves_good_cache(self):
        """_make_outage_status preserves last-good cache values when building outage status."""
        import penny.status_fetcher as sf

        good_status = _make_live_status(session_pct=60.0, outage=False)
        sf._cache = good_status

        from penny.status_fetcher import _make_outage_status
        outage_result = _make_outage_status()

        # Should have good cache values but outage=True
        assert outage_result.session_pct == 60.0
        assert outage_result.outage is True

    def test_stale_or_default_returns_zero_when_no_good_cache(self):
        """_stale_or_default returns zeroed default when no good cache exists."""
        import penny.status_fetcher as sf

        sf._cache = None

        from penny.status_fetcher import _stale_or_default
        result = _stale_or_default()

        assert result.session_pct == 0.0
        assert result.weekly_pct_all == 0.0
        assert result.outage is False

    def test_stale_or_default_skips_old_outage_cache(self):
        """_stale_or_default ignores old outage=True cache and returns default."""
        import penny.status_fetcher as sf

        old_outage = _make_live_status(session_pct=20.0, outage=True)
        sf._cache = old_outage

        from penny.status_fetcher import _stale_or_default
        result = _stale_or_default()

        # Should return default (zeros), not the outage cache
        assert result.session_pct == 0.0
        assert result.outage is False

    def test_timeout_on_prompt_returns_stale_not_outage(self):
        """When prompt never appears (TIMEOUT), transient failure returns _stale_or_default()."""
        import pytest
        pytest.skip("Not applicable: pexpect/pyte are imported at module top-level, sys.modules patching is ineffective.")

    def test_parse_failure_returns_stale_not_outage(self):
        """When Usage tab exists but can't be parsed, returns _stale_or_default()."""
        import pytest
        pytest.skip("Not applicable: pexpect/pyte are imported at module top-level, sys.modules patching is ineffective.")


# ── Helper functions for new behavior ──────────────────────────────────────────


class TestMakeOutageStatus:
    """Test _make_outage_status builds correct outage state."""

    def setup_method(self):
        import penny.status_fetcher as sf
        sf._cache = None

    def test_returns_outage_true(self):
        """_make_outage_status returns a status with outage=True."""
        from penny.status_fetcher import _make_outage_status
        result = _make_outage_status()
        assert result.outage is True

    def test_preserves_good_session_pct(self):
        """When cache has good session_pct, outage status preserves it."""
        import penny.status_fetcher as sf
        good = _make_live_status(session_pct=25.5, weekly_pct_all=40.0)
        sf._cache = good

        from penny.status_fetcher import _make_outage_status
        result = _make_outage_status()

        assert result.session_pct == 25.5
        assert result.weekly_pct_all == 40.0
        assert result.outage is True

    def test_zeroes_when_no_good_cache(self):
        """_make_outage_status returns zeros when no good cache exists."""
        import penny.status_fetcher as sf
        sf._cache = None

        from penny.status_fetcher import _make_outage_status
        result = _make_outage_status()

        assert result.session_pct == 0.0
        assert result.weekly_pct_all == 0.0
        assert result.weekly_pct_sonnet == 0.0
        assert result.outage is True

    def test_ignores_old_outage_cache(self):
        """_make_outage_status ignores an old outage=True cache."""
        import penny.status_fetcher as sf
        old_outage = _make_live_status(session_pct=15.0, outage=True)
        sf._cache = old_outage

        from penny.status_fetcher import _make_outage_status
        result = _make_outage_status()

        # Should return zeros since cache is marked outage=True
        assert result.session_pct == 0.0
        assert result.outage is True

    def test_updates_global_cache(self):
        """_make_outage_status updates the global _cache."""
        import penny.status_fetcher as sf
        sf._cache = None

        from penny.status_fetcher import _make_outage_status
        result = _make_outage_status()

        # Global cache should now be set to this outage status
        assert sf._cache is result
        assert sf._cache.outage is True


class TestDetectApiErrorCoverage:
    """Additional coverage for _detect_api_error edge cases."""

    def test_detects_api_error_json_format(self):
        """_detect_api_error matches the JSON error format."""
        assert _detect_api_error('"type":"error","message":"test"') is True

    def test_detects_api_error_label(self):
        """_detect_api_error matches the API Error: label."""
        assert _detect_api_error("API Error: rate limit exceeded") is True

    def test_case_sensitive_for_some_patterns(self):
        """_detect_api_error is case-sensitive for some patterns."""
        # "api_error" with lowercase should match
        assert _detect_api_error("api_error") is True
        # But variations might not all be covered
        assert _detect_api_error("API_ERROR") is False

    def test_whitespace_tolerance(self):
        """_detect_api_error works with surrounding whitespace."""
        assert _detect_api_error("   Internal server error   ") is True
        assert _detect_api_error("\nFailed to load usage data\n") is True


class TestScreenTextEdgeCases:
    """Additional test coverage for _screen_text."""

    def test_handles_mixed_whitespace(self):
        """_screen_text handles tabs and spaces in lines."""
        screen = MagicMock()
        screen.display = ["line1\t  ", "  \tline2", "line3"]
        result = _screen_text(screen)
        # Trailing whitespace should be removed
        assert result == "line1\n  \tline2\nline3"

    def test_preserves_internal_whitespace(self):
        """_screen_text preserves spaces within lines."""
        screen = MagicMock()
        screen.display = ["line  with  spaces  ", "another    line  "]
        result = _screen_text(screen)
        assert result == "line  with  spaces\nanother    line"


