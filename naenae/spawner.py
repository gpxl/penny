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
3. Implement the solution following project conventions (TDD: write tests first, then implement)
4. Run all project tests and fix any failures
5. Run lint and fix all warnings (code is not complete until lint passes)
6. Commit with a descriptive message
7. Run: bd close {task_id}
8. Run: bd sync --flush-only

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

        # Check if process is still alive
        try:
            os.kill(pid, 0)  # signal 0 = check existence only
            still_running.append(agent)
        except ProcessLookupError:
            completed.append({**agent, "status": "completed"})
        except PermissionError:
            # Process exists but we can't signal it (still alive)
            still_running.append(agent)

    state["agents_running"] = still_running
    return completed


def send_notification(title: str, message: str) -> None:
    """Send a macOS Notification Center notification via osascript."""
    script = (
        f'display notification "{message}" with title "{title}"'
    )
    try:
        subprocess.run(
            ["osascript", "-e", script],
            check=False,
            capture_output=True,
            timeout=5,
        )
    except Exception:
        pass  # Notifications are best-effort
