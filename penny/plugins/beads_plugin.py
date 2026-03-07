"""Beads integration plugin for Penny.

Provides task discovery, agent prompts, preflight checks, and UI sections
for projects using the Beads issue tracker (bd CLI).
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from ..plugin import PennyPlugin
from ..preflight import PreflightIssue
from ..tasks import Task

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


def _parse_bd_list(output: str, project_path: str) -> list[Task]:
    """Parse `bd list --status=closed` output into Task objects.

    Format: ✓ <id> [P<n>] [<type>] - <title>
    """
    tasks = []
    project_name = Path(project_path).name
    pattern = re.compile(r"✓\s+([\w-]+)\s+\[(P\d)\]\s+\[.*?\]\s+-\s+(.+)")
    for line in output.splitlines():
        m = pattern.search(line)
        if not m:
            continue
        tasks.append(
            Task(
                task_id=m.group(1).strip(),
                title=m.group(3).strip(),
                priority=m.group(2),
                project_path=project_path,
                project_name=project_name,
                raw_line=line.strip(),
            )
        )
    return tasks


class Plugin(PennyPlugin):
    """Beads issue tracker integration."""

    def __init__(self) -> None:
        self._app: Any = None

    @property
    def name(self) -> str:
        return "beads"

    @property
    def description(self) -> str:
        return "Task discovery and agent prompts via Beads (bd CLI)"

    def is_available(self) -> bool:
        return shutil.which("bd") is not None

    def on_activate(self, app: Any) -> None:
        self._app = app

    def on_first_activated(self, app: Any) -> None:
        """Notify the user that beads was detected and task management is now active."""
        try:
            from ..spawner import send_notification
            send_notification(
                "Penny",
                "Beads detected \u2014 task management activated. Run \u2018bd ready\u2019 to see ready tasks.",
            )
        except Exception:
            pass

    def on_deactivate(self) -> None:
        self._app = None

    def preflight_checks(self, config: dict[str, Any]) -> list[PreflightIssue]:
        issues: list[PreflightIssue] = []

        if shutil.which("bd") is None:
            issues.append(PreflightIssue(
                severity="error",
                message="`bd` (beads) CLI not found in PATH.",
                fix_hint="Install it: brew install beads  (or: npm install -g @beads/bd)\n"
                         "Then re-run install.sh so launchd picks up the new PATH.",
            ))

        for entry in config.get("projects", []):
            path_str: str = entry.get("path", "")
            if "PLACEHOLDER" in path_str:
                continue
            project_path = Path(path_str).expanduser()
            if not project_path.exists():
                continue
            beads_dir = project_path / ".beads"
            if not beads_dir.exists():
                issues.append(PreflightIssue(
                    severity="warning",
                    message=f"No .beads/ directory in {project_path}.",
                    fix_hint=f"Run `bd init` inside {project_path} to initialise beads.",
                ))

        return issues

    def get_tasks(self, projects: list[dict[str, Any]]) -> list[Task]:
        all_tasks: list[Task] = []

        for project in projects:
            path = str(Path(project["path"]).expanduser())
            if not Path(path).exists():
                continue
            output = _run_bd(["ready"], path)
            tasks = _parse_bd_ready(output, path)
            for t in tasks:
                t.metadata["project_priority"] = project.get("priority", 99)
            all_tasks.extend(tasks)

        priority_order = {"P1": 1, "P2": 2, "P3": 3}
        all_tasks.sort(
            key=lambda t: (
                t.metadata.get("project_priority", 99),
                priority_order.get(t.priority, 99),
            )
        )
        return all_tasks

    def on_agent_spawned(self, task: Task, record: dict[str, Any], plugin_state: dict[str, Any]) -> None:
        plugin_state.setdefault("spawned_task_ids", []).append(task.task_id)

    def on_agent_completed(self, record: dict[str, Any], plugin_state: dict[str, Any]) -> None:
        pass

    def filter_tasks(
        self,
        tasks: list[Task],
        state: dict[str, Any],
        config: dict[str, Any],
    ) -> list[Task]:
        work_cfg = config.get("work", {})
        max_agents = work_cfg.get("max_agents_per_run", 2)
        priority_levels = work_cfg.get("task_priority_levels", ["P1", "P2", "P3"])

        beads_state = state.get("plugin_state", {}).get("beads", {})
        spawned_ids = set(beads_state.get("spawned_task_ids", []))
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

    def get_completed_tasks(
        self, projects: list[dict[str, Any]], plugin_state: dict[str, Any]
    ) -> list[Task]:
        seen_ids = set(plugin_state.get("seen_closed_ids", []))
        new_tasks: list[Task] = []
        for project in projects:
            path = str(Path(project["path"]).expanduser())
            if not Path(path).exists():
                continue
            output = _run_bd(["list", "--status=closed"], path)
            tasks = _parse_bd_list(output, path)
            for task in tasks:
                if task.task_id not in seen_ids:
                    new_tasks.append(task)
                    seen_ids.add(task.task_id)
        plugin_state["seen_closed_ids"] = list(seen_ids)
        return new_tasks

    def get_task_description(self, task: Task) -> str | None:
        output = _run_bd(["show", task.task_id], task.project_path)
        return output if output else None

    def get_agent_prompt_template(self) -> str | None:
        return AGENT_PROMPT_TEMPLATE

    def handle_action(self, action: str, payload: Any) -> bool:
        """Handle bd CLI actions dispatched from the UI."""
        if action != "bd_command":
            return False
        args, cwd = payload
        str_args = [str(a) for a in args]
        str_cwd = str(cwd) if cwd else ""
        if not str_cwd:
            return False
        try:
            r = subprocess.run(
                ["bd"] + str_args,
                cwd=str_cwd,
                capture_output=True,
                text=True,
                timeout=30,
            )
            if r.returncode != 0:
                print(f"[penny] bd {str_args} failed (rc={r.returncode}): {r.stderr.strip()}", flush=True)
        except Exception as exc:
            print(f"[penny] bd {str_args} exception: {exc}", flush=True)
        return True

    def cli_commands(self) -> list[dict[str, Any]]:
        return [
            {"name": "tasks",         "description": "List ready beads tasks",       "api_path": "/api/state", "method": "GET"},
            {"name": "agents",        "description": "List running Claude agents",    "api_path": "/api/state", "method": "GET"},
            {"name": "run",           "description": "Spawn a Claude agent for a task", "api_path": "/api/run",  "method": "POST", "arg": "task-id"},
            {"name": "stop-agent",    "description": "Stop a running agent",          "api_path": "/api/stop-agent", "method": "POST", "arg": "task-id"},
            {"name": "dismiss",       "description": "Dismiss a completed task",      "api_path": "/api/dismiss",    "method": "POST", "arg": "task-id"},
            {"name": "clear-completed", "description": "Clear all completed tasks",   "api_path": "/api/clear-completed", "method": "POST"},
        ]

    def config_schema(self) -> dict[str, Any]:
        return {
            "enabled": {
                "type": "string",
                "default": "auto",
                "description": "Enable beads plugin: true, false, or auto (detect)",
            },
        }
