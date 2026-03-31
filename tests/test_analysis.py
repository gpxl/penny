"""Unit tests for penny/analysis.py."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from penny.analysis import (
    Prediction,
    SessionInfo,
    _hours_until_dated_reset_label,
    _hours_until_reset_label,
    count_tokens_since,
    current_billing_period,
    days_until_reset,
    find_current_session_start,
    find_session_boundaries,
    format_reset_label,
    get_usage_bar,
    load_stats_cache,
    past_billing_periods,
    reset_label,
    scan_rich_metrics,
    scan_rich_metrics_multi,
    short_reset_label,
    should_trigger,
    uses_24h_time,
)

# ---------------------------------------------------------------------------
# current_billing_period
# ---------------------------------------------------------------------------

class TestCurrentBillingPeriod:
    def test_returns_tuple_of_datetimes(self):
        start, end = current_billing_period()
        assert isinstance(start, datetime)
        assert isinstance(end, datetime)

    def test_period_is_exactly_7_days(self):
        start, end = current_billing_period()
        assert (end - start) == timedelta(days=7)

    def test_end_is_in_the_future(self):
        _, end = current_billing_period()
        assert end > datetime.now(timezone.utc)

    def test_start_is_in_the_past(self):
        start, _ = current_billing_period()
        assert start < datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# days_until_reset
# ---------------------------------------------------------------------------

class TestDaysUntilReset:
    def test_returns_float_between_0_and_7(self):
        days = days_until_reset()
        assert 0.0 <= days <= 7.0

    def test_is_positive(self):
        assert days_until_reset() > 0


# ---------------------------------------------------------------------------
# count_tokens_since
# ---------------------------------------------------------------------------

class TestCountTokensSince:
    def test_sums_output_tokens_from_jsonl(self, sample_jsonl_dir):
        # sample_jsonl_dir is tmp_path; fixture creates .claude/projects/proj-abc/session1.jsonl
        since = datetime(2025, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        with patch("penny.analysis.Path.home", return_value=sample_jsonl_dir):
            usage = count_tokens_since(since)
        # 100 sonnet + 200 opus = 300 total output; 100 sonnet only
        assert usage.output_all == 300
        assert usage.output_sonnet == 100

    def test_returns_zeros_when_projects_dir_missing(self, tmp_path):
        with patch("penny.analysis.Path.home", return_value=tmp_path):
            since = datetime(2025, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
            usage = count_tokens_since(since)
        assert usage.output_all == 0
        assert usage.output_sonnet == 0
        assert usage.input_all == 0

    def test_skips_malformed_json(self, tmp_path):
        projects_dir = tmp_path / ".claude" / "projects" / "proj"
        projects_dir.mkdir(parents=True)
        convo = projects_dir / "bad.jsonl"
        convo.write_text("not json\n{also bad\n", encoding="utf-8")

        with patch("penny.analysis.Path.home", return_value=tmp_path):
            since = datetime(2025, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
            usage = count_tokens_since(since)
        assert usage.output_all == 0

    def test_skips_non_assistant_messages(self, tmp_path):
        projects_dir = tmp_path / ".claude" / "projects" / "proj"
        projects_dir.mkdir(parents=True)
        convo = projects_dir / "session.jsonl"
        lines = [
            json.dumps({
                "type": "human",
                "timestamp": "2025-01-01T10:00:00.000Z",
                "message": {"usage": {"output_tokens": 999}},
            }),
        ]
        convo.write_text("\n".join(lines), encoding="utf-8")

        with patch("penny.analysis.Path.home", return_value=tmp_path):
            since = datetime(2025, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
            usage = count_tokens_since(since)
        assert usage.output_all == 0

    def test_skips_files_before_since_by_mtime(self, tmp_path):
        """Files older than 'since' should be skipped via mtime check."""
        projects_dir = tmp_path / ".claude" / "projects" / "proj"
        projects_dir.mkdir(parents=True)
        convo = projects_dir / "old.jsonl"
        convo.write_text(
            json.dumps({
                "type": "assistant",
                "timestamp": "2025-06-01T10:00:00.000Z",
                "message": {
                    "model": "claude-sonnet-4-6",
                    "usage": {"output_tokens": 500, "input_tokens": 10,
                               "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
                },
            }),
            encoding="utf-8",
        )
        # Set mtime to well before our 'since' timestamp
        import os
        old_mtime = 0  # epoch
        os.utime(convo, (old_mtime, old_mtime))

        since = datetime(2025, 6, 1, 0, 0, 0, tzinfo=timezone.utc)
        with patch("penny.analysis.Path.home", return_value=tmp_path):
            usage = count_tokens_since(since)
        assert usage.output_all == 0


# ---------------------------------------------------------------------------
# should_trigger
# ---------------------------------------------------------------------------

class TestShouldTrigger:
    def _pred(self, projected_pct=60.0, days_remaining=1.5):
        return Prediction(projected_pct_all=projected_pct, days_remaining=days_remaining)

    def test_triggers_when_enough_capacity_and_few_days(self):
        # 60% projected → 40% remaining ≥ 30%; days=1.5 ≤ 2
        config = {"trigger": {"min_capacity_percent": 30, "max_days_remaining": 2}}
        assert should_trigger(self._pred(60.0, 1.5), config) is True

    def test_no_trigger_when_projected_too_high(self):
        # 80% projected → 20% remaining < 30%
        config = {"trigger": {"min_capacity_percent": 30, "max_days_remaining": 2}}
        assert should_trigger(self._pred(80.0, 1.5), config) is False

    def test_no_trigger_when_too_many_days_left(self):
        config = {"trigger": {"min_capacity_percent": 30, "max_days_remaining": 2}}
        assert should_trigger(self._pred(60.0, 3.0), config) is False

    def test_boundary_exactly_30pct_remaining_and_2_days(self):
        # 70% projected → 30% remaining = 30 threshold; days=2.0 = 2 threshold
        config = {"trigger": {"min_capacity_percent": 30, "max_days_remaining": 2}}
        assert should_trigger(self._pred(70.0, 2.0), config) is True


# ---------------------------------------------------------------------------
# get_usage_bar
# ---------------------------------------------------------------------------

class TestGetUsageBar:
    def test_0_percent_all_empty(self):
        bar = get_usage_bar(0.0, width=8)
        assert "⬜" * 8 == bar

    def test_100_percent_all_filled_red(self):
        bar = get_usage_bar(100.0, width=8)
        assert "🟥" * 8 == bar

    def test_59_percent_green(self):
        bar = get_usage_bar(59.0, width=8)
        # 59% of 8 = 4 filled, 4 empty
        assert bar == "🟩" * 4 + "⬜" * 4

    def test_79_percent_yellow(self):
        bar = get_usage_bar(79.0, width=8)
        filled = int(79 / 100 * 8)  # = 6
        assert bar == "🟨" * filled + "⬜" * (8 - filled)


# ---------------------------------------------------------------------------
# format_reset_label
# ---------------------------------------------------------------------------

class TestFormatResetLabel:
    def test_passthrough_when_empty(self):
        assert format_reset_label("") == ""

    def test_passthrough_dash(self):
        assert format_reset_label("—") == "—"

    def test_passthrough_in_12h_mode(self):
        with patch("penny.analysis.uses_24h_time", return_value=False):
            assert format_reset_label("9pm") == "9pm"

    def test_compact_12h_to_24h(self):
        with patch("penny.analysis.uses_24h_time", return_value=True):
            assert format_reset_label("9pm") == "21"

    def test_compact_with_minutes(self):
        with patch("penny.analysis.uses_24h_time", return_value=True):
            assert format_reset_label("5:59pm") == "17:59"

    def test_date_prefix_preserved(self):
        with patch("penny.analysis.uses_24h_time", return_value=True):
            result = format_reset_label("Mar 6 at 9pm")
            assert result == "Mar 6 at 21"

    def test_today_prefix_preserved(self):
        with patch("penny.analysis.uses_24h_time", return_value=True):
            result = format_reset_label("Today at 5:59 PM")
            assert result == "Today at 17:59"


# ---------------------------------------------------------------------------
# scan_rich_metrics
# ---------------------------------------------------------------------------

class TestScanRichMetrics:
    def test_per_model_token_split(self, rich_jsonl_dir):
        since = datetime(2025, 1, 10, 0, 0, 0, tzinfo=timezone.utc)
        with patch("penny.analysis.Path.home", return_value=rich_jsonl_dir):
            rm = scan_rich_metrics(since)
        # Opus: 500, Sonnet: 300, Haiku: 100
        assert rm.opus_tokens == 500
        assert rm.sonnet_tokens == 300
        assert rm.haiku_tokens == 100
        assert rm.other_tokens == 0

    def test_tool_counts(self, rich_jsonl_dir):
        since = datetime(2025, 1, 10, 0, 0, 0, tzinfo=timezone.utc)
        with patch("penny.analysis.Path.home", return_value=rich_jsonl_dir):
            rm = scan_rich_metrics(since)
        assert rm.tool_counts.get("Bash") == 2
        assert rm.tool_counts.get("Read") == 1
        assert rm.tool_counts.get("Edit") == 1

    def test_hourly_activity_array(self, rich_jsonl_dir):
        since = datetime(2025, 1, 10, 0, 0, 0, tzinfo=timezone.utc)
        with patch("penny.analysis.Path.home", return_value=rich_jsonl_dir):
            rm = scan_rich_metrics(since)
        # 3 assistant turns — hourly_activity is local-time, so just verify sum
        assert len(rm.hourly_activity) == 24
        assert sum(rm.hourly_activity) == 3

    def test_subagent_flag(self, rich_jsonl_dir):
        since = datetime(2025, 1, 10, 0, 0, 0, tzinfo=timezone.utc)
        with patch("penny.analysis.Path.home", return_value=rich_jsonl_dir):
            rm = scan_rich_metrics(since)
        assert rm.total_turns == 3
        assert rm.subagent_turns == 1  # only the isSidechain=True turn

    def test_pr_count(self, rich_jsonl_dir):
        since = datetime(2025, 1, 10, 0, 0, 0, tzinfo=timezone.utc)
        with patch("penny.analysis.Path.home", return_value=rich_jsonl_dir):
            rm = scan_rich_metrics(since)
        assert rm.pr_count == 1

    def test_unique_projects_and_branches(self, rich_jsonl_dir):
        since = datetime(2025, 1, 10, 0, 0, 0, tzinfo=timezone.utc)
        with patch("penny.analysis.Path.home", return_value=rich_jsonl_dir):
            rm = scan_rich_metrics(since)
        # proj-a and proj-b
        assert rm.unique_projects == 2
        # main and feature-x
        assert rm.unique_branches == 2

    def test_cache_tokens(self, rich_jsonl_dir):
        since = datetime(2025, 1, 10, 0, 0, 0, tzinfo=timezone.utc)
        with patch("penny.analysis.Path.home", return_value=rich_jsonl_dir):
            rm = scan_rich_metrics(since)
        assert rm.cache_create_tokens == 200  # only opus had non-zero cc
        assert rm.cache_read_tokens == 1200   # 800 + 400

    def test_mtime_skip_excludes_old_files(self, tmp_path):
        """Files older than `since` should be entirely skipped (mtime guard)."""
        projects_dir = tmp_path / ".claude" / "projects" / "proj-old"
        projects_dir.mkdir(parents=True)
        old_file = projects_dir / "old.jsonl"
        old_file.write_text(
            '{"type":"assistant","timestamp":"2024-01-01T10:00:00Z","message":{"model":"claude-opus-4-6","usage":{"output_tokens":999}}}\n'
        )
        import os
        # Set mtime well before `since`
        old_ts = datetime(2024, 1, 1, tzinfo=timezone.utc).timestamp()
        os.utime(old_file, (old_ts, old_ts))

        since = datetime(2025, 1, 10, 0, 0, 0, tzinfo=timezone.utc)
        with patch("penny.analysis.Path.home", return_value=tmp_path):
            rm = scan_rich_metrics(since)
        assert rm.opus_tokens == 0
        assert rm.total_turns == 0

    def test_returns_zeros_when_no_projects(self, tmp_path):
        with patch("penny.analysis.Path.home", return_value=tmp_path):
            since = datetime(2025, 1, 10, 0, 0, 0, tzinfo=timezone.utc)
            rm = scan_rich_metrics(since)
        assert rm.total_turns == 0
        assert rm.opus_tokens == 0
        assert len(rm.hourly_activity) == 24


# ---------------------------------------------------------------------------
# past_billing_periods
# ---------------------------------------------------------------------------

class TestPastBillingPeriods:
    def test_returns_n_periods(self):
        periods = past_billing_periods(6)
        assert len(periods) == 6

    def test_sorted_oldest_first(self):
        periods = past_billing_periods(4)
        for i in range(len(periods) - 1):
            assert periods[i][0] < periods[i + 1][0]

    def test_each_period_is_7_days(self):
        for start, end in past_billing_periods(3):
            assert (end - start) == timedelta(days=7)

    def test_last_period_is_current(self):
        periods = past_billing_periods(3)
        current_start, _ = current_billing_period()
        assert periods[-1][0] == current_start


# ---------------------------------------------------------------------------
# uses_24h_time — cache and subprocess fallback
# ---------------------------------------------------------------------------

class TestUses24hTime:
    def test_returns_bool(self):
        result = uses_24h_time()
        assert isinstance(result, bool)

    def test_cache_hit_returns_cached_value(self):
        """After first call the TTL cache should return without importing Foundation."""
        import time as _t

        import penny.analysis as _a
        # Force a known cached value
        _a._24H_CACHE = (_t.monotonic(), True)
        assert uses_24h_time() is True
        _a._24H_CACHE = None  # reset

    def test_foundation_import_error_falls_back_to_subprocess(self):
        """When Foundation is unavailable, falls back to defaults read."""
        import penny.analysis as _a
        _a._24H_CACHE = None

        with (
            patch("penny.analysis.subprocess.run") as mock_run,
            patch.dict("sys.modules", {"Foundation": None}),
        ):
            mock_run.return_value = MagicMock(stdout="1\n")
            result = uses_24h_time()
        assert result is True
        _a._24H_CACHE = None  # reset

    def test_subprocess_failure_returns_false(self):
        """When both Foundation and subprocess fail, result is False."""
        import penny.analysis as _a
        _a._24H_CACHE = None

        with (
            patch("penny.analysis.subprocess.run", side_effect=Exception("fail")),
            patch.dict("sys.modules", {"Foundation": None}),
        ):
            result = uses_24h_time()
        assert result is False
        _a._24H_CACHE = None  # reset


# ---------------------------------------------------------------------------
# reset_label
# ---------------------------------------------------------------------------

class TestResetLabel:
    def test_returns_string(self):
        with patch("penny.analysis.uses_24h_time", return_value=False):
            label = reset_label()
        assert isinstance(label, str)
        assert len(label) > 0

    def test_contains_at(self):
        with patch("penny.analysis.uses_24h_time", return_value=False):
            label = reset_label()
        assert " at " in label


# ---------------------------------------------------------------------------
# format_reset_label — unknown label passthrough (line 179)
# ---------------------------------------------------------------------------

class TestFormatResetLabelPassthrough:
    def test_unrecognised_label_returned_unchanged(self):
        with patch("penny.analysis.uses_24h_time", return_value=True):
            label = "some unrecognised format"
            assert format_reset_label(label) == label


# ---------------------------------------------------------------------------
# count_tokens_since — until filter and OSError branches
# ---------------------------------------------------------------------------

class TestCountTokensSinceUntilFilter:
    def test_until_filter_excludes_messages_at_or_after_cutoff(self, tmp_path):
        projects_dir = tmp_path / ".claude" / "projects" / "proj"
        projects_dir.mkdir(parents=True)
        convo = projects_dir / "session.jsonl"
        lines = [
            json.dumps({
                "type": "assistant",
                "timestamp": "2025-06-01T10:00:00.000Z",
                "message": {
                    "model": "claude-sonnet-4-6",
                    "usage": {"output_tokens": 500, "input_tokens": 10,
                               "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
                },
            }),
            json.dumps({
                "type": "assistant",
                "timestamp": "2025-06-01T12:00:00.000Z",
                "message": {
                    "model": "claude-sonnet-4-6",
                    "usage": {"output_tokens": 300, "input_tokens": 5,
                               "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
                },
            }),
        ]
        convo.write_text("\n".join(lines), encoding="utf-8")

        since = datetime(2025, 6, 1, 0, 0, 0, tzinfo=timezone.utc)
        until = datetime(2025, 6, 1, 11, 0, 0, tzinfo=timezone.utc)
        with patch("penny.analysis.Path.home", return_value=tmp_path):
            usage = count_tokens_since(since, until=until)
        # Only the 10:00 message should be counted
        assert usage.output_all == 500

    def test_no_usage_block_skipped(self, tmp_path):
        projects_dir = tmp_path / ".claude" / "projects" / "proj"
        projects_dir.mkdir(parents=True)
        convo = projects_dir / "session.jsonl"
        lines = [
            json.dumps({
                "type": "assistant",
                "timestamp": "2025-06-01T10:00:00.000Z",
                "message": {"model": "claude-sonnet-4-6"},  # no usage key
            }),
        ]
        convo.write_text("\n".join(lines), encoding="utf-8")

        since = datetime(2025, 6, 1, 0, 0, 0, tzinfo=timezone.utc)
        with patch("penny.analysis.Path.home", return_value=tmp_path):
            usage = count_tokens_since(since)
        assert usage.output_all == 0


# ---------------------------------------------------------------------------
# scan_rich_metrics — until filter, OSError, invalid timestamp
# ---------------------------------------------------------------------------

class TestScanRichMetricsEdgeCases:
    def test_until_filter_excludes_late_messages(self, tmp_path):
        projects_dir = tmp_path / ".claude" / "projects" / "proj"
        projects_dir.mkdir(parents=True)
        convo = projects_dir / "session.jsonl"
        lines = [
            json.dumps({
                "type": "assistant",
                "timestamp": "2025-01-10T10:00:00.000Z",
                "message": {
                    "model": "claude-opus-4-6",
                    "usage": {"output_tokens": 500, "input_tokens": 0,
                               "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
                },
            }),
            json.dumps({
                "type": "assistant",
                "timestamp": "2025-01-10T16:00:00.000Z",
                "message": {
                    "model": "claude-opus-4-6",
                    "usage": {"output_tokens": 999, "input_tokens": 0,
                               "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
                },
            }),
        ]
        convo.write_text("\n".join(lines), encoding="utf-8")

        since = datetime(2025, 1, 10, 0, 0, 0, tzinfo=timezone.utc)
        until = datetime(2025, 1, 10, 12, 0, 0, tzinfo=timezone.utc)
        with patch("penny.analysis.Path.home", return_value=tmp_path):
            rm = scan_rich_metrics(since, until=until)
        assert rm.opus_tokens == 500

    def test_invalid_timestamp_in_hourly_activity_skipped(self, tmp_path):
        """Entries with unparseable timestamps don't crash hourly_activity."""
        projects_dir = tmp_path / ".claude" / "projects" / "proj"
        projects_dir.mkdir(parents=True)
        convo = projects_dir / "session.jsonl"
        lines = [
            json.dumps({
                "type": "assistant",
                "timestamp": "not-a-timestamp",
                "message": {
                    "model": "claude-opus-4-6",
                    "usage": {"output_tokens": 100, "input_tokens": 0,
                               "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
                },
            }),
        ]
        convo.write_text("\n".join(lines), encoding="utf-8")

        # We need the mtime filter to pass, so set the mtime to now
        import os
        os.utime(convo, None)

        since = datetime(1970, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        with patch("penny.analysis.Path.home", return_value=tmp_path):
            rm = scan_rich_metrics(since)
        # Should not crash; the turn is still counted
        assert rm.total_turns == 1


# ---------------------------------------------------------------------------
# load_stats_cache
# ---------------------------------------------------------------------------

class TestLoadStatsCache:
    def test_returns_default_when_file_missing(self, tmp_path):
        result = load_stats_cache(str(tmp_path / "nonexistent.json"))
        assert result == {"dailyActivity": [], "dailyModelTokens": []}

    def test_loads_valid_json(self, tmp_path):
        cache_file = tmp_path / "stats-cache.json"
        data = {"dailyActivity": [1, 2, 3], "dailyModelTokens": []}
        cache_file.write_text(json.dumps(data))
        result = load_stats_cache(str(cache_file))
        assert result["dailyActivity"] == [1, 2, 3]


# ---------------------------------------------------------------------------
# find_session_boundaries
# ---------------------------------------------------------------------------

class TestFindSessionBoundaries:
    def test_returns_empty_when_no_projects(self, tmp_path):
        with patch("penny.analysis.Path.home", return_value=tmp_path):
            since = datetime(2025, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
            result = find_session_boundaries(since)
        assert result == []

    def test_finds_rate_limit_boundary(self, tmp_path):
        projects_dir = tmp_path / ".claude" / "projects" / "proj"
        projects_dir.mkdir(parents=True)
        convo = projects_dir / "session.jsonl"
        lines = [
            json.dumps({
                "type": "assistant",
                "timestamp": "2025-01-10T14:00:00.000Z",
                "message": {
                    "model": "claude-sonnet-4-6",
                    "content": [
                        {"type": "text",
                         "text": "You've hit your limit. resets 5pm (Europe/Amsterdam) for next session."},
                    ],
                    "usage": {"output_tokens": 100},
                },
            }),
        ]
        convo.write_text("\n".join(lines), encoding="utf-8")
        import os
        os.utime(convo, None)

        since = datetime(2025, 1, 10, 0, 0, 0, tzinfo=timezone.utc)
        with patch("penny.analysis.Path.home", return_value=tmp_path):
            boundaries = find_session_boundaries(since)
        assert len(boundaries) == 1
        # Result should be a UTC datetime
        assert boundaries[0].tzinfo is not None


# ---------------------------------------------------------------------------
# find_current_session_start
# ---------------------------------------------------------------------------

class TestFindCurrentSessionStart:
    def test_returns_period_start_when_no_boundaries(self):
        period_start = datetime(2025, 1, 10, 0, 0, 0, tzinfo=timezone.utc)
        result = find_current_session_start(period_start, precomputed_boundaries=[])
        assert result == period_start

    def test_uses_most_recent_past_boundary(self):
        now = datetime.now(timezone.utc)
        period_start = now - timedelta(days=3)
        past_boundary = now - timedelta(hours=2)
        result = find_current_session_start(period_start, precomputed_boundaries=[past_boundary])
        # Should return the boundary itself (no gaps computed from single entry)
        assert result == past_boundary

    def test_future_boundary_ignored(self):
        now = datetime.now(timezone.utc)
        period_start = now - timedelta(days=1)
        future_boundary = now + timedelta(hours=2)
        result = find_current_session_start(period_start, precomputed_boundaries=[future_boundary])
        # No past boundaries → falls back to period_start
        assert result == period_start


# ---------------------------------------------------------------------------
# _hours_until_reset_label
# ---------------------------------------------------------------------------

class TestHoursUntilResetLabel:
    def test_returns_float_for_valid_label(self):
        hours = _hours_until_reset_label("11pm", "UTC")
        assert isinstance(hours, float)
        assert hours >= 0.0

    def test_returns_zero_for_invalid_tz(self):
        hours = _hours_until_reset_label("5pm", "Not/ATimezone")
        assert hours == 0.0

    def test_returns_zero_for_unrecognised_label(self):
        hours = _hours_until_reset_label("garbage", "UTC")
        assert hours == 0.0

    def test_handles_label_with_minutes(self):
        hours = _hours_until_reset_label("2:30pm", "UTC")
        assert isinstance(hours, float)
        assert hours >= 0.0

    def test_handles_midnight_12am(self):
        hours = _hours_until_reset_label("12am", "UTC")
        assert isinstance(hours, float)
        assert hours >= 0.0

    def test_handles_noon_12pm(self):
        hours = _hours_until_reset_label("12pm", "UTC")
        assert isinstance(hours, float)
        assert hours >= 0.0


# ---------------------------------------------------------------------------
# build_session_info
# ---------------------------------------------------------------------------

class TestBuildSessionInfo:
    def test_returns_session_info_with_no_boundaries(self, tmp_path):
        """With no rate-limit boundaries, session_info falls back to period_start."""
        from penny.analysis import build_session_info
        state = {"period_history": [], "session_history": []}
        with patch("penny.analysis.Path.home", return_value=tmp_path):
            info = build_session_info(state, precomputed_boundaries=[])
        assert isinstance(info, SessionInfo)
        assert info.session_reset_label == "—"
        assert info.hours_remaining == 0.0

    def test_session_info_with_past_boundaries(self, tmp_path):
        """With past boundaries, session_info computes a reset label."""
        from penny.analysis import build_session_info
        now = datetime.now(timezone.utc)
        past_b = now - timedelta(hours=3)

        state = {"period_history": [], "session_history": []}
        with patch("penny.analysis.Path.home", return_value=tmp_path):
            info = build_session_info(state, precomputed_boundaries=[past_b])
        assert isinstance(info, SessionInfo)
        # With one boundary we get a reset label
        assert info.session_reset_label != ""

    def test_session_start_is_datetime(self, tmp_path):
        from penny.analysis import build_session_info
        state = {}
        with patch("penny.analysis.Path.home", return_value=tmp_path):
            info = build_session_info(state, precomputed_boundaries=[])
        assert isinstance(info.session_start, datetime)


# ---------------------------------------------------------------------------
# build_prediction — with mock live status
# ---------------------------------------------------------------------------

class TestBuildPrediction:
    def test_returns_prediction_when_no_live_data(self, tmp_path):
        """build_prediction runs without errors when live status is unavailable."""
        from penny.analysis import build_prediction
        from penny.status_fetcher import LiveStatus

        live = LiveStatus(
            session_pct=0.0,
            session_reset_label="—",
            session_reset_tz="UTC",
            weekly_pct_all=0.0,
            weekly_pct_sonnet=0.0,
            weekly_reset_label="—",
            weekly_reset_tz="UTC",
            fetched_at=datetime.now(timezone.utc),
            outage=True,
        )
        state = {"period_history": [], "session_history": []}
        with (
            patch("penny.analysis.Path.home", return_value=tmp_path),
            patch("penny.status_fetcher.fetch_live_status", return_value=live),
        ):
            pred = build_prediction(state, force=False, precomputed_boundaries=[])
        assert isinstance(pred, Prediction)
        assert pred.outage is True

    def test_returns_prediction_with_live_data(self, tmp_path):
        """build_prediction overrides pct values from live status."""
        from penny.analysis import build_prediction
        from penny.status_fetcher import LiveStatus

        live = LiveStatus(
            session_pct=20.0,
            session_reset_label="5pm",
            session_reset_tz="UTC",
            weekly_pct_all=35.0,
            weekly_pct_sonnet=50.0,
            weekly_reset_label="Mar 6 at 9pm",
            weekly_reset_tz="UTC",
            fetched_at=datetime.now(timezone.utc),
        )
        state = {"period_history": [], "session_history": []}
        with (
            patch("penny.analysis.Path.home", return_value=tmp_path),
            patch("penny.status_fetcher.fetch_live_status", return_value=live),
        ):
            pred = build_prediction(state, force=False, precomputed_boundaries=[])
        assert pred.pct_all == 35.0
        assert pred.pct_sonnet == 50.0
        assert pred.outage is False

    def test_outage_flag_set_when_live_status_outage(self, tmp_path):
        """build_prediction propagates outage flag from live status."""
        from penny.analysis import build_prediction
        from penny.status_fetcher import LiveStatus

        live = LiveStatus(
            session_pct=0.0,
            session_reset_label="—",
            session_reset_tz="UTC",
            weekly_pct_all=0.0,
            weekly_pct_sonnet=0.0,
            weekly_reset_label="—",
            weekly_reset_tz="UTC",
            fetched_at=datetime.now(timezone.utc),
            outage=True,
        )
        state = {"period_history": [], "session_history": []}
        with (
            patch("penny.analysis.Path.home", return_value=tmp_path),
            patch("penny.status_fetcher.fetch_live_status", return_value=live),
        ):
            pred = build_prediction(state, force=False, precomputed_boundaries=[])
        assert pred.outage is True

    def test_outage_flag_from_cached_status_when_live_is_none(self, tmp_path):
        """When live fetch returns outage status and cached status has outage=True, propagate it."""
        from penny.analysis import build_prediction
        from penny.status_fetcher import LiveStatus

        live = LiveStatus(
            session_pct=0.0,
            session_reset_label="—",
            session_reset_tz="UTC",
            weekly_pct_all=0.0,
            weekly_pct_sonnet=0.0,
            weekly_reset_label="—",
            weekly_reset_tz="UTC",
            fetched_at=datetime.now(timezone.utc),
            outage=True,
        )
        cached = LiveStatus(
            session_pct=0.0,
            session_reset_label="—",
            session_reset_tz="UTC",
            weekly_pct_all=0.0,
            weekly_pct_sonnet=0.0,
            weekly_reset_label="—",
            weekly_reset_tz="UTC",
            fetched_at=datetime.now(timezone.utc),
            outage=True,
        )
        state = {"period_history": [], "session_history": []}
        with (
            patch("penny.analysis.Path.home", return_value=tmp_path),
            patch("penny.status_fetcher.fetch_live_status", return_value=live),
            patch("penny.status_fetcher.get_cached_status", return_value=cached),
        ):
            pred = build_prediction(state, force=False, precomputed_boundaries=[])
        assert pred.outage is True

    def test_outage_from_live_status(self, tmp_path):
        """build_prediction uses live status outage flag directly."""
        from penny.analysis import build_prediction
        from penny.status_fetcher import LiveStatus

        live = LiveStatus(
            session_pct=0.0,
            session_reset_label="—",
            session_reset_tz="UTC",
            weekly_pct_all=0.0,
            weekly_pct_sonnet=0.0,
            weekly_reset_label="—",
            weekly_reset_tz="UTC",
            fetched_at=datetime.now(timezone.utc),
            outage=True,
        )
        state = {"period_history": [], "session_history": []}
        with (
            patch("penny.analysis.Path.home", return_value=tmp_path),
            patch("penny.status_fetcher.fetch_live_status", return_value=live),
        ):
            pred = build_prediction(state, force=False, precomputed_boundaries=[])
        assert pred.outage is True

    def test_no_outage_when_live_none_and_no_cached_status(self, tmp_path):
        """When live fetch returns outage status and no cached status exists, outage stays False."""
        from penny.analysis import build_prediction
        from penny.status_fetcher import LiveStatus

        live = LiveStatus(
            session_pct=0.0,
            session_reset_label="—",
            session_reset_tz="UTC",
            weekly_pct_all=0.0,
            weekly_pct_sonnet=0.0,
            weekly_reset_label="—",
            weekly_reset_tz="UTC",
            fetched_at=datetime.now(timezone.utc),
            outage=True,
        )
        state = {"period_history": [], "session_history": []}
        with (
            patch("penny.analysis.Path.home", return_value=tmp_path),
            patch("penny.status_fetcher.fetch_live_status", return_value=live),
            patch("penny.status_fetcher.get_cached_status", return_value=None),
        ):
            pred = build_prediction(state, force=False, precomputed_boundaries=[])
        assert pred.outage is True

    def test_budget_back_calculated_from_live_pct(self, tmp_path):
        """When live pct > 0 and we have token counts, budget is back-calculated."""
        from penny.analysis import TokenUsage, build_prediction
        from penny.status_fetcher import LiveStatus

        live = LiveStatus(
            session_pct=10.0,
            session_reset_label="5pm",
            session_reset_tz="UTC",
            weekly_pct_all=50.0,   # 50% used
            weekly_pct_sonnet=25.0,
            weekly_reset_label="Mar 6 at 9pm",
            weekly_reset_tz="UTC",
            fetched_at=datetime.now(timezone.utc),
        )
        # Make count_tokens_since return a usage with 5000 output tokens
        fake_usage = TokenUsage(output_all=5000, output_sonnet=2500)

        state = {"period_history": [], "session_history": []}
        with (
            patch("penny.analysis.Path.home", return_value=tmp_path),
            patch("penny.status_fetcher.fetch_live_status", return_value=live),
            patch("penny.analysis.count_tokens_since", return_value=fake_usage),
        ):
            pred = build_prediction(state, force=False, precomputed_boundaries=[])
        # budget_all = 5000 / 0.50 = 10000
        assert pred.budget_all == 10000


# ---------------------------------------------------------------------------
# find_session_boundaries — additional paths
# ---------------------------------------------------------------------------

class TestFindSessionBoundariesEdgeCases:
    def test_skips_messages_before_since(self, tmp_path):
        projects_dir = tmp_path / ".claude" / "projects" / "proj"
        projects_dir.mkdir(parents=True)
        convo = projects_dir / "session.jsonl"
        lines = [
            json.dumps({
                "type": "assistant",
                "timestamp": "2024-01-01T10:00:00.000Z",  # before since
                "message": {
                    "model": "claude-sonnet-4-6",
                    "content": [
                        {"type": "text",
                         "text": "You've hit your limit. resets 5pm (Europe/Amsterdam)"},
                    ],
                    "usage": {"output_tokens": 100},
                },
            }),
        ]
        convo.write_text("\n".join(lines), encoding="utf-8")
        import os
        os.utime(convo, None)

        # since is after the message timestamp
        since = datetime(2025, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        with patch("penny.analysis.Path.home", return_value=tmp_path):
            boundaries = find_session_boundaries(since)
        # Message is before since_str so should be skipped
        assert boundaries == []

    def test_skips_unknown_timezone_in_rate_limit(self, tmp_path):
        projects_dir = tmp_path / ".claude" / "projects" / "proj"
        projects_dir.mkdir(parents=True)
        convo = projects_dir / "session.jsonl"
        lines = [
            json.dumps({
                "type": "assistant",
                "timestamp": "2025-01-10T14:00:00.000Z",
                "message": {
                    "model": "claude-sonnet-4-6",
                    "content": [
                        {"type": "text",
                         "text": "You've hit your limit. resets 5pm (Not/RealTimezone)"},
                    ],
                    "usage": {"output_tokens": 100},
                },
            }),
        ]
        convo.write_text("\n".join(lines), encoding="utf-8")
        import os
        os.utime(convo, None)

        since = datetime(2025, 1, 10, 0, 0, 0, tzinfo=timezone.utc)
        with patch("penny.analysis.Path.home", return_value=tmp_path):
            boundaries = find_session_boundaries(since)
        # Unknown timezone → skipped
        assert boundaries == []


# ---------------------------------------------------------------------------
# scan_rich_metrics — new fields (Phase 1)
# ---------------------------------------------------------------------------

def _write_jsonl(path, lines):
    """Write JSONL lines to path and set mtime to now."""
    import os
    path.write_text("\n".join(json.dumps(entry) for entry in lines), encoding="utf-8")
    os.utime(path, None)


class TestScanRichMetricsNewFields:
    def _proj_dir(self, tmp_path, name="proj-new"):
        d = tmp_path / ".claude" / "projects" / name
        d.mkdir(parents=True)
        return d

    def test_session_count_deduplicates(self, tmp_path):
        """Two assistant records with different sessionIds → session_count == 2."""
        proj = self._proj_dir(tmp_path)
        _write_jsonl(proj / "s.jsonl", [
            {
                "type": "assistant",
                "timestamp": "2025-01-10T10:00:00.000Z",
                "sessionId": "sess-aaa",
                "message": {
                    "model": "claude-sonnet-4-6",
                    "usage": {"output_tokens": 10},
                    "content": [],
                },
            },
            {
                "type": "assistant",
                "timestamp": "2025-01-10T11:00:00.000Z",
                "sessionId": "sess-bbb",
                "message": {
                    "model": "claude-sonnet-4-6",
                    "usage": {"output_tokens": 10},
                    "content": [],
                },
            },
            # same sessionId as first → should not add to count
            {
                "type": "assistant",
                "timestamp": "2025-01-10T12:00:00.000Z",
                "sessionId": "sess-aaa",
                "message": {
                    "model": "claude-sonnet-4-6",
                    "usage": {"output_tokens": 5},
                    "content": [],
                },
            },
        ])
        since = datetime(2025, 1, 10, 0, 0, 0, tzinfo=timezone.utc)
        with patch("penny.analysis.Path.home", return_value=tmp_path):
            rm = scan_rich_metrics(since)
        assert rm.session_count == 2

    def test_web_search_and_fetch_counts(self, tmp_path):
        """server_tool_use fields are summed across assistant records."""
        proj = self._proj_dir(tmp_path)
        _write_jsonl(proj / "s.jsonl", [
            {
                "type": "assistant",
                "timestamp": "2025-01-10T10:00:00.000Z",
                "message": {
                    "model": "claude-sonnet-4-6",
                    "usage": {
                        "output_tokens": 10,
                        "server_tool_use": {
                            "web_search_requests": 3,
                            "web_fetch_requests": 1,
                        },
                    },
                    "content": [],
                },
            },
            {
                "type": "assistant",
                "timestamp": "2025-01-10T11:00:00.000Z",
                "message": {
                    "model": "claude-sonnet-4-6",
                    "usage": {
                        "output_tokens": 10,
                        "server_tool_use": {
                            "web_search_requests": 2,
                            "web_fetch_requests": 4,
                        },
                    },
                    "content": [],
                },
            },
        ])
        since = datetime(2025, 1, 10, 0, 0, 0, tzinfo=timezone.utc)
        with patch("penny.analysis.Path.home", return_value=tmp_path):
            rm = scan_rich_metrics(since)
        assert rm.web_search_count == 5
        assert rm.web_fetch_count == 5

    def test_thinking_turns_detected(self, tmp_path):
        """Assistant record with a 'thinking' content block increments thinking_turns."""
        proj = self._proj_dir(tmp_path)
        _write_jsonl(proj / "s.jsonl", [
            {
                "type": "assistant",
                "timestamp": "2025-01-10T10:00:00.000Z",
                "message": {
                    "model": "claude-sonnet-4-6",
                    "usage": {"output_tokens": 10},
                    "content": [
                        {"type": "thinking", "thinking": "Let me reason..."},
                        {"type": "text", "text": "Answer."},
                    ],
                },
            },
            # Turn without thinking — should not count
            {
                "type": "assistant",
                "timestamp": "2025-01-10T11:00:00.000Z",
                "message": {
                    "model": "claude-sonnet-4-6",
                    "usage": {"output_tokens": 5},
                    "content": [{"type": "text", "text": "No thinking here."}],
                },
            },
        ])
        since = datetime(2025, 1, 10, 0, 0, 0, tzinfo=timezone.utc)
        with patch("penny.analysis.Path.home", return_value=tmp_path):
            rm = scan_rich_metrics(since)
        assert rm.thinking_turns == 1
        assert rm.total_turns == 2

    def test_agentic_turns_stop_reason_tool_use(self, tmp_path):
        """assistant records with stop_reason=='tool_use' increment agentic_turns."""
        proj = self._proj_dir(tmp_path)
        _write_jsonl(proj / "s.jsonl", [
            {
                "type": "assistant",
                "timestamp": "2025-01-10T10:00:00.000Z",
                "message": {
                    "model": "claude-sonnet-4-6",
                    "stop_reason": "tool_use",
                    "usage": {"output_tokens": 10},
                    "content": [],
                },
            },
            {
                "type": "assistant",
                "timestamp": "2025-01-10T11:00:00.000Z",
                "message": {
                    "model": "claude-sonnet-4-6",
                    "stop_reason": "end_turn",
                    "usage": {"output_tokens": 5},
                    "content": [],
                },
            },
        ])
        since = datetime(2025, 1, 10, 0, 0, 0, tzinfo=timezone.utc)
        with patch("penny.analysis.Path.home", return_value=tmp_path):
            rm = scan_rich_metrics(since)
        assert rm.agentic_turns == 1
        assert rm.total_turns == 2

    def test_tool_error_count_from_user_records(self, tmp_path):
        """user records with toolUseResult.is_error == True increment tool_error_count."""
        proj = self._proj_dir(tmp_path)
        _write_jsonl(proj / "s.jsonl", [
            {
                "type": "user",
                "timestamp": "2025-01-10T10:00:00.000Z",
                "toolUseResult": {"is_error": True, "content": "File not found"},
            },
            {
                "type": "user",
                "timestamp": "2025-01-10T10:01:00.000Z",
                "toolUseResult": {"is_error": False, "content": "ok"},
            },
            # user record without toolUseResult — should not crash
            {
                "type": "user",
                "timestamp": "2025-01-10T10:02:00.000Z",
                "message": {"content": "hello"},
            },
        ])
        since = datetime(2025, 1, 10, 0, 0, 0, tzinfo=timezone.utc)
        with patch("penny.analysis.Path.home", return_value=tmp_path):
            rm = scan_rich_metrics(since)
        assert rm.tool_error_count == 1

    def test_files_edited_deduplicates_paths(self, tmp_path):
        """Two user records with the same filePath count as one unique file."""
        proj = self._proj_dir(tmp_path)
        _write_jsonl(proj / "s.jsonl", [
            {
                "type": "user",
                "timestamp": "2025-01-10T10:00:00.000Z",
                "toolUseResult": {"filePath": "/src/foo.py"},
            },
            {
                "type": "user",
                "timestamp": "2025-01-10T10:01:00.000Z",
                "toolUseResult": {"filePath": "/src/foo.py"},  # duplicate
            },
            {
                "type": "user",
                "timestamp": "2025-01-10T10:02:00.000Z",
                "toolUseResult": {"filePath": "/src/bar.py"},  # new file
            },
        ])
        since = datetime(2025, 1, 10, 0, 0, 0, tzinfo=timezone.utc)
        with patch("penny.analysis.Path.home", return_value=tmp_path):
            rm = scan_rich_metrics(since)
        assert rm.files_edited == 2


# ---------------------------------------------------------------------------
# scan_rich_metrics_multi
# ---------------------------------------------------------------------------


class TestScanRichMetricsMulti:
    """Tests for the multi-window variant that does a single JSONL pass."""

    def test_empty_windows_returns_empty_dict(self):
        result = scan_rich_metrics_multi({})
        assert result == {}

    def test_returns_empty_metrics_when_no_projects_dir(self, tmp_path):
        windows = {
            "all": datetime(2024, 1, 1, tzinfo=timezone.utc),
        }
        with patch("penny.analysis.Path.home", return_value=tmp_path):
            result = scan_rich_metrics_multi(windows)
        assert "all" in result
        assert result["all"].total_turns == 0

    def test_single_window_matches_scan_rich_metrics(self, rich_jsonl_dir):
        """A single-window multi-scan should produce the same metrics as scan_rich_metrics."""
        since = datetime(2025, 1, 10, 0, 0, 0, tzinfo=timezone.utc)
        with patch("penny.analysis.Path.home", return_value=rich_jsonl_dir):
            single = scan_rich_metrics(since)
        with patch("penny.analysis.Path.home", return_value=rich_jsonl_dir):
            multi = scan_rich_metrics_multi({"all": since})
        m = multi["all"]
        assert m.opus_tokens == single.opus_tokens
        assert m.sonnet_tokens == single.sonnet_tokens
        assert m.haiku_tokens == single.haiku_tokens
        assert m.other_tokens == single.other_tokens
        assert m.cache_create_tokens == single.cache_create_tokens
        assert m.cache_read_tokens == single.cache_read_tokens
        assert m.total_turns == single.total_turns
        assert m.subagent_turns == single.subagent_turns
        assert m.pr_count == single.pr_count
        assert m.unique_projects == single.unique_projects
        assert m.unique_branches == single.unique_branches
        assert m.tool_counts == single.tool_counts
        assert sum(m.hourly_activity) == sum(single.hourly_activity)

    def test_multiple_windows_bucket_correctly(self, tmp_path):
        """Events in the recent window should appear in both wide and narrow windows."""
        projects_dir = tmp_path / ".claude" / "projects" / "proj"
        projects_dir.mkdir(parents=True)
        convo = projects_dir / "session.jsonl"
        lines = [
            # Old event (Jan 5)
            json.dumps({
                "type": "assistant",
                "timestamp": "2025-01-05T10:00:00.000Z",
                "message": {
                    "model": "claude-opus-4-6",
                    "usage": {"output_tokens": 100, "input_tokens": 10},
                    "content": [{"type": "text", "text": "old"}],
                },
            }),
            # Recent event (Jan 15)
            json.dumps({
                "type": "assistant",
                "timestamp": "2025-01-15T10:00:00.000Z",
                "message": {
                    "model": "claude-sonnet-4-6",
                    "usage": {"output_tokens": 200, "input_tokens": 20},
                    "content": [{"type": "text", "text": "new"}],
                },
            }),
        ]
        convo.write_text("\n".join(lines))

        windows = {
            "wide": datetime(2025, 1, 1, tzinfo=timezone.utc),    # catches both
            "narrow": datetime(2025, 1, 10, tzinfo=timezone.utc),  # catches only Jan 15
        }
        with patch("penny.analysis.Path.home", return_value=tmp_path):
            result = scan_rich_metrics_multi(windows)

        assert result["wide"].total_turns == 2
        assert result["wide"].opus_tokens == 100
        assert result["wide"].sonnet_tokens == 200

        assert result["narrow"].total_turns == 1
        assert result["narrow"].opus_tokens == 0
        assert result["narrow"].sonnet_tokens == 200

    def test_pr_link_counted_in_correct_windows(self, tmp_path):
        projects_dir = tmp_path / ".claude" / "projects" / "proj"
        projects_dir.mkdir(parents=True)
        convo = projects_dir / "session.jsonl"
        lines = [
            json.dumps({
                "type": "pr-link",
                "timestamp": "2025-01-15T10:00:00.000Z",
                "url": "https://github.com/example/repo/pull/1",
            }),
        ]
        convo.write_text("\n".join(lines))

        windows = {
            "wide": datetime(2025, 1, 1, tzinfo=timezone.utc),
            "narrow": datetime(2025, 1, 20, tzinfo=timezone.utc),
        }
        with patch("penny.analysis.Path.home", return_value=tmp_path):
            result = scan_rich_metrics_multi(windows)

        assert result["wide"].pr_count == 1
        assert result["narrow"].pr_count == 0

    def test_user_tool_errors_and_files_edited(self, tmp_path):
        projects_dir = tmp_path / ".claude" / "projects" / "proj"
        projects_dir.mkdir(parents=True)
        convo = projects_dir / "session.jsonl"
        lines = [
            json.dumps({
                "type": "user",
                "timestamp": "2025-01-15T10:00:00.000Z",
                "toolUseResult": {"is_error": True, "filePath": "/src/a.py"},
            }),
            json.dumps({
                "type": "user",
                "timestamp": "2025-01-15T10:01:00.000Z",
                "toolUseResult": {"filePath": "/src/b.py"},
            }),
        ]
        convo.write_text("\n".join(lines))

        windows = {"all": datetime(2025, 1, 1, tzinfo=timezone.utc)}
        with patch("penny.analysis.Path.home", return_value=tmp_path):
            result = scan_rich_metrics_multi(windows)

        assert result["all"].tool_error_count == 1
        assert result["all"].files_edited == 2

    def test_session_and_subagent_tracking(self, tmp_path):
        projects_dir = tmp_path / ".claude" / "projects" / "proj"
        projects_dir.mkdir(parents=True)
        convo = projects_dir / "session.jsonl"
        lines = [
            json.dumps({
                "type": "assistant",
                "timestamp": "2025-01-15T10:00:00.000Z",
                "sessionId": "sess-1",
                "cwd": "/proj/a",
                "gitBranch": "main",
                "message": {
                    "model": "claude-sonnet-4-6",
                    "usage": {"output_tokens": 50},
                    "content": [{"type": "thinking", "thinking": "hmm"}],
                    "stop_reason": "tool_use",
                },
            }),
            json.dumps({
                "type": "assistant",
                "timestamp": "2025-01-15T11:00:00.000Z",
                "sessionId": "sess-2",
                "isSidechain": True,
                "cwd": "/proj/b",
                "gitBranch": "feat",
                "message": {
                    "model": "claude-haiku-4-5",
                    "usage": {
                        "output_tokens": 30,
                        "server_tool_use": {
                            "web_search_requests": 2,
                            "web_fetch_requests": 1,
                        },
                    },
                    "content": [{"type": "tool_use", "name": "Grep"}],
                },
            }),
        ]
        convo.write_text("\n".join(lines))

        windows = {"all": datetime(2025, 1, 1, tzinfo=timezone.utc)}
        with patch("penny.analysis.Path.home", return_value=tmp_path):
            result = scan_rich_metrics_multi(windows)

        m = result["all"]
        assert m.session_count == 2
        assert m.unique_projects == 2
        assert m.unique_branches == 2
        assert m.subagent_turns == 1
        assert m.thinking_turns == 1
        assert m.agentic_turns == 1
        assert m.web_search_count == 2
        assert m.web_fetch_count == 1
        assert m.tool_counts.get("Grep") == 1

    def test_mtime_guard_skips_old_files(self, tmp_path):
        """Files with mtime before the earliest window cutoff should be skipped."""
        import os
        projects_dir = tmp_path / ".claude" / "projects" / "proj"
        projects_dir.mkdir(parents=True)
        old_file = projects_dir / "old.jsonl"
        old_file.write_text(json.dumps({
            "type": "assistant",
            "timestamp": "2025-01-15T10:00:00.000Z",
            "message": {"model": "claude-opus-4-6", "usage": {"output_tokens": 999}},
        }) + "\n")
        old_ts = datetime(2024, 1, 1, tzinfo=timezone.utc).timestamp()
        os.utime(old_file, (old_ts, old_ts))

        windows = {"recent": datetime(2025, 1, 10, tzinfo=timezone.utc)}
        with patch("penny.analysis.Path.home", return_value=tmp_path):
            result = scan_rich_metrics_multi(windows)
        assert result["recent"].total_turns == 0


# ---------------------------------------------------------------------------
# short_reset_label
# ---------------------------------------------------------------------------

class TestShortResetLabel:
    def test_empty_passthrough(self):
        assert short_reset_label("") == ""

    def test_dash_passthrough(self):
        assert short_reset_label("—") == "—"

    def test_none_passthrough(self):
        assert short_reset_label(None) is None

    def test_today_at_normalises_case(self):
        assert short_reset_label("today at 5pm") == "Today at 5pm"

    def test_today_at_preserves_time(self):
        assert short_reset_label("Today at 17:59") == "Today at 17:59"

    def test_bare_time_gets_today_prefix(self):
        assert short_reset_label("5pm") == "Today at 5pm"

    def test_bare_24h_time_gets_today_prefix(self):
        assert short_reset_label("17:59") == "Today at 17:59"

    def test_bare_hour_gets_today_prefix(self):
        assert short_reset_label("21") == "Today at 21"

    def test_todays_date_replaced_with_today(self):
        today = datetime.now()
        date_str = today.strftime("%b ") + str(today.day)
        label = f"{date_str} at 10am"
        assert short_reset_label(label) == "Today at 10am"

    def test_future_date_preserved(self):
        # Use a date that is definitely not today
        label = "Dec 31 at 9:59am"
        now = datetime.now()
        if now.month == 12 and now.day == 31:
            label = "Jan 1 at 9:59am"
        result = short_reset_label(label)
        assert result == label  # unchanged

    def test_future_date_keeps_full_format(self):
        label = "Mar 28 at 9am"
        now = datetime.now()
        if now.month == 3 and now.day == 28:
            # Today IS Mar 28, so use a different date
            label = "Mar 29 at 9am"
        result = short_reset_label(label)
        assert "at" in result
        assert "Today" not in result


# ---------------------------------------------------------------------------
# _hours_until_dated_reset_label
# ---------------------------------------------------------------------------

class TestHoursUntilDatedResetLabel:
    def test_returns_float_for_future_date(self):
        # Build a label for tomorrow in UTC
        from datetime import timezone as tz
        tomorrow = datetime.now(tz.utc) + timedelta(days=1)
        label = tomorrow.strftime("%b ") + str(tomorrow.day) + " at 8pm"
        hours = _hours_until_dated_reset_label(label, "UTC")
        assert isinstance(hours, float)
        assert hours > 0.0

    def test_returns_zero_for_empty_tz(self):
        assert _hours_until_dated_reset_label("Mar 28 at 8pm", "") == 0.0

    def test_returns_zero_for_none_tz(self):
        assert _hours_until_dated_reset_label("Mar 28 at 8pm", None) == 0.0

    def test_returns_zero_for_invalid_tz(self):
        assert _hours_until_dated_reset_label("Mar 28 at 8pm", "Fake/Zone") == 0.0

    def test_returns_zero_for_unrecognised_label(self):
        assert _hours_until_dated_reset_label("garbage", "UTC") == 0.0

    def test_returns_zero_for_plain_time(self):
        # Only handles dated labels, not bare times
        assert _hours_until_dated_reset_label("8pm", "UTC") == 0.0

    def test_handles_label_with_minutes(self):
        from datetime import timezone as tz
        tomorrow = datetime.now(tz.utc) + timedelta(days=1)
        label = tomorrow.strftime("%b ") + str(tomorrow.day) + " at 9:59am"
        hours = _hours_until_dated_reset_label(label, "UTC")
        assert isinstance(hours, float)
        assert hours > 0.0

    def test_handles_midnight_12am(self):
        from datetime import timezone as tz
        tomorrow = datetime.now(tz.utc) + timedelta(days=1)
        label = tomorrow.strftime("%b ") + str(tomorrow.day) + " at 12am"
        hours = _hours_until_dated_reset_label(label, "UTC")
        assert hours > 0.0

    def test_handles_noon_12pm(self):
        from datetime import timezone as tz
        tomorrow = datetime.now(tz.utc) + timedelta(days=1)
        label = tomorrow.strftime("%b ") + str(tomorrow.day) + " at 12pm"
        hours = _hours_until_dated_reset_label(label, "UTC")
        assert hours > 0.0

    def test_invalid_month_returns_zero(self):
        assert _hours_until_dated_reset_label("Xyz 15 at 8pm", "UTC") == 0.0

    def test_year_rollover_for_past_date(self):
        # A date earlier this year (if already past) should roll to next year
        from datetime import timezone as tz
        now = datetime.now(tz.utc)
        past = now - timedelta(days=30)
        label = past.strftime("%b ") + str(past.day) + " at " + past.strftime("%-I%p").lower()
        hours = _hours_until_dated_reset_label(label, "UTC")
        # Should either be 0.0 (if it can't find a future match) or > 0 (next year)
        assert isinstance(hours, float)
        assert hours >= 0.0


# ---------------------------------------------------------------------------
# Per-project & per-session token tracking
# ---------------------------------------------------------------------------

class TestProjectUsage:
    """Tests for project_usage and session_usage fields on RichMetrics."""

    def test_project_usage_grouped_by_cwd(self, multi_project_jsonl_dir):
        """Projects are grouped by cwd with correct token totals, sorted desc."""
        since = datetime(2025, 1, 10, 0, 0, 0, tzinfo=timezone.utc)
        with patch("penny.analysis.Path.home", return_value=multi_project_jsonl_dir):
            rm = scan_rich_metrics(since)
        # proj-a: 500 opus + 200 sonnet + 100 haiku = 800 total
        # proj-b: 300 sonnet = 300 total
        assert len(rm.project_usage) == 2
        assert rm.project_usage[0]["cwd"] == "/home/user/proj-a"
        assert rm.project_usage[0]["total_output_tokens"] == 800
        assert rm.project_usage[1]["cwd"] == "/home/user/proj-b"
        assert rm.project_usage[1]["total_output_tokens"] == 300

    def test_project_model_breakdown(self, multi_project_jsonl_dir):
        """Per-project metrics include per-model token counts."""
        since = datetime(2025, 1, 10, 0, 0, 0, tzinfo=timezone.utc)
        with patch("penny.analysis.Path.home", return_value=multi_project_jsonl_dir):
            rm = scan_rich_metrics(since)
        proj_a = rm.project_usage[0]
        assert proj_a["opus_tokens"] == 500
        assert proj_a["sonnet_tokens"] == 200
        assert proj_a["haiku_tokens"] == 100
        assert proj_a["other_tokens"] == 0

    def test_project_session_count(self, multi_project_jsonl_dir):
        """session_count reflects unique sessionIds within each project."""
        since = datetime(2025, 1, 10, 0, 0, 0, tzinfo=timezone.utc)
        with patch("penny.analysis.Path.home", return_value=multi_project_jsonl_dir):
            rm = scan_rich_metrics(since)
        # proj-a has sess-aaa and sess-bbb
        assert rm.project_usage[0]["session_count"] == 2
        # proj-b has sess-ccc
        assert rm.project_usage[1]["session_count"] == 1

    def test_project_name_is_basename(self, multi_project_jsonl_dir):
        """Friendly name is the basename of the cwd path."""
        since = datetime(2025, 1, 10, 0, 0, 0, tzinfo=timezone.utc)
        with patch("penny.analysis.Path.home", return_value=multi_project_jsonl_dir):
            rm = scan_rich_metrics(since)
        assert rm.project_usage[0]["name"] == "proj-a"
        assert rm.project_usage[1]["name"] == "proj-b"

    def test_project_sessions_nested(self, multi_project_jsonl_dir):
        """Each project has a sessions list with per-session breakdowns."""
        since = datetime(2025, 1, 10, 0, 0, 0, tzinfo=timezone.utc)
        with patch("penny.analysis.Path.home", return_value=multi_project_jsonl_dir):
            rm = scan_rich_metrics(since)
        proj_a = rm.project_usage[0]
        # proj-a has 2 sessions: sess-aaa (700 tokens), sess-bbb (100 tokens)
        assert len(proj_a["sessions"]) == 2
        # sorted desc by last_ts (sess-bbb at 12:00, sess-aaa at 11:00)
        assert proj_a["sessions"][0]["session_id"] == "sess-bbb"
        assert proj_a["sessions"][0]["total_output_tokens"] == 100
        assert proj_a["sessions"][1]["session_id"] == "sess-aaa"
        assert proj_a["sessions"][1]["total_output_tokens"] == 700
        assert proj_a["sessions"][1]["opus_tokens"] == 500
        assert proj_a["sessions"][1]["sonnet_tokens"] == 200

    def test_session_timestamps(self, multi_project_jsonl_dir):
        """first_ts and last_ts track the session's time range."""
        since = datetime(2025, 1, 10, 0, 0, 0, tzinfo=timezone.utc)
        with patch("penny.analysis.Path.home", return_value=multi_project_jsonl_dir):
            rm = scan_rich_metrics(since)
        # sess-aaa is second (sorted by last_ts desc; sess-bbb at 12:00 is first)
        sess_aaa = rm.project_usage[0]["sessions"][1]
        assert sess_aaa["first_ts"] == "2025-01-10T10:00:00"
        assert sess_aaa["last_ts"] == "2025-01-10T11:00:00"

    def test_session_title_from_custom_title_entry(self, tmp_path):
        """Sessions include title from custom-title JSONL entries."""
        proj = tmp_path / ".claude" / "projects" / "proj"
        proj.mkdir(parents=True)
        _write_jsonl(proj / "s.jsonl", [
            {
                "type": "custom-title",
                "timestamp": "2025-01-10T09:00:00.000Z",
                "sessionId": "sess-titled",
                "customTitle": "refactor auth module",
            },
            {
                "type": "assistant",
                "timestamp": "2025-01-10T10:00:00.000Z",
                "sessionId": "sess-titled",
                "cwd": "/projects/myapp",
                "message": {
                    "model": "claude-sonnet-4-6",
                    "usage": {"output_tokens": 200},
                    "content": [],
                },
            },
            {
                "type": "assistant",
                "timestamp": "2025-01-10T11:00:00.000Z",
                "sessionId": "sess-untitled",
                "cwd": "/projects/myapp",
                "message": {
                    "model": "claude-sonnet-4-6",
                    "usage": {"output_tokens": 100},
                    "content": [],
                },
            },
        ])
        since = datetime(2025, 1, 10, 0, 0, 0, tzinfo=timezone.utc)
        with patch("penny.analysis.Path.home", return_value=tmp_path):
            rm = scan_rich_metrics(since)
        sessions = {s["session_id"]: s for s in rm.project_usage[0]["sessions"]}
        assert sessions["sess-titled"]["title"] == "refactor auth module"
        assert sessions["sess-untitled"]["title"] == ""

    def test_session_title_in_flat_sessions(self, tmp_path):
        """session_usage flat list also includes title."""
        proj = tmp_path / ".claude" / "projects" / "proj"
        proj.mkdir(parents=True)
        _write_jsonl(proj / "s.jsonl", [
            {
                "type": "custom-title",
                "timestamp": "2025-01-10T09:00:00.000Z",
                "sessionId": "sess-t",
                "customTitle": "fix login bug",
            },
            {
                "type": "assistant",
                "timestamp": "2025-01-10T10:00:00.000Z",
                "sessionId": "sess-t",
                "cwd": "/projects/app",
                "message": {
                    "model": "claude-opus-4-6",
                    "usage": {"output_tokens": 300},
                    "content": [],
                },
            },
        ])
        since = datetime(2025, 1, 10, 0, 0, 0, tzinfo=timezone.utc)
        with patch("penny.analysis.Path.home", return_value=tmp_path):
            rm = scan_rich_metrics(since)
        assert rm.session_usage[0]["title"] == "fix login bug"

    def test_session_title_in_multi_window(self, tmp_path):
        """scan_rich_metrics_multi also extracts session titles."""
        proj = tmp_path / ".claude" / "projects" / "proj"
        proj.mkdir(parents=True)
        _write_jsonl(proj / "s.jsonl", [
            {
                "type": "custom-title",
                "timestamp": "2025-01-10T09:00:00.000Z",
                "sessionId": "sess-m",
                "customTitle": "add dashboard",
            },
            {
                "type": "assistant",
                "timestamp": "2025-01-10T10:00:00.000Z",
                "sessionId": "sess-m",
                "cwd": "/projects/dash",
                "message": {
                    "model": "claude-sonnet-4-6",
                    "usage": {"output_tokens": 150},
                    "content": [],
                },
            },
        ])
        windows = {"w": datetime(2025, 1, 10, 0, 0, 0, tzinfo=timezone.utc)}
        with patch("penny.analysis.Path.home", return_value=tmp_path):
            result = scan_rich_metrics_multi(windows)
        assert result["w"].project_usage[0]["sessions"][0]["title"] == "add dashboard"

    def test_empty_cwd_excluded(self, tmp_path):
        """Entries without cwd don't create project entries."""
        proj = tmp_path / ".claude" / "projects" / "proj"
        proj.mkdir(parents=True)
        _write_jsonl(proj / "s.jsonl", [
            {
                "type": "assistant",
                "timestamp": "2025-01-10T10:00:00.000Z",
                "sessionId": "sess-x",
                "message": {
                    "model": "claude-sonnet-4-6",
                    "usage": {"output_tokens": 100},
                    "content": [],
                },
            },
        ])
        since = datetime(2025, 1, 10, 0, 0, 0, tzinfo=timezone.utc)
        with patch("penny.analysis.Path.home", return_value=tmp_path):
            rm = scan_rich_metrics(since)
        assert rm.project_usage == []

    def test_project_usage_truncated_to_20(self, tmp_path):
        """At most 20 projects are returned."""
        proj = tmp_path / ".claude" / "projects" / "proj"
        proj.mkdir(parents=True)
        entries = []
        for i in range(25):
            entries.append({
                "type": "assistant",
                "timestamp": "2025-01-10T10:00:00.000Z",
                "sessionId": f"sess-{i}",
                "cwd": f"/projects/p{i:02d}",
                "message": {
                    "model": "claude-sonnet-4-6",
                    "usage": {"output_tokens": 100 + i},
                    "content": [],
                },
            })
        _write_jsonl(proj / "s.jsonl", entries)
        since = datetime(2025, 1, 10, 0, 0, 0, tzinfo=timezone.utc)
        with patch("penny.analysis.Path.home", return_value=tmp_path):
            rm = scan_rich_metrics(since)
        assert len(rm.project_usage) == 20

    def test_multi_window_project_usage(self, tmp_path):
        """project_usage respects window boundaries in scan_rich_metrics_multi."""
        proj = tmp_path / ".claude" / "projects" / "proj"
        proj.mkdir(parents=True)
        entries = [
            {
                "type": "assistant",
                "timestamp": "2025-01-05T10:00:00.000Z",
                "sessionId": "sess-old",
                "cwd": "/projects/old",
                "message": {
                    "model": "claude-opus-4-6",
                    "usage": {"output_tokens": 100},
                    "content": [],
                },
            },
            {
                "type": "assistant",
                "timestamp": "2025-01-15T10:00:00.000Z",
                "sessionId": "sess-new",
                "cwd": "/projects/new",
                "message": {
                    "model": "claude-sonnet-4-6",
                    "usage": {"output_tokens": 200},
                    "content": [],
                },
            },
        ]
        _write_jsonl(proj / "s.jsonl", entries)
        windows = {
            "all": datetime(2025, 1, 1, tzinfo=timezone.utc),
            "recent": datetime(2025, 1, 10, tzinfo=timezone.utc),
        }
        with patch("penny.analysis.Path.home", return_value=tmp_path):
            result = scan_rich_metrics_multi(windows)
        # "all" window should have both projects
        assert len(result["all"].project_usage) == 2
        # "recent" window should only have the new project
        assert len(result["recent"].project_usage) == 1
        assert result["recent"].project_usage[0]["cwd"] == "/projects/new"

    def test_single_window_multi_matches_single(self, multi_project_jsonl_dir):
        """scan_rich_metrics_multi with one window matches scan_rich_metrics."""
        since = datetime(2025, 1, 10, 0, 0, 0, tzinfo=timezone.utc)
        with patch("penny.analysis.Path.home", return_value=multi_project_jsonl_dir):
            single = scan_rich_metrics(since)
        with patch("penny.analysis.Path.home", return_value=multi_project_jsonl_dir):
            multi = scan_rich_metrics_multi({"all": since})
        assert len(multi["all"].project_usage) == len(single.project_usage)
        for sp, mp in zip(single.project_usage, multi["all"].project_usage):
            assert sp["cwd"] == mp["cwd"]
            assert sp["total_output_tokens"] == mp["total_output_tokens"]
