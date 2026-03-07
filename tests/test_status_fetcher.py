"""Unit tests for penny/status_fetcher.py — parsing and caching logic."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from penny.status_fetcher import (
    LiveStatus,
    _cache_file,
    _detect_api_error,
    _load_cache,
    _parse_usage_screen,
    _save_cache,
    fetch_live_status,
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

    def test_parses_weekly_reset(self):
        result = _parse_usage_screen(SAMPLE_USAGE_SCREEN)
        assert result is not None
        assert result.weekly_reset_label == "Mar 6 at 9pm"
        assert result.weekly_reset_tz == "Europe/Amsterdam"

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
            fetched_at=datetime.now(timezone.utc),
        )
        result = status_as_prediction_overrides(status)
        assert result["session_pct_all"] == 16.0
        assert result["pct_all"] == 30.0
        assert result["pct_sonnet"] == 41.0
        assert result["reset_label"] == "Mar 6 at 9pm"
        assert result["session_reset_label"] == "2pm"
        assert result["reset_tz"] == "Europe/Amsterdam"


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
        """force=True skips TTL but returns None when claude is not available."""
        import penny.status_fetcher as sf

        cached = _make_live_status(session_pct=99.0)
        sf._cache = cached

        with patch("penny.status_fetcher.shutil.which", return_value=None):
            result = fetch_live_status(force=True)

        assert result is None

    def test_returns_none_when_claude_not_in_path(self):
        """No in-memory or disk cache, and claude is absent → returns None."""
        import penny.status_fetcher as sf
        sf._cache = None

        with (
            patch("penny.status_fetcher.shutil.which", return_value=None),
            patch("penny.status_fetcher._load_cache", return_value=None),
        ):
            result = fetch_live_status()

        assert result is None

    def test_stale_cache_attempts_live_fetch(self):
        """An expired in-memory cache should attempt a live fetch (falls back to None when claude absent)."""
        import penny.status_fetcher as sf

        old_time = datetime.now(timezone.utc) - timedelta(seconds=600)
        sf._cache = _make_live_status(session_pct=50.0, fetched_at=old_time)

        with patch("penny.status_fetcher.shutil.which", return_value=None):
            result = fetch_live_status(force=False)

        # Cache is stale, claude not available → None
        assert result is None

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

        # Outage TTL expired, claude not available → None
        assert result is None

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
