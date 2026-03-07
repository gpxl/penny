"""Unit tests for penny/spawner.py."""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

from penny.spawner import (
    _build_claude_flags,
    _get_screen_pid,
    _get_tmux_pid,
    _pid_is_alive,
    _tmux_available,
    _tmux_pane_command,
    _write_secure_file,
    check_running_agents,
    spawn_claude_agent,
)
from penny.tasks import Task


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


class TestBuildClaudeFlags:
    def test_full_mode_returns_dangerously_skip(self):
        assert _build_claude_flags({"work": {"agent_permissions": "full"}}) == [
            "--dangerously-skip-permissions"
        ]

    def test_default_mode_returns_dangerously_skip(self):
        assert _build_claude_flags({}) == ["--dangerously-skip-permissions"]

    def test_none_config_returns_dangerously_skip(self):
        assert _build_claude_flags(None) == ["--dangerously-skip-permissions"]

    def test_scoped_mode_returns_allowed_tools(self):
        config = {
            "work": {
                "agent_permissions": "scoped",
                "allowed_tools": ["Read", "Edit", "Bash(git:*)"],
            }
        }
        flags = _build_claude_flags(config)
        assert flags == ["--allowed-tools", "Read,Edit,Bash(git:*)"]

    def test_scoped_mode_without_tools_falls_back_to_full(self):
        config = {"work": {"agent_permissions": "scoped", "allowed_tools": []}}
        assert _build_claude_flags(config) == ["--dangerously-skip-permissions"]

    def test_scoped_mode_missing_allowed_tools_key_falls_back_to_full(self):
        config = {"work": {"agent_permissions": "scoped"}}
        assert _build_claude_flags(config) == ["--dangerously-skip-permissions"]

    def test_off_mode_returns_dangerously_skip(self):
        # off mode is handled by callers — _build_claude_flags is not called
        # when mode is off, but if it were called, it defaults to full
        config = {"work": {"agent_permissions": "off"}}
        assert _build_claude_flags(config) == ["--dangerously-skip-permissions"]


class TestTmuxAvailable:
    def test_returns_true_when_tmux_found(self):
        with patch("penny.spawner.shutil.which", return_value="/usr/bin/tmux"):
            assert _tmux_available() is True

    def test_returns_false_when_tmux_missing(self):
        with patch("penny.spawner.shutil.which", return_value=None):
            assert _tmux_available() is False


class TestGetScreenPid:
    def test_returns_pid_when_session_found(self):
        output = "\t12345.penny-task-abc\t(Detached)\n"
        mock_result = MagicMock(stdout=output)
        with patch("penny.spawner.subprocess.run", return_value=mock_result):
            pid = _get_screen_pid("penny-task-abc")
        assert pid == 12345

    def test_returns_none_when_session_not_found(self):
        mock_result = MagicMock(stdout="No Sockets found.\n")
        with patch("penny.spawner.subprocess.run", return_value=mock_result):
            pid = _get_screen_pid("penny-nonexistent")
        assert pid is None


class TestGetTmuxPid:
    def test_returns_pid_on_success(self):
        mock_result = MagicMock(returncode=0, stdout="9876\n")
        with patch("penny.spawner.subprocess.run", return_value=mock_result):
            pid = _get_tmux_pid("penny-task-abc")
        assert pid == 9876

    def test_returns_none_when_session_missing(self):
        mock_result = MagicMock(returncode=1, stdout="")
        with patch("penny.spawner.subprocess.run", return_value=mock_result):
            pid = _get_tmux_pid("penny-nonexistent")
        assert pid is None

    def test_returns_none_on_bad_output(self):
        mock_result = MagicMock(returncode=0, stdout="not-a-number\n")
        with patch("penny.spawner.subprocess.run", return_value=mock_result):
            pid = _get_tmux_pid("penny-task-abc")
        assert pid is None


class TestTmuxPaneCommand:
    def test_returns_command_name(self):
        mock_result = MagicMock(returncode=0, stdout="claude\n")
        with patch("penny.spawner.subprocess.run", return_value=mock_result):
            cmd = _tmux_pane_command("penny-task-abc")
        assert cmd == "claude"

    def test_returns_none_when_session_gone(self):
        mock_result = MagicMock(returncode=1, stdout="")
        with patch("penny.spawner.subprocess.run", return_value=mock_result):
            cmd = _tmux_pane_command("penny-nonexistent")
        assert cmd is None


class TestSpawnClaudeAgentDryRun:
    def test_dry_run_returns_dry_run_status(self):
        task = _make_task()
        with patch("penny.spawner.data_dir") as mock_dd:
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
            patch("penny.spawner.data_dir") as mock_dd,
            patch("penny.spawner.subprocess.Popen") as mock_popen,
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
        with patch("penny.spawner.data_dir") as mock_dd:
            mock_dd.return_value = MagicMock()
            mock_dd.return_value.__truediv__ = lambda s, x: MagicMock(
                mkdir=MagicMock(),
                __truediv__=lambda s2, y: MagicMock(write_text=MagicMock()),
            )
            record = spawn_claude_agent(task, "desc", dry_run=True)
        assert record["session"] == "penny-myproject-xyz"


class TestWriteSecureFile:
    def test_file_has_owner_only_permissions(self, tmp_path):
        target = tmp_path / "secret.txt"
        _write_secure_file(target, "sensitive content")
        mode = target.stat().st_mode & 0o777
        assert mode == 0o600

    def test_file_content_is_written(self, tmp_path):
        target = tmp_path / "secret.txt"
        _write_secure_file(target, "hello world")
        assert target.read_text(encoding="utf-8") == "hello world"

    def test_overwrites_existing_file(self, tmp_path):
        target = tmp_path / "existing.txt"
        target.write_text("old content")
        os.chmod(target, 0o644)
        _write_secure_file(target, "new content")
        assert target.read_text(encoding="utf-8") == "new content"
        mode = target.stat().st_mode & 0o777
        assert mode == 0o600


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
                    "session": "penny-proj-abc",
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
                    "session": "penny-proj-xyz",
                    "interactive": False,
                }
            ]
        }
        with patch("penny.spawner._pid_is_alive", return_value=True):
            completed = check_running_agents(state)
        assert completed == []
        assert len(state["agents_running"]) == 1

    def test_dead_pid_moves_to_completed(self):
        state = {
            "agents_running": [
                {
                    "task_id": "proj-dead",
                    "pid": 99999,
                    "session": "penny-proj-dead",
                    "interactive": False,
                }
            ]
        }
        with patch("penny.spawner._pid_is_alive", return_value=False):
            completed = check_running_agents(state)
        assert len(completed) == 1
        assert completed[0]["task_id"] == "proj-dead"
        assert state["agents_running"] == []
