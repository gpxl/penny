"""Beads task discovery across configured projects."""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class Task:
    task_id: str
    title: str
    priority: str
    project_path: str
    project_name: str
    raw_line: str = ""


def _run_bd(args: list[str], cwd: str) -> str:
    """Run a bd command in a given directory and return stdout."""
    try:
        result = subprocess.run(
            ["bd"] + args,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=30,
        )
        return result.stdout
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return ""


def _parse_bd_ready(output: str, project_path: str) -> list[Task]:
    """Parse `bd ready` output into Task objects."""
    tasks = []
    project_name = Path(project_path).name

    # Match lines like: 1. [● P1] [task] SetDigger-g3jj: Add missing seed data...
    pattern = re.compile(
        r"\d+\.\s+\[.*?\]\s+\[.*?\]\s+([\w-]+):\s+(.+)"
    )
    priority_pattern = re.compile(r"\[\S*\s*(P\d)\]")

    for line in output.splitlines():
        m = pattern.search(line)
        if not m:
            continue
        task_id = m.group(1).strip()
        title = m.group(2).strip()

        pm = priority_pattern.search(line)
        priority = pm.group(1) if pm else "P3"

        tasks.append(
            Task(
                task_id=task_id,
                title=title,
                priority=priority,
                project_path=project_path,
                project_name=project_name,
                raw_line=line.strip(),
            )
        )
    return tasks


def get_ready_tasks(projects: list[dict[str, Any]]) -> list[Task]:
    """
    Discover all ready (unblocked) tasks across configured projects.
    Returns tasks sorted by: project priority → task priority (P1 first).
    """
    all_tasks: list[Task] = []

    for project in projects:
        path = str(Path(project["path"]).expanduser())
        if not Path(path).exists():
            continue
        output = _run_bd(["ready"], path)
        tasks = _parse_bd_ready(output, path)
        for t in tasks:
            t._project_priority = project.get("priority", 99)  # type: ignore[attr-defined]
        all_tasks.extend(tasks)

    # Sort: project priority ASC, then task priority ASC (P1 < P2 < P3)
    priority_order = {"P1": 1, "P2": 2, "P3": 3}
    all_tasks.sort(
        key=lambda t: (
            getattr(t, "_project_priority", 99),
            priority_order.get(t.priority, 99),
        )
    )
    return all_tasks


def filter_tasks(
    tasks: list[Task],
    state: dict[str, Any],
    config: dict[str, Any],
) -> list[Task]:
    """
    Filter out already-spawned tasks and respect max_agents_per_run.
    """
    work_cfg = config.get("work", {})
    max_agents = work_cfg.get("max_agents_per_run", 2)
    priority_levels = work_cfg.get("task_priority_levels", ["P1", "P2", "P3"])

    spawned_ids = {
        s["task_id"]
        for s in state.get("spawned_this_week", [])
    }
    running_ids = {
        a["task_id"]
        for a in state.get("agents_running", [])
    }
    skip_ids = spawned_ids | running_ids

    filtered = [
        t for t in tasks
        if t.task_id not in skip_ids and t.priority in priority_levels
    ]
    return filtered[:max_agents]


def get_task_description(task: Task) -> str:
    """Fetch full task description via `bd show <task_id>`."""
    output = _run_bd(["show", task.task_id], task.project_path)
    return output if output else f"Task {task.task_id}: {task.title}"
