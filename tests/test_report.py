"""Tests for penny/report.py — HTML report generation and opening."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from penny.report import _history_svg, generate_report, open_report
from penny.tasks import Task

# ── _history_svg ─────────────────────────────────────────────────────────────


class TestHistorySvg:
    def test_empty_history_returns_message(self):
        result = _history_svg([])
        assert "No historical data" in result

    def test_single_period(self):
        history = [{"output_all": 50000, "period_start": "2025-03-01T00:00:00"}]
        result = _history_svg(history)
        assert "<svg" in result
        assert "<rect" in result

    def test_multiple_periods_create_bars(self):
        history = [
            {"output_all": 30000, "period_start": "2025-02-01T00:00:00"},
            {"output_all": 60000, "period_start": "2025-02-08T00:00:00"},
            {"output_all": 90000, "period_start": "2025-02-15T00:00:00"},
        ]
        result = _history_svg(history)
        assert result.count("<rect") == 3

    def test_zero_output_handled(self):
        history = [{"output_all": 0, "period_start": "2025-03-01T00:00:00"}]
        result = _history_svg(history)
        assert "<svg" in result

    def test_color_coding_by_usage(self):
        history = [
            {"output_all": 10, "period_start": "2025-01-01T00:00:00"},
            {"output_all": 100, "period_start": "2025-01-08T00:00:00"},
        ]
        result = _history_svg(history)
        # Low usage bar (10/100 = 10%) → green
        assert "#10b981" in result
        # High usage bar (100/100 = 100%) → red
        assert "#ef4444" in result


# ── generate_report ──────────────────────────────────────────────────────────


class TestGenerateReport:
    @pytest.fixture
    def report_dir(self, tmp_path):
        d = tmp_path / "reports"
        with patch("penny.report.REPORT_DIR", d):
            yield d

    def _empty_state(self):
        return {
            "predictions": {},
            "agents_running": [],
            "recently_completed": [],
            "period_history": [],
            "last_check": "never",
        }

    def _mock_mgr(self, tasks=None):
        mgr = MagicMock()
        mgr.get_all_tasks.return_value = tasks or []
        mgr.get_task_description.return_value = "description"
        return mgr

    def test_creates_html_file(self, report_dir):
        with patch("penny.report.REPORT_DIR", report_dir):
            path = generate_report(self._empty_state(), {}, self._mock_mgr())
        assert path.exists()
        assert path.suffix == ".html"

    def test_creates_latest_symlink(self, report_dir):
        with patch("penny.report.REPORT_DIR", report_dir):
            generate_report(self._empty_state(), {}, self._mock_mgr())
        assert (report_dir / "latest.html").is_symlink()

    def test_overwrites_existing_symlink(self, report_dir):
        report_dir.mkdir(parents=True)
        (report_dir / "latest.html").symlink_to("old-report.html")
        with patch("penny.report.REPORT_DIR", report_dir):
            generate_report(self._empty_state(), {}, self._mock_mgr())
        assert (report_dir / "latest.html").is_symlink()

    def test_html_contains_title(self, report_dir):
        with patch("penny.report.REPORT_DIR", report_dir):
            path = generate_report(self._empty_state(), {}, self._mock_mgr())
        html = path.read_text()
        assert "Penny" in html
        assert "Status Report" in html

    def test_includes_usage_percentages(self, report_dir):
        state = self._empty_state()
        state["predictions"] = {
            "pct_all": 42.5,
            "pct_sonnet": 30.0,
            "days_remaining": 3.5,
            "output_all": 50000,
            "output_sonnet": 20000,
            "projected_pct_all": 60.0,
            "session_pct_all": 10.0,
            "session_pct_sonnet": 5.0,
            "session_reset_label": "2pm",
            "session_hours_remaining": 4.0,
            "sessions_remaining_week": 8,
            "reset_label": "Mar 6 at 9pm",
        }
        with patch("penny.report.REPORT_DIR", report_dir):
            path = generate_report(state, {}, self._mock_mgr())
        html = path.read_text()
        assert "42.5%" in html
        assert "30.0%" in html

    def test_includes_running_agents(self, report_dir):
        state = self._empty_state()
        state["agents_running"] = [
            {"task_id": "t-1", "project": "proj", "title": "Fix bug", "log": "/tmp/log"},
        ]
        with patch("penny.report.REPORT_DIR", report_dir):
            path = generate_report(state, {}, self._mock_mgr())
        html = path.read_text()
        assert "t-1" in html
        assert "Fix bug" in html

    def test_includes_completed_agents(self, report_dir):
        state = self._empty_state()
        state["recently_completed"] = [
            {
                "task_id": "d-1",
                "project": "proj",
                "title": "Done task",
                "status": "completed",
                "spawned_at": "2025-03-07T10:00:00",
                "log": "/tmp/log",
            },
        ]
        with patch("penny.report.REPORT_DIR", report_dir):
            path = generate_report(state, {}, self._mock_mgr())
        html = path.read_text()
        assert "d-1" in html
        assert "Done task" in html

    def test_includes_ready_tasks(self, report_dir):
        tasks = [Task("t-1", "Fix bug", "P1", "/tmp/proj", "proj")]
        mgr = self._mock_mgr(tasks=tasks)
        with patch("penny.report.REPORT_DIR", report_dir):
            path = generate_report(self._empty_state(), {"projects": [{"path": "/tmp"}]}, mgr)
        html = path.read_text()
        assert "t-1" in html
        assert "Fix bug" in html

    def test_handles_empty_state(self, report_dir):
        with patch("penny.report.REPORT_DIR", report_dir):
            path = generate_report(self._empty_state(), {}, self._mock_mgr())
        html = path.read_text()
        assert "No ready tasks" in html
        assert "No agents currently running" in html

    def test_includes_history_svg(self, report_dir):
        state = self._empty_state()
        state["period_history"] = [
            {"output_all": 30000, "period_start": "2025-02-01T00:00:00"},
        ]
        with patch("penny.report.REPORT_DIR", report_dir):
            path = generate_report(state, {}, self._mock_mgr())
        html = path.read_text()
        assert "<svg" in html

    def test_escapes_html_in_task_descriptions(self, report_dir):
        tasks = [Task("t-1", "Fix <script>alert('xss')</script>", "P1", "/tmp/p", "proj")]
        mgr = self._mock_mgr(tasks=tasks)
        mgr.get_task_description.return_value = "<b>bold</b> desc"
        with patch("penny.report.REPORT_DIR", report_dir):
            path = generate_report(self._empty_state(), {"projects": []}, mgr)
        html = path.read_text()
        assert "<script>" not in html
        assert "&lt;script&gt;" in html


# ── open_report ──────────────────────────────────────────────────────────────


class TestOpenReport:
    def test_opens_given_path(self):
        with patch("penny.report.subprocess.run") as mock_run:
            open_report(Path("/tmp/report.html"))
        mock_run.assert_called_once_with(["open", "/tmp/report.html"], check=False)

    def test_defaults_to_latest(self):
        with patch("penny.report.subprocess.run") as mock_run, \
             patch("penny.report.REPORT_DIR", Path("/tmp/penny/reports")):
            open_report()
        mock_run.assert_called_once_with(
            ["open", "/tmp/penny/reports/latest.html"], check=False,
        )
