"""Integration test for spawn_claude_agent with dry_run=True.

Unlike the unit tests in test_spawner.py (which mock data_dir), this test
calls spawn_claude_agent with a real temporary data directory to exercise
the full code path: session naming, log path generation, and state recording.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from penny.spawner import spawn_claude_agent
from penny.tasks import Task


def _make_task(**overrides) -> Task:
    defaults = {
        "task_id": "integ-abc",
        "title": "Integration test task",
        "priority": "P1",
        "project_path": "/tmp/test-proj",
        "project_name": "test-proj",
    }
    defaults.update(overrides)
    return Task(**defaults)


@pytest.fixture
def penny_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Set PENNY_HOME to a temporary directory for the duration of the test."""
    monkeypatch.setenv("PENNY_HOME", str(tmp_path))
    return tmp_path


class TestDryRunIntegration:
    """Integration tests that exercise spawn_claude_agent(dry_run=True) end-to-end."""

    def test_returns_record_with_dry_run_status(self, penny_home):
        task = _make_task()
        record = spawn_claude_agent(task, "Do the thing", dry_run=True)
        assert record["status"] == "dry_run"

    def test_pid_is_minus_one(self, penny_home):
        task = _make_task()
        record = spawn_claude_agent(task, "Do the thing", dry_run=True)
        assert record["pid"] == -1

    def test_session_name_follows_convention(self, penny_home):
        """Session name must be 'penny-<task_id>'."""
        task = _make_task(task_id="myproj-007")
        record = spawn_claude_agent(task, "desc", dry_run=True)
        assert record["session"] == "penny-myproj-007"

    def test_log_path_is_under_penny_home(self, penny_home):
        """Log file path must live inside PENNY_HOME/logs/."""
        task = _make_task()
        record = spawn_claude_agent(task, "desc", dry_run=True)
        log_path = Path(record["log"])
        assert log_path.is_relative_to(penny_home / "logs")

    def test_log_path_contains_task_id(self, penny_home):
        """Log filename must embed the task_id."""
        task = _make_task(task_id="watcher-xyz")
        record = spawn_claude_agent(task, "desc", dry_run=True)
        assert "watcher-xyz" in record["log"]

    def test_log_path_contains_timestamp(self, penny_home):
        """Log filename must embed a UTC timestamp (YYYYmmddTHHMMSS format)."""
        task = _make_task()
        record = spawn_claude_agent(task, "desc", dry_run=True)
        log_name = Path(record["log"]).name
        # Timestamp like 20250601T120000 — 15 consecutive digits with a T
        import re
        assert re.search(r"\d{8}T\d{6}", log_name), (
            f"Expected timestamp in log filename, got: {log_name!r}"
        )

    def test_logs_directory_is_created(self, penny_home):
        """spawn_claude_agent must create PENNY_HOME/logs/ even for dry runs."""
        task = _make_task()
        spawn_claude_agent(task, "desc", dry_run=True)
        assert (penny_home / "logs").is_dir()

    def test_record_contains_task_metadata(self, penny_home):
        """Record must capture task_id, project, title, and priority."""
        task = _make_task(
            task_id="proj-42",
            title="Fix the thing",
            priority="P2",
            project_name="myproject",
        )
        record = spawn_claude_agent(task, "desc", dry_run=True)
        assert record["task_id"] == "proj-42"
        assert record["project"] == "myproject"
        assert record["title"] == "Fix the thing"
        assert record["priority"] == "P2"

    def test_spawned_at_is_recent_utc_iso(self, penny_home):
        """spawned_at must be a UTC ISO timestamp close to now."""
        task = _make_task()
        before = datetime.now(timezone.utc)
        record = spawn_claude_agent(task, "desc", dry_run=True)
        after = datetime.now(timezone.utc)

        spawned_at = datetime.fromisoformat(record["spawned_at"])
        assert before <= spawned_at <= after

    def test_no_subprocess_spawned_during_dry_run(self, penny_home):
        """Dry run must never call subprocess.Popen."""
        task = _make_task()
        with patch("penny.spawner.subprocess.Popen") as mock_popen:
            spawn_claude_agent(task, "desc", dry_run=True)
        mock_popen.assert_not_called()

    def test_interactive_flag_recorded(self, penny_home):
        """The interactive flag passed to spawn must appear in the record."""
        task = _make_task()
        record = spawn_claude_agent(task, "desc", dry_run=True, interactive=True)
        assert record["interactive"] is True

    def test_non_interactive_flag_recorded(self, penny_home):
        task = _make_task()
        record = spawn_claude_agent(task, "desc", dry_run=True, interactive=False)
        assert record["interactive"] is False

    def test_custom_prompt_template_accepted(self, penny_home):
        """Passing a custom prompt_template must not raise in dry-run mode."""
        task = _make_task()
        tmpl = "Work on {task_id}: {task_title} in {project_path} [{priority}].\n{task_description}"
        record = spawn_claude_agent(task, "the description", dry_run=True, prompt_template=tmpl)
        assert record["status"] == "dry_run"

    def test_record_suitable_for_state_storage(self, penny_home):
        """Record must be serialisable and suitable for appending to agents_running."""
        import json

        task = _make_task()
        record = spawn_claude_agent(task, "desc", dry_run=True)
        # Must be JSON-serialisable without error
        serialised = json.dumps(record)
        roundtripped = json.loads(serialised)
        assert roundtripped["task_id"] == task.task_id
        assert roundtripped["status"] == "dry_run"
