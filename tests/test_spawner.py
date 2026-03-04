"""Unit tests for naenae/spawner.py."""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

from naenae.spawner import (
    _get_screen_pid,
    _get_tmux_pid,
    _pid_is_alive,
    _tmux_available,
    _tmux_pane_command,
    check_running_agents,
    spawn_claude_agent,
)
from naenae.tasks import Task


def _make_task(**overrides):
    defaults = {
        "task_id": "proj-abc",
        "title": "Test task",
        "priority": "P1",
        "project_path": "/tmp/test-proj",
        "project_name": "test-proj",
    }
    defaults.update(overrides)
    return Task(**defaults)


class TestTmuxAvailable:
    def test_returns_true_when_tmux_found(self):
        with patch("naenae.spawner.shutil.which", return_value="/usr/bin/tmux"):
            assert _tmux_available() is True

    def test_returns_false_when_tmux_missing(self):
        with patch("naenae.spawner.shutil.which", return_value=None):
            assert _tmux_available() is False


class TestGetScreenPid:
    def test_returns_pid_when_session_found(self):
        output = "\t12345.naenae-task-abc\t(Detached)\n"
        mock_result = MagicMock(stdout=output)
        with patch("naenae.spawner.subprocess.run", return_value=mock_result):
            pid = _get_screen_pid("naenae-task-abc")
        assert pid == 12345

    def test_returns_none_when_session_not_found(self):
        mock_result = MagicMock(stdout="No Sockets found.\n")
        with patch("naenae.spawner.subprocess.run", return_value=mock_result):
            pid = _get_screen_pid("naenae-nonexistent")
        assert pid is None


class TestGetTmuxPid:
    def test_returns_pid_on_success(self):
        mock_result = MagicMock(returncode=0, stdout="9876\n")
        with patch("naenae.spawner.subprocess.run", return_value=mock_result):
            pid = _get_tmux_pid("naenae-task-abc")
        assert pid == 9876

    def test_returns_none_when_session_missing(self):
        mock_result = MagicMock(returncode=1, stdout="")
        with patch("naenae.spawner.subprocess.run", return_value=mock_result):
            pid = _get_tmux_pid("naenae-nonexistent")
        assert pid is None

    def test_returns_none_on_bad_output(self):
        mock_result = MagicMock(returncode=0, stdout="not-a-number\n")
        with patch("naenae.spawner.subprocess.run", return_value=mock_result):
            pid = _get_tmux_pid("naenae-task-abc")
        assert pid is None


class TestTmuxPaneCommand:
    def test_returns_command_name(self):
        mock_result = MagicMock(returncode=0, stdout="claude\n")
        with patch("naenae.spawner.subprocess.run", return_value=mock_result):
            cmd = _tmux_pane_command("naenae-task-abc")
        assert cmd == "claude"

    def test_returns_none_when_session_gone(self):
        mock_result = MagicMock(returncode=1, stdout="")
        with patch("naenae.spawner.subprocess.run", return_value=mock_result):
            cmd = _tmux_pane_command("naenae-nonexistent")
        assert cmd is None


class TestSpawnClaudeAgentDryRun:
    def test_dry_run_returns_dry_run_status(self):
        task = _make_task()
        with patch("naenae.spawner.data_dir") as mock_dd:
            mock_dd.return_value = MagicMock()
            mock_dd.return_value.__truediv__ = lambda s, x: MagicMock(
                mkdir=MagicMock(),
                __truediv__=lambda s2, y: MagicMock(write_text=MagicMock()),
            )
            record = spawn_claude_agent(task, "description", dry_run=True)

        assert record["status"] == "dry_run"
        assert record["pid"] == -1
        assert record["task_id"] == "proj-abc"

    def test_dry_run_does_not_spawn_process(self):
        task = _make_task()
        with (
            patch("naenae.spawner.data_dir") as mock_dd,
            patch("naenae.spawner.subprocess.Popen") as mock_popen,
        ):
            mock_dd.return_value = MagicMock()
            mock_dd.return_value.__truediv__ = lambda s, x: MagicMock(
                mkdir=MagicMock(),
                __truediv__=lambda s2, y: MagicMock(write_text=MagicMock()),
            )
            spawn_claude_agent(task, "description", dry_run=True)
        mock_popen.assert_not_called()

    def test_session_name_format(self):
        task = _make_task(task_id="myproject-xyz")
        with patch("naenae.spawner.data_dir") as mock_dd:
            mock_dd.return_value = MagicMock()
            mock_dd.return_value.__truediv__ = lambda s, x: MagicMock(
                mkdir=MagicMock(),
                __truediv__=lambda s2, y: MagicMock(write_text=MagicMock()),
            )
            record = spawn_claude_agent(task, "desc", dry_run=True)
        assert record["session"] == "naenae-myproject-xyz"


class TestPidIsAlive:
    def test_returns_false_for_process_lookup_error(self):
        with patch("os.kill", side_effect=ProcessLookupError):
            assert _pid_is_alive(99999) is False

    def test_returns_true_for_permission_error(self):
        with patch("os.kill", side_effect=PermissionError):
            assert _pid_is_alive(99999) is True


class TestCheckRunningAgents:
    def test_pid_minus_1_moves_to_completed_immediately(self):
        state = {
            "agents_running": [
                {
                    "task_id": "proj-abc",
                    "pid": -1,
                    "session": "naenae-proj-abc",
                    "interactive": False,
                }
            ]
        }
        completed = check_running_agents(state)
        assert len(completed) == 1
        assert completed[0]["status"] == "completed"
        assert state["agents_running"] == []

    def test_alive_pid_stays_in_running(self):
        state = {
            "agents_running": [
                {
                    "task_id": "proj-xyz",
                    "pid": 12345,
                    "session": "naenae-proj-xyz",
                    "interactive": False,
                }
            ]
        }
        with patch("naenae.spawner._pid_is_alive", return_value=True):
            completed = check_running_agents(state)
        assert completed == []
        assert len(state["agents_running"]) == 1

    def test_dead_pid_moves_to_completed(self):
        state = {
            "agents_running": [
                {
                    "task_id": "proj-dead",
                    "pid": 99999,
                    "session": "naenae-proj-dead",
                    "interactive": False,
                }
            ]
        }
        with patch("naenae.spawner._pid_is_alive", return_value=False):
            completed = check_running_agents(state)
        assert len(completed) == 1
        assert completed[0]["task_id"] == "proj-dead"
        assert state["agents_running"] == []
