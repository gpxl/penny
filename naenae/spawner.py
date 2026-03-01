"""Claude agent spawning and process management."""

from __future__ import annotations

import os
import subprocess
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


def _logs_dir() -> Path:
    d = data_dir() / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def spawn_claude_agent(
    task: Any,
    task_description: str,
    dry_run: bool = False,
) -> dict[str, Any]:
    """
    Spawn a Claude agent for a task as a background process.
    Returns a dict with PID, task metadata, and log path.
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

    record: dict[str, Any] = {
        "task_id": task.task_id,
        "project": task.project_name,
        "project_path": task.project_path,
        "title": task.title,
        "priority": task.priority,
        "spawned_at": datetime.now(timezone.utc).isoformat(),
        "status": "running",
        "log": str(log_file),
        "pid": None,
    }

    if dry_run:
        record["pid"] = -1
        record["status"] = "dry_run"
        return record

    # Build environment: unset CLAUDECODE so nested sessions are allowed
    env = os.environ.copy()
    env.pop("CLAUDECODE", None)
    env.pop("CLAUDE_CODE", None)

    log_fh = log_file.open("w")
    log_fh.write(f"# Nae Nae agent — {task.task_id}\n")
    log_fh.write(f"# Spawned at {record['spawned_at']}\n")
    log_fh.write(f"# Project: {task.project_path}\n\n")
    log_fh.write("=== PROMPT ===\n")
    log_fh.write(prompt)
    log_fh.write("\n=== OUTPUT ===\n")
    log_fh.flush()

    proc = subprocess.Popen(
        [
            "claude",
            "--dangerously-skip-permissions",
            "-p",
            prompt,
        ],
        cwd=task.project_path,
        env=env,
        stdout=log_fh,
        stderr=subprocess.STDOUT,
        start_new_session=True,  # detach from parent process group
    )

    record["pid"] = proc.pid
    record["_proc_handle"] = proc  # ephemeral — not serialised to JSON
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
        if pid is None or pid == -1:
            completed.append({**agent, "status": "completed"})
            continue

        if _pid_is_alive(pid):
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
