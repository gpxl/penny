"""Unit tests for penny/tasks.py (Task dataclass) and penny/plugins/beads_plugin.py."""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from penny.tasks import Task
from penny.plugins.beads_plugin import Plugin, _parse_bd_ready


BD_READY_SAMPLE = """\
1. [● P1] [task] SetDigger-abc: Fix broken search
2. [● P2] [feature] SetDigger-def: Add export button
3. [● P3] [bug] SetDigger-ghi: Minor display glitch
"""

BD_READY_MALFORMED = """\
this line has no task id format
also bad
1. missing brackets SetDigger-xyz: title
"""


class TestParseBdReady:
    def test_parses_well_formed_lines(self):
        tasks = _parse_bd_ready(BD_READY_SAMPLE, "/tmp/proj")
        assert len(tasks) == 3
        assert tasks[0].task_id == "SetDigger-abc"
        assert tasks[0].title == "Fix broken search"
        assert tasks[0].priority == "P1"
        assert tasks[0].project_path == "/tmp/proj"
        assert tasks[0].project_name == "proj"

    def test_skips_malformed_lines_without_crashing(self):
        tasks = _parse_bd_ready(BD_READY_MALFORMED, "/tmp/proj")
        assert tasks == []

    def test_empty_output_returns_empty_list(self):
        assert _parse_bd_ready("", "/tmp/proj") == []

    def test_priority_defaults_to_p3_when_missing(self):
        # Line has task id but no priority bracket content
        line = "1. [  ] [task] myid: some title\n"
        tasks = _parse_bd_ready(line, "/tmp/proj")
        if tasks:
            assert tasks[0].priority == "P3"

    def test_priority_regex_matches_p1_through_p4(self):
        for p in ["P1", "P2", "P3", "P4"]:
            line = f"1. [● {p}] [task] proj-abc: title\n"
            tasks = _parse_bd_ready(line, "/tmp/proj")
            assert len(tasks) == 1
            assert tasks[0].priority == p


class TestBeadsPluginGetTasks:
    def test_returns_empty_when_projects_empty(self):
        plugin = Plugin()
        tasks = plugin.get_tasks([])
        assert tasks == []

    def test_returns_empty_when_bd_fails(self, tmp_path):
        plugin = Plugin()
        project = {"path": str(tmp_path), "priority": 1}
        with patch("penny.plugins.beads_plugin.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=1, stdout="", stderr="error"
            )
            tasks = plugin.get_tasks([project])
        assert tasks == []

    def test_returns_empty_when_project_path_missing(self, tmp_path):
        plugin = Plugin()
        project = {"path": str(tmp_path / "nonexistent"), "priority": 1}
        tasks = plugin.get_tasks([project])
        assert tasks == []

    def test_sorts_by_project_priority_then_task_priority(self, tmp_path):
        plugin = Plugin()
        proj1 = tmp_path / "proj1"
        proj1.mkdir()
        proj2 = tmp_path / "proj2"
        proj2.mkdir()

        def fake_run(args, **kwargs):
            cwd = kwargs.get("cwd", "")
            if "proj1" in str(cwd):
                return MagicMock(returncode=0, stdout="1. [● P2] [task] p1-aaa: Task A\n")
            if "proj2" in str(cwd):
                return MagicMock(returncode=0, stdout="1. [● P1] [task] p2-bbb: Task B\n")
            return MagicMock(returncode=0, stdout="")

        projects = [
            {"path": str(proj1), "priority": 1},
            {"path": str(proj2), "priority": 2},
        ]
        with patch("penny.plugins.beads_plugin.subprocess.run", side_effect=fake_run):
            tasks = plugin.get_tasks(projects)

        # proj1 has priority 1 (wins) even though task is P2
        assert tasks[0].project_name == "proj1"
        assert tasks[1].project_name == "proj2"
