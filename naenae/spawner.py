"""Claude agent spawning and process management."""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .paths import data_dir

AGENT_PROMPT_TEMPLATE = """\
You are a background agent working on the project at {project_path}.

Task {task_id}: {task_title}
Priority: {priority}

Full task description:
{task_description}

Instructions (follow exactly):
1. Run: bd prime  (understand full project context)
2. Run: bd update {task_id} --status=in_progress
3. Create a git branch for this task: git checkout -b agent/{task_id}
4. Implement the solution following project conventions (TDD: write tests first, then implement)
5. Run all project tests and fix any failures
6. Run lint and fix all warnings (code is not complete until lint passes)
7. Stage and commit with a descriptive message: git add <files> && git commit -m "..."
8. Push the branch: git push -u origin agent/{task_id}
9. Open a pull request: gh pr create --title "<task title>" --body "<summary of changes>"
10. Run: bd close {task_id}
11. Run: bd sync --flush-only

Work autonomously. Do not ask for confirmation. Complete the full task end-to-end.
"""


def _open_in_terminal(cmd: str) -> None:
    """Open a shell command in a new Terminal.app window using a .command file.

    .command files are executed by Terminal.app natively — no AppleScript
    Automation permission required (unlike ``osascript do script``).
    The temp file is removed 30 s after launch to avoid accumulation.
    """
    import stat
    import tempfile
    import threading

    # Wrap the command so the .command file deletes itself after 30 s
    script = (
        "#!/bin/sh\n"
        f"{cmd}\n"
        # Background self-cleanup: wait 30 s then remove this file
        f'( sleep 30 && rm -f "$0" ) &\n'
    )
    fd, path = tempfile.mkstemp(suffix=".command")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(script)
        os.chmod(path, stat.S_IRWXU | stat.S_IRGRP | stat.S_IROTH)
        subprocess.Popen(["open", path], start_new_session=True)
    except Exception as exc:
        print(f"[naenae] _open_in_terminal failed: {exc}", flush=True)


def _logs_dir() -> Path:
    d = data_dir() / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _tmux_available() -> bool:
    return shutil.which("tmux") is not None


def _get_screen_pid(session_name: str) -> int | None:
    """Return the PID of a running detached screen session, or None if not found."""
    result = subprocess.run(
        ["screen", "-ls"],
        capture_output=True, text=True,
    )
    # screen -ls output: "\t12345.naenae-sa-xxx\t(Detached)"
    m = re.search(r"\t(\d+)\." + re.escape(session_name) + r"\t", result.stdout)
    return int(m.group(1)) if m else None


def _tmux_pane_command(session_name: str) -> str | None:
    """Return the current command running in the first pane, or None if session is gone."""
    result = subprocess.run(
        ["tmux", "list-panes", "-t", session_name, "-F", "#{pane_current_command}"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return None
    line = result.stdout.strip().splitlines()[0] if result.stdout.strip() else ""
    return line or None


def _get_tmux_pid(session_name: str) -> int | None:
    """Return the PID of the main pane process in a tmux session, or None."""
    result = subprocess.run(
        ["tmux", "list-panes", "-t", session_name, "-F", "#{pane_pid}"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return None
    try:
        return int(result.stdout.strip().splitlines()[0])
    except (ValueError, IndexError):
        return None


def _get_session_pid(session_name: str) -> int | None:
    """Return the main process PID for a tmux or screen session."""
    if _tmux_available():
        pid = _get_tmux_pid(session_name)
        if pid is not None:
            return pid
    return _get_screen_pid(session_name)


def spawn_claude_agent(
    task: Any,
    task_description: str,
    dry_run: bool = False,
    interactive: bool = False,
) -> dict[str, Any]:
    """
    Spawn a Claude agent for a task inside a named tmux session.

    interactive=True  — "Run Now" / user-triggered:
        Starts Claude in interactive mode (no -p), waits ~5 s for the TUI to
        render, then injects the task prompt via `tmux send-keys`.  Opens
        Terminal.app attached so the user lands in a live session mid-response.
        Single continuous session — no blank-terminal wait, no context loss.

    interactive=False — scheduled background spawn:
        Runs `claude -p <prompt>` via a Python runner script in a detached
        tmux session.  Claude exits when the task completes; PID tracking in
        check_running_agents() detects completion automatically.
    """
    prompt = AGENT_PROMPT_TEMPLATE.format(
        project_path=task.project_path,
        task_id=task.task_id,
        task_title=task.title,
        priority=task.priority,
        task_description=task_description,
    )

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    log_file = _logs_dir() / f"agent-{task.task_id}-{timestamp}.log"
    prompt_file = _logs_dir() / f"agent-{task.task_id}-{timestamp}.prompt"
    session_name = f"naenae-{task.task_id}"

    record: dict[str, Any] = {
        "task_id": task.task_id,
        "project": task.project_name,
        "project_path": task.project_path,
        "title": task.title,
        "priority": task.priority,
        "spawned_at": datetime.now(timezone.utc).isoformat(),
        "status": "running",
        "log": str(log_file),
        "session": session_name,
        "pid": None,
        "tmux_bin": None,
        "interactive": interactive,
    }

    if dry_run:
        record["pid"] = -1
        record["status"] = "dry_run"
        return record

    # Resolve binaries at spawn time so terminal PATH gaps don't matter
    claude_bin = shutil.which("claude")
    if claude_bin is None:
        raise RuntimeError(
            "claude binary not found in PATH. Run preflight to diagnose."
        )
    tmux_bin = shutil.which("tmux")
    screen_bin = shutil.which("screen")
    if tmux_bin is None and screen_bin is None:
        raise RuntimeError(
            "Neither tmux nor screen found in PATH. "
            "Install tmux: brew install tmux"
        )
    tmux_bin = tmux_bin or "tmux"

    # Build a minimal environment from a whitelist of known-safe variables.
    # This prevents leakage of ANTHROPIC_API_KEY, AWS_*, DATABASE_URL, etc.
    # from the parent process into spawned agent processes.
    _ENV_PASSTHROUGH = {
        "HOME", "PATH", "USER", "LOGNAME", "SHELL",
        "LANG", "LC_ALL", "LC_CTYPE", "TERM",
        "TMPDIR", "XDG_RUNTIME_DIR",
        "SSH_AUTH_SOCK",
    }
    env = {k: v for k, v in os.environ.items() if k in _ENV_PASSTHROUGH}

    # Write prompt to a file — avoids shell quoting issues and lets Claude read it directly.
    prompt_file.write_text(prompt, encoding="utf-8")

    # Kill any stale session with this name before creating a new one.
    subprocess.run([tmux_bin, "kill-session", "-t", session_name],
                   capture_output=True, check=False)

    if _tmux_available():
        record["tmux_bin"] = tmux_bin

        if interactive:
            # ── Interactive path: tmux send-keys injection ─────────────────────────
            # Start Claude in interactive mode (no -p) — one continuous session from
            # the start. No blank-terminal wait, no context loss between phases.
            proc = subprocess.Popen(
                [tmux_bin, "new-session", "-d", "-s", session_name,
                 "-x", "220", "-y", "50", claude_bin, "--dangerously-skip-permissions"],
                cwd=task.project_path, env=env, start_new_session=True,
            )
            proc.wait()

            # Wait for Claude's TUI to render its input prompt.
            # 7 s gives headroom on first launch (keychain prompt, slow disk).
            time.sleep(7)

            # Inject a single-line message that references the prompt file.
            # Newlines in the full prompt would each trigger Enter and break injection,
            # so we point Claude at the file instead of embedding the text directly.
            inject_msg = (
                f"Read the task description from {prompt_file} and follow every "
                "instruction in it. Work autonomously end-to-end without asking "
                "for confirmation."
            )
            # Claude Code's TUI requires text and Enter as separate send-keys calls;
            # combining them in one call leaves the text stuck in the input buffer.
            subprocess.run([tmux_bin, "send-keys", "-t", session_name, inject_msg],
                           check=False)
            subprocess.run([tmux_bin, "send-keys", "-t", session_name, "", "Enter"],
                           check=False)

            # Open Terminal.app attached via a .command file — Terminal opens these
            # natively without requiring Automation/AppleScript permissions.
            _open_in_terminal(shlex.join([tmux_bin, "attach-session", "-t", session_name]))

        else:
            # ── Background path: batch runner ──────────────────────────────────────
            # claude -p runs to completion and exits; PID tracking detects when done.
            runner_file = _logs_dir() / f"agent-{task.task_id}-{timestamp}.runner.py"
            runner_file.write_text(
                "import subprocess\n"
                f"with open({repr(str(prompt_file))}, encoding='utf-8') as f:\n"
                "    prompt = f.read()\n"
                f"subprocess.run([{repr(claude_bin)}, '--dangerously-skip-permissions',"
                " '-p', prompt])\n",
                encoding="utf-8",
            )
            proc = subprocess.Popen(
                [tmux_bin, "new-session", "-d", "-s", session_name,
                 "-x", "220", "-y", "50", sys.executable, str(runner_file)],
                cwd=task.project_path, env=env, start_new_session=True,
            )
            proc.wait()

    else:
        # ── Screen fallback (background only) ──────────────────────────────────────
        runner_file = _logs_dir() / f"agent-{task.task_id}-{timestamp}.runner.py"
        runner_file.write_text(
            "import subprocess\n"
            f"with open({repr(str(prompt_file))}, encoding='utf-8') as f:\n"
            "    prompt = f.read()\n"
            f"subprocess.run([{repr(claude_bin)}, '--dangerously-skip-permissions',"
            " '-p', prompt])\n",
            encoding="utf-8",
        )
        proc = subprocess.Popen(
            ["screen", "-dmS", session_name, "-h", "10000",
             sys.executable, str(runner_file)],
            cwd=task.project_path,
            env=env,
            start_new_session=True,
        )
        proc.wait()

    # Resolve the actual PID of the detached session process
    time.sleep(0.5)
    screen_pid = _get_session_pid(session_name) or 0
    record["pid"] = screen_pid
    return record


def _pid_is_alive(pid: int) -> bool:
    """Return True only if PID refers to a living, non-zombie process."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, no permission to signal — assume alive

    # os.kill(pid, 0) succeeds for zombie processes too; filter them out.
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "stat="],
            capture_output=True, text=True, timeout=2,
        )
        stat = result.stdout.strip()
        return bool(stat) and not stat.startswith("Z")
    except Exception:
        return True  # can't determine — assume alive


def check_running_agents(state: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Poll all running agents for completion.
    Returns list of newly-completed agent records.
    """
    completed = []
    still_running = []

    for agent in state.get("agents_running", []):
        pid = agent.get("pid")
        session = agent.get("session", "")

        if pid is None or pid == -1:
            completed.append({**agent, "status": "completed"})
            continue

        # pid=0 means session PID lookup failed at spawn time; check session by name
        if pid == 0:
            if session and _get_session_pid(session):
                still_running.append(agent)
            else:
                completed.append({**agent, "status": "completed"})
            continue

        # Interactive agents: check whether claude is still the active pane command.
        # PID-only tracking is unreliable — the original PID can be recycled by the OS
        # after Claude exits and the pane drops to a shell.
        if agent.get("interactive") and session and _tmux_available():
            # Grace period: spawnTask_ calls fetch() immediately after spawning, so
            # the tmux session may not have claude running yet. Skip the check for
            # agents spawned less than 90 seconds ago to avoid false completions.
            spawned_at_str = agent.get("spawned_at", "")
            try:
                spawned_at = datetime.fromisoformat(spawned_at_str)
                age_secs = (datetime.now(timezone.utc) - spawned_at).total_seconds()
            except (ValueError, TypeError):
                age_secs = 999
            if age_secs < 90:
                still_running.append(agent)
                continue

            pane_cmd = _tmux_pane_command(session)
            if pane_cmd is None or "claude" not in pane_cmd.lower():
                # Session gone or Claude no longer running in it → done
                completed.append({**agent, "status": "completed"})
            else:
                still_running.append(agent)
        elif _pid_is_alive(pid):
            still_running.append(agent)
        else:
            completed.append({**agent, "status": "completed"})

    state["agents_running"] = still_running
    return completed


def send_notification(title: str, message: str) -> None:
    """Send a macOS Notification Center notification via NSUserNotificationCenter."""
    try:
        from AppKit import NSUserNotification, NSUserNotificationCenter
        note = NSUserNotification.alloc().init()
        note.setTitle_(title)
        note.setInformativeText_(message)
        NSUserNotificationCenter.defaultUserNotificationCenter().deliverNotification_(note)
    except Exception:
        pass  # Notifications are best-effort
