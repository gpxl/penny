"""Unit tests for naenae/preflight.py."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from naenae.preflight import (
    PreflightIssue,
    format_issues_for_cli,
    has_errors,
    run_preflight,
)


def _minimal_config(**overrides):
    cfg = {
        "projects": [],
        "stats_cache_path": "/nonexistent/stats.json",
    }
    cfg.update(overrides)
    return cfg


class TestRunPreflight:
    def test_error_when_claude_not_in_path(self):
        with patch("naenae.preflight.shutil.which", return_value=None):
            issues = run_preflight(_minimal_config())
        errors = [i for i in issues if i.severity == "error"]
        assert any("`claude`" in i.message for i in errors)

    def test_warning_when_claude_fails_to_execute(self, tmp_path):
        with (
            patch("naenae.preflight.shutil.which", return_value="/usr/bin/claude"),
            patch(
                "naenae.preflight.subprocess.run",
                side_effect=FileNotFoundError,
            ),
        ):
            issues = run_preflight(_minimal_config())
        warnings = [i for i in issues if i.severity == "warning"]
        assert any("claude" in i.message.lower() for i in warnings)

    def test_error_when_no_projects_configured(self):
        with patch("naenae.preflight.shutil.which", return_value="/usr/bin/claude"):
            with patch(
                "naenae.preflight.subprocess.run",
                return_value=MagicMock(returncode=0),
            ):
                issues = run_preflight(_minimal_config(projects=[]))
        errors = [i for i in issues if i.severity == "error"]
        assert any("No projects" in i.message for i in errors)

    def test_warning_when_project_path_missing(self, tmp_path):
        config = _minimal_config(
            projects=[{"path": str(tmp_path / "nonexistent")}]
        )
        with (
            patch("naenae.preflight.shutil.which", return_value="/usr/bin/claude"),
            patch(
                "naenae.preflight.subprocess.run",
                return_value=MagicMock(returncode=0),
            ),
        ):
            issues = run_preflight(config)
        warnings = [i for i in issues if i.severity == "warning"]
        assert any("does not exist" in i.message for i in warnings)


class TestHasErrors:
    def test_true_when_error_present(self):
        issues = [PreflightIssue(severity="error", message="bad", fix_hint="fix")]
        assert has_errors(issues) is True

    def test_false_when_only_warnings(self):
        issues = [PreflightIssue(severity="warning", message="meh", fix_hint="ok")]
        assert has_errors(issues) is False

    def test_false_when_empty(self):
        assert has_errors([]) is False


class TestFormatIssuesForCli:
    def test_all_passed_message_when_empty(self):
        assert format_issues_for_cli([]) == "✅ All checks passed."

    def test_error_icon_for_errors(self):
        issues = [PreflightIssue(severity="error", message="bad thing", fix_hint="do this")]
        result = format_issues_for_cli(issues)
        assert "❌" in result
        assert "bad thing" in result

    def test_warning_icon_for_warnings(self):
        issues = [PreflightIssue(severity="warning", message="hmm", fix_hint="try this")]
        result = format_issues_for_cli(issues)
        assert "⚠️" in result
        assert "hmm" in result
