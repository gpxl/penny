"""Smoke tests for the penny CLI (scripts/penny).

These test the shell script's output and exit codes without requiring
a running Penny instance (except where noted).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

# Resolve the CLI script path relative to this file
CLI = str(Path(__file__).parent.parent / "scripts" / "penny")


def _run(args: list[str], **kwargs) -> subprocess.CompletedProcess:
    """Run the penny CLI and return the CompletedProcess."""
    return subprocess.run(
        [CLI] + args,
        capture_output=True,
        text=True,
        timeout=10,
        **kwargs,
    )


class TestCLIHelp:
    def test_help_flag(self):
        result = _run(["help"])
        assert result.returncode == 0
        assert "Usage:" in result.stdout
        assert "penny <command>" in result.stdout

    def test_dash_h(self):
        result = _run(["-h"])
        assert result.returncode == 0
        assert "Usage:" in result.stdout

    def test_dash_question(self):
        result = _run(["-?"])
        assert result.returncode == 0
        assert "Usage:" in result.stdout

    def test_help_lists_all_commands(self):
        result = _run(["help"])
        for cmd in ["start", "stop", "status", "quit", "refresh", "tasks",
                     "agents", "run", "stop-agent", "dismiss", "clear-completed",
                     "open", "logs", "prefs", "version", "help"]:
            assert cmd in result.stdout, f"Missing command: {cmd}"


class TestCLIVersion:
    def test_version_flag(self):
        result = _run(["version"])
        assert result.returncode == 0
        assert "Penny" in result.stdout

    def test_dash_v(self):
        result = _run(["-v"])
        assert result.returncode == 0
        assert "Penny" in result.stdout


class TestCLIUnknownCommand:
    def test_unknown_command_exits_nonzero(self):
        result = _run(["nonexistent-command"])
        assert result.returncode != 0
        assert "Unknown command" in result.stdout or "Unknown command" in result.stderr

    def test_no_args_exits_nonzero(self):
        result = _run([])
        assert result.returncode != 0


class TestCLIStatus:
    def test_status_output(self):
        result = _run(["status"])
        assert result.returncode == 0
        # Should say either running or not running
        assert "Penny is" in result.stdout
        assert "running" in result.stdout.lower()


class TestCLIRequiresRunning:
    """Commands that require a running Penny instance should fail gracefully when stopped."""

    @pytest.fixture(autouse=True)
    def _skip_if_running(self):
        """Skip these tests if Penny is actually running."""
        result = _run(["status"])
        if "is running" in result.stdout and "not running" not in result.stdout:
            pytest.skip("Penny is running — these tests need it stopped")

    def test_tasks_requires_running(self):
        result = _run(["tasks"])
        assert result.returncode != 0
        assert "not running" in result.stdout.lower()

    def test_agents_requires_running(self):
        result = _run(["agents"])
        assert result.returncode != 0
        assert "not running" in result.stdout.lower()

    def test_refresh_requires_running(self):
        result = _run(["refresh"])
        assert result.returncode != 0
        assert "not running" in result.stdout.lower()

    def test_run_requires_running(self):
        result = _run(["run", "some-task"])
        assert result.returncode != 0
        assert "not running" in result.stdout.lower()

    def test_stop_agent_requires_running(self):
        result = _run(["stop-agent", "some-task"])
        assert result.returncode != 0
        assert "not running" in result.stdout.lower()

    def test_dismiss_requires_running(self):
        result = _run(["dismiss", "some-task"])
        assert result.returncode != 0
        assert "not running" in result.stdout.lower()

    def test_clear_completed_requires_running(self):
        result = _run(["clear-completed"])
        assert result.returncode != 0
        assert "not running" in result.stdout.lower()

    def test_quit_requires_running(self):
        result = _run(["quit"])
        assert result.returncode != 0
        assert "not running" in result.stdout.lower()


class TestCLIArgumentValidation:
    """Commands that require arguments should fail with usage info."""

    @pytest.fixture(autouse=True)
    def _skip_if_not_running(self):
        """These tests need Penny running to reach the argument check."""
        result = _run(["status"])
        if "not running" in result.stdout:
            pytest.skip("Penny is not running — argument tests need it running")

    def test_run_without_task_id(self):
        result = _run(["run"])
        assert result.returncode != 0
        assert "Usage:" in result.stdout or "task-id" in result.stdout

    def test_stop_agent_without_task_id(self):
        result = _run(["stop-agent"])
        assert result.returncode != 0
        assert "Usage:" in result.stdout or "task-id" in result.stdout

    def test_dismiss_without_task_id(self):
        result = _run(["dismiss"])
        assert result.returncode != 0
        assert "Usage:" in result.stdout or "task-id" in result.stdout
