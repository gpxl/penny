"""Smoke tests for the penny CLI (scripts/penny).

These test the shell script's output and exit codes without requiring
a running Penny instance (except where noted).
"""

from __future__ import annotations

import http.server
import json
import os
import socket
import subprocess
import threading
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


# ── CLI happy-path tests with mock server ────────────────────────────────────

_SAMPLE_STATE = {
    "generated_at": "2025-03-07T12:00:00",
    "prediction": {"pct_all": 42.0, "pct_sonnet": 30.0},
    "ready_tasks": [
        {"task_id": "t-1", "title": "Fix bug", "priority": "P1", "project_name": "proj"},
        {"task_id": "t-2", "title": "Add feature", "priority": "P2", "project_name": "proj2"},
    ],
    "state": {
        "agents_running": [
            {"task_id": "a-1", "project_name": "proj", "title": "Running task"},
        ],
        "recently_completed": [],
    },
    "completed_this_period": [],
    "session_history": [],
}


class _MockHandler(http.server.BaseHTTPRequestHandler):
    """Minimal handler responding to dashboard API requests for CLI testing."""

    def do_GET(self):
        if self.path == "/api/state":
            body = json.dumps(_SAMPLE_STATE).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_error(404)

    def do_POST(self):
        # Read and discard body
        length = int(self.headers.get("Content-Length", 0))
        if length:
            self.rfile.read(length)
        body = json.dumps({"ok": True}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def cli_env(tmp_path):
    """Set up a mock environment: fake launchctl, mock HTTP server, temp PENNY_HOME."""
    # PENNY_HOME layout
    penny_home = tmp_path / "penny_home"
    penny_home.mkdir()
    log_dir = penny_home / "logs"
    log_dir.mkdir()
    log_file = log_dir / "launchd.log"
    log_file.write_text("\n".join(f"log line {i}" for i in range(100)) + "\n")
    (penny_home / "config.yaml").write_text("projects: []\n")

    # Fake launchctl that always succeeds
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_launchctl = bin_dir / "launchctl"
    fake_launchctl.write_text("#!/bin/sh\nexit 0\n")
    os.chmod(str(fake_launchctl), 0o755)

    # Fake open command that echoes arguments
    fake_open = bin_dir / "open"
    fake_open.write_text('#!/bin/sh\necho "OPEN: $*"\n')
    os.chmod(str(fake_open), 0o755)

    # Start mock HTTP server on a random port
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    server = http.server.HTTPServer(("127.0.0.1", port), _MockHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()

    # Write port file
    (penny_home / ".dashboard_port").write_text(str(port))

    env = {
        **os.environ,
        "PATH": str(bin_dir) + ":" + os.environ.get("PATH", ""),
        "PENNY_HOME": str(penny_home),
    }
    yield env, port, penny_home

    server.shutdown()


def _run_env(args: list[str], env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        [CLI] + args,
        capture_output=True,
        text=True,
        timeout=10,
        env=env,
    )


class TestCLIHappyPath:
    """Test CLI commands with a running mock dashboard server."""

    def test_status_shows_running(self, cli_env):
        env, _, _ = cli_env
        result = _run_env(["status"], env)
        assert result.returncode == 0
        assert "Penny is running" in result.stdout

    def test_tasks_shows_table(self, cli_env):
        env, _, _ = cli_env
        result = _run_env(["tasks"], env)
        assert result.returncode == 0
        assert "t-1" in result.stdout
        assert "Fix bug" in result.stdout
        assert "P1" in result.stdout

    def test_tasks_shows_multiple(self, cli_env):
        env, _, _ = cli_env
        result = _run_env(["tasks"], env)
        assert "t-2" in result.stdout
        assert "Add feature" in result.stdout

    def test_agents_shows_running(self, cli_env):
        env, _, _ = cli_env
        result = _run_env(["agents"], env)
        assert result.returncode == 0
        assert "a-1" in result.stdout
        assert "Running task" in result.stdout

    def test_refresh_succeeds(self, cli_env):
        env, _, _ = cli_env
        result = _run_env(["refresh"], env)
        assert result.returncode == 0
        assert "Refresh triggered" in result.stdout

    def test_run_task_succeeds(self, cli_env):
        env, _, _ = cli_env
        result = _run_env(["run", "t-1"], env)
        assert result.returncode == 0
        assert "Agent started" in result.stdout

    def test_stop_agent_succeeds(self, cli_env):
        env, _, _ = cli_env
        result = _run_env(["stop-agent", "a-1"], env)
        assert result.returncode == 0
        assert "Stopped agent" in result.stdout

    def test_dismiss_succeeds(self, cli_env):
        env, _, _ = cli_env
        result = _run_env(["dismiss", "d-1"], env)
        assert result.returncode == 0
        assert "Dismissed" in result.stdout

    def test_clear_completed_succeeds(self, cli_env):
        env, _, _ = cli_env
        result = _run_env(["clear-completed"], env)
        assert result.returncode == 0
        assert "Cleared completed" in result.stdout

    def test_quit_succeeds(self, cli_env):
        env, _, _ = cli_env
        result = _run_env(["quit"], env)
        assert result.returncode == 0
        assert "Penny quit" in result.stdout


class TestCLILogsCommand:
    def test_logs_shows_output(self, cli_env):
        env, _, _ = cli_env
        result = _run_env(["logs"], env)
        assert result.returncode == 0
        assert "log line" in result.stdout

    def test_logs_shows_last_80_lines(self, cli_env):
        env, _, _ = cli_env
        result = _run_env(["logs"], env)
        lines = [line for line in result.stdout.strip().split("\n") if line.startswith("log line")]
        assert len(lines) == 80

    def test_logs_includes_recent_not_old(self, cli_env):
        """tail -n 80 on 100 lines should include line 99 but not line 0."""
        env, _, _ = cli_env
        result = _run_env(["logs"], env)
        assert "log line 99" in result.stdout
        # Line 0 through 19 should be excluded (100 - 80 = 20)
        assert "log line 0\n" not in result.stdout


class TestCLIOpenCommand:
    def test_open_runs_without_error(self, cli_env):
        env, _, _ = cli_env
        result = _run_env(["open"], env)
        assert result.returncode == 0

    def test_open_invokes_browser(self, cli_env):
        env, port, _ = cli_env
        result = _run_env(["open"], env)
        # Our fake open command echoes "OPEN: http://..."
        assert "OPEN:" in result.stdout
        assert str(port) in result.stdout


class TestCLIPrefsCommand:
    def test_prefs_runs_without_error(self, cli_env):
        env, _, _ = cli_env
        result = _run_env(["prefs"], env)
        assert result.returncode == 0

    def test_prefs_opens_config_file(self, cli_env):
        env, _, penny_home = cli_env
        result = _run_env(["prefs"], env)
        assert "OPEN:" in result.stdout
        assert "config.yaml" in result.stdout


class TestCLIStartCommand:
    def test_start_when_already_running(self, cli_env):
        env, _, _ = cli_env
        result = _run_env(["start"], env)
        assert result.returncode == 0
        assert "already running" in result.stdout.lower()
