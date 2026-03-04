"""Unit tests for naenae/analysis.py."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from naenae.analysis import (
    count_tokens_since,
    current_billing_period,
    days_until_reset,
    estimate_budget_from_history,
    format_reset_label,
    get_usage_bar,
    should_trigger,
)
from naenae.analysis import Prediction


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
        with patch("naenae.analysis.Path.home", return_value=sample_jsonl_dir):
            usage = count_tokens_since(since)
        # 100 sonnet + 200 opus = 300 total output; 100 sonnet only
        assert usage.output_all == 300
        assert usage.output_sonnet == 100

    def test_returns_zeros_when_projects_dir_missing(self, tmp_path):
        with patch("naenae.analysis.Path.home", return_value=tmp_path):
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

        with patch("naenae.analysis.Path.home", return_value=tmp_path):
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

        with patch("naenae.analysis.Path.home", return_value=tmp_path):
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
        import os, time
        old_mtime = 0  # epoch
        os.utime(convo, (old_mtime, old_mtime))

        since = datetime(2025, 6, 1, 0, 0, 0, tzinfo=timezone.utc)
        with patch("naenae.analysis.Path.home", return_value=tmp_path):
            usage = count_tokens_since(since)
        assert usage.output_all == 0


# ---------------------------------------------------------------------------
# estimate_budget_from_history
# ---------------------------------------------------------------------------

class TestEstimateBudgetFromHistory:
    def test_returns_none_when_empty_history(self):
        result = estimate_budget_from_history({})
        assert result["all"] is None
        assert result["sonnet"] is None

    def test_returns_none_with_single_entry(self):
        state = {"period_history": [{"output_all": 1000, "output_sonnet": 500}]}
        result = estimate_budget_from_history(state)
        assert result["all"] is None

    def test_returns_max_when_fewer_than_4_samples(self):
        state = {
            "period_history": [
                {"output_all": 1000, "output_sonnet": 500},
                {"output_all": 2000, "output_sonnet": 800},
                {"output_all": 1500, "output_sonnet": 600},
            ]
        }
        result = estimate_budget_from_history(state)
        assert result["all"] == 2000

    def test_returns_90th_percentile_with_4_plus_samples(self):
        state = {
            "period_history": [
                {"output_all": v, "output_sonnet": v // 2}
                for v in [1000, 2000, 3000, 4000, 5000, 6000, 7000, 8000, 9000, 10000]
            ]
        }
        result = estimate_budget_from_history(state)
        # 90th percentile of 10 values sorted = index 9 = 10000
        assert result["all"] == 10000


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
        with patch("naenae.analysis.uses_24h_time", return_value=False):
            assert format_reset_label("9pm") == "9pm"

    def test_compact_12h_to_24h(self):
        with patch("naenae.analysis.uses_24h_time", return_value=True):
            assert format_reset_label("9pm") == "21"

    def test_compact_with_minutes(self):
        with patch("naenae.analysis.uses_24h_time", return_value=True):
            assert format_reset_label("5:59pm") == "17:59"

    def test_date_prefix_preserved(self):
        with patch("naenae.analysis.uses_24h_time", return_value=True):
            result = format_reset_label("Mar 6 at 9pm")
            assert result == "Mar 6 at 21"

    def test_today_prefix_preserved(self):
        with patch("naenae.analysis.uses_24h_time", return_value=True):
            result = format_reset_label("Today at 5:59 PM")
            assert result == "Today at 17:59"
