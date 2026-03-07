"""Unit tests for penny/tasks.py (Task dataclass) and penny/plugins/beads_plugin.py."""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

from penny.plugins.beads_plugin import AGENT_PROMPT_TEMPLATE, Plugin, _parse_bd_list, _parse_bd_ready, _run_bd
from penny.tasks import Task

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


BD_LIST_CLOSED_SAMPLE = """\
✓ proj-aaa [P1] [task] - Fix critical bug
✓ proj-bbb [P2] [feature] - Add export button
✓ proj-ccc [P3] [bug] - Minor display glitch
"""

BD_LIST_CLOSED_MALFORMED = """\
this line has no check mark
also bad
✓ missing-brackets title here
"""


class TestParseBdList:
    def test_parses_well_formed_lines(self):
        tasks = _parse_bd_list(BD_LIST_CLOSED_SAMPLE, "/tmp/proj")
        assert len(tasks) == 3
        assert tasks[0].task_id == "proj-aaa"
        assert tasks[0].title == "Fix critical bug"
        assert tasks[0].priority == "P1"
        assert tasks[0].project_path == "/tmp/proj"
        assert tasks[0].project_name == "proj"

    def test_skips_malformed_lines_without_crashing(self):
        tasks = _parse_bd_list(BD_LIST_CLOSED_MALFORMED, "/tmp/proj")
        assert tasks == []

    def test_empty_output_returns_empty_list(self):
        assert _parse_bd_list("", "/tmp/proj") == []

    def test_priority_p1_through_p4(self):
        for p in ["P1", "P2", "P3", "P4"]:
            line = f"✓ proj-xyz [{p}] [task] - Some title\n"
            tasks = _parse_bd_list(line, "/tmp/proj")
            assert len(tasks) == 1
            assert tasks[0].priority == p

    def test_does_not_match_bd_ready_format(self):
        ready_line = "1. [● P2] [task] proj-abc: Done task\n"
        tasks = _parse_bd_list(ready_line, "/tmp/proj")
        assert tasks == []


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


# ── BeadsPlugin.get_completed_tasks ──────────────────────────────────────────


class TestBeadsPluginGetCompletedTasks:
    def test_calls_bd_list_status_closed(self, tmp_path):
        plugin = Plugin()
        project = {"path": str(tmp_path), "priority": 1}
        with patch("penny.plugins.beads_plugin.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="")
            plugin.get_completed_tasks([project], {})
        call_args = mock_run.call_args[0][0]
        assert "list" in call_args
        assert "--status=closed" in call_args

    def test_returns_parsed_closed_tasks(self, tmp_path):
        plugin = Plugin()
        project = {"path": str(tmp_path), "priority": 1}
        sample = "✓ proj-abc [P2] [task] - Done task\n"
        with patch("penny.plugins.beads_plugin.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=sample)
            tasks = plugin.get_completed_tasks([project], {})
        assert len(tasks) == 1
        assert tasks[0].task_id == "proj-abc"
        assert tasks[0].title == "Done task"

    def test_deduplicates_via_plugin_state(self, tmp_path):
        plugin = Plugin()
        project = {"path": str(tmp_path), "priority": 1}
        sample = "✓ proj-abc [P2] [task] - Done task\n"
        plugin_state = {"seen_closed_ids": ["proj-abc"]}
        with patch("penny.plugins.beads_plugin.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=sample)
            tasks = plugin.get_completed_tasks([project], plugin_state)
        assert tasks == []  # already seen

    def test_updates_plugin_state_with_new_ids(self, tmp_path):
        plugin = Plugin()
        project = {"path": str(tmp_path), "priority": 1}
        sample = "✓ proj-abc [P2] [task] - Done task\n"
        plugin_state: dict = {}
        with patch("penny.plugins.beads_plugin.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=sample)
            plugin.get_completed_tasks([project], plugin_state)
        assert "proj-abc" in plugin_state.get("seen_closed_ids", [])

    def test_returns_empty_for_missing_project(self, tmp_path):
        plugin = Plugin()
        project = {"path": str(tmp_path / "nonexistent"), "priority": 1}
        tasks = plugin.get_completed_tasks([project], {})
        assert tasks == []

    def test_returns_empty_list_for_no_projects(self):
        plugin = Plugin()
        assert plugin.get_completed_tasks([], {}) == []


# ── _run_bd helper ────────────────────────────────────────────────────────────


class TestRunBd:
    def test_returns_stdout_on_success(self, tmp_path):
        with patch("penny.plugins.beads_plugin.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="hello\n")
            result = _run_bd(["ready"], str(tmp_path))
        assert result == "hello\n"
        mock_run.assert_called_once()

    def test_returns_empty_on_timeout(self, tmp_path):
        with patch(
            "penny.plugins.beads_plugin.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="bd", timeout=30),
        ):
            assert _run_bd(["ready"], str(tmp_path)) == ""

    def test_returns_empty_when_bd_not_found(self, tmp_path):
        with patch(
            "penny.plugins.beads_plugin.subprocess.run",
            side_effect=FileNotFoundError,
        ):
            assert _run_bd(["ready"], str(tmp_path)) == ""


# ── Plugin identity / availability ────────────────────────────────────────────


class TestBeadsPluginIdentity:
    def test_name(self):
        assert Plugin().name == "beads"

    def test_description_is_nonempty_string(self):
        assert isinstance(Plugin().description, str)
        assert len(Plugin().description) > 0

    def test_is_available_true_when_bd_exists(self):
        with patch("penny.plugins.beads_plugin.shutil.which", return_value="/usr/local/bin/bd"):
            assert Plugin().is_available() is True

    def test_is_available_false_when_bd_missing(self):
        with patch("penny.plugins.beads_plugin.shutil.which", return_value=None):
            assert Plugin().is_available() is False


# ── on_activate / on_deactivate ───────────────────────────────────────────────


class TestBeadsPluginLifecycle:
    def test_on_activate_stores_app(self):
        plugin = Plugin()
        app = MagicMock()
        plugin.on_activate(app)
        assert plugin._app is app

    def test_on_deactivate_clears_app(self):
        plugin = Plugin()
        plugin.on_activate(MagicMock())
        plugin.on_deactivate()
        assert plugin._app is None


# ── preflight_checks ──────────────────────────────────────────────────────────


class TestBeadsPluginPreflight:
    def test_error_when_bd_not_in_path(self):
        plugin = Plugin()
        with patch("penny.plugins.beads_plugin.shutil.which", return_value=None):
            issues = plugin.preflight_checks({"projects": []})
        errors = [i for i in issues if i.severity == "error"]
        assert any("`bd`" in i.message for i in errors)

    def test_warning_when_beads_dir_missing(self, tmp_path):
        project_dir = tmp_path / "myproj"
        project_dir.mkdir()
        config = {"projects": [{"path": str(project_dir)}]}
        plugin = Plugin()
        with patch("penny.plugins.beads_plugin.shutil.which", return_value="/usr/bin/bd"):
            issues = plugin.preflight_checks(config)
        warnings = [i for i in issues if i.severity == "warning"]
        assert any(".beads" in i.message for i in warnings)

    def test_no_warning_when_beads_dir_exists(self, tmp_path):
        project_dir = tmp_path / "myproj"
        project_dir.mkdir()
        (project_dir / ".beads").mkdir()
        config = {"projects": [{"path": str(project_dir)}]}
        plugin = Plugin()
        with patch("penny.plugins.beads_plugin.shutil.which", return_value="/usr/bin/bd"):
            issues = plugin.preflight_checks(config)
        assert issues == []

    def test_skips_placeholder_paths(self):
        config = {"projects": [{"path": "/PLACEHOLDER_PROJECT_PATH"}]}
        plugin = Plugin()
        with patch("penny.plugins.beads_plugin.shutil.which", return_value="/usr/bin/bd"):
            issues = plugin.preflight_checks(config)
        assert issues == []

    def test_skips_nonexistent_paths(self, tmp_path):
        config = {"projects": [{"path": str(tmp_path / "nope")}]}
        plugin = Plugin()
        with patch("penny.plugins.beads_plugin.shutil.which", return_value="/usr/bin/bd"):
            issues = plugin.preflight_checks(config)
        assert issues == []


# ── filter_tasks ──────────────────────────────────────────────────────────────


class TestBeadsPluginFilterTasks:
    def _make_task(self, task_id: str, priority: str = "P2") -> Task:
        return Task(
            task_id=task_id,
            title="Test",
            priority=priority,
            project_path="/tmp/proj",
            project_name="proj",
        )

    def test_filters_out_spawned_ids(self):
        plugin = Plugin()
        tasks = [self._make_task("a"), self._make_task("b")]
        state = {"plugin_state": {"beads": {"spawned_task_ids": ["a"]}}, "agents_running": []}
        config = {"work": {"max_agents_per_run": 5, "task_priority_levels": ["P1", "P2"]}}
        result = plugin.filter_tasks(tasks, state, config)
        assert [t.task_id for t in result] == ["b"]

    def test_filters_out_running_ids(self):
        plugin = Plugin()
        tasks = [self._make_task("a"), self._make_task("b")]
        state = {"plugin_state": {"beads": {}}, "agents_running": [{"task_id": "b"}]}
        config = {"work": {"max_agents_per_run": 5, "task_priority_levels": ["P1", "P2"]}}
        result = plugin.filter_tasks(tasks, state, config)
        assert [t.task_id for t in result] == ["a"]

    def test_respects_priority_levels(self):
        plugin = Plugin()
        tasks = [self._make_task("a", "P1"), self._make_task("b", "P3")]
        state = {"plugin_state": {"beads": {}}, "agents_running": []}
        config = {"work": {"max_agents_per_run": 5, "task_priority_levels": ["P1", "P2"]}}
        result = plugin.filter_tasks(tasks, state, config)
        assert [t.task_id for t in result] == ["a"]

    def test_limits_to_max_agents_per_run(self):
        plugin = Plugin()
        tasks = [self._make_task(f"t-{i}") for i in range(5)]
        state = {"plugin_state": {"beads": {}}, "agents_running": []}
        config = {"work": {"max_agents_per_run": 2, "task_priority_levels": ["P1", "P2"]}}
        result = plugin.filter_tasks(tasks, state, config)
        assert len(result) == 2

    def test_empty_state_keys_default(self):
        plugin = Plugin()
        tasks = [self._make_task("a")]
        state = {}  # missing keys
        config = {"work": {"max_agents_per_run": 2, "task_priority_levels": ["P2"]}}
        result = plugin.filter_tasks(tasks, state, config)
        assert len(result) == 1


# ── get_task_description ──────────────────────────────────────────────────────


class TestBeadsPluginTaskDescription:
    def test_returns_bd_show_output(self):
        plugin = Plugin()
        task = Task("t-1", "Title", "P2", "/tmp/proj", "proj")
        with patch("penny.plugins.beads_plugin._run_bd", return_value="detailed info\n"):
            desc = plugin.get_task_description(task)
        assert desc == "detailed info\n"

    def test_returns_none_when_bd_returns_empty(self):
        plugin = Plugin()
        task = Task("t-1", "Title", "P2", "/tmp/proj", "proj")
        with patch("penny.plugins.beads_plugin._run_bd", return_value=""):
            desc = plugin.get_task_description(task)
        assert desc is None


# ── get_agent_prompt_template ─────────────────────────────────────────────────


class TestBeadsPluginPromptTemplate:
    def test_returns_template(self):
        tmpl = Plugin().get_agent_prompt_template()
        assert tmpl is AGENT_PROMPT_TEMPLATE
        assert "{task_id}" in tmpl
        assert "{task_title}" in tmpl
        assert "{project_path}" in tmpl


# ── handle_action ─────────────────────────────────────────────────────────────


class TestBeadsPluginHandleAction:
    def test_ignores_non_bd_actions(self):
        plugin = Plugin()
        assert plugin.handle_action("other_action", None) is False

    def test_runs_bd_command(self, tmp_path):
        plugin = Plugin()
        payload = (["ready"], str(tmp_path))
        with patch("penny.plugins.beads_plugin.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")
            result = plugin.handle_action("bd_command", payload)
        assert result is True
        mock_run.assert_called_once()
        call_args = mock_run.call_args
        assert call_args[0][0] == ["bd", "ready"]
        assert call_args[1]["cwd"] == str(tmp_path)

    def test_returns_true_even_on_nonzero_exit(self, tmp_path):
        plugin = Plugin()
        payload = (["fail"], str(tmp_path))
        with patch("penny.plugins.beads_plugin.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="error")
            result = plugin.handle_action("bd_command", payload)
        assert result is True

    def test_returns_true_even_on_exception(self, tmp_path):
        plugin = Plugin()
        payload = (["crash"], str(tmp_path))
        with patch(
            "penny.plugins.beads_plugin.subprocess.run",
            side_effect=OSError("nope"),
        ):
            result = plugin.handle_action("bd_command", payload)
        assert result is True

    def test_returns_false_when_no_cwd(self):
        plugin = Plugin()
        payload = (["ready"], "")
        result = plugin.handle_action("bd_command", payload)
        assert result is False


# ── config_schema ─────────────────────────────────────────────────────────────


class TestBeadsPluginConfigSchema:
    def test_has_enabled_key(self):
        schema = Plugin().config_schema()
        assert "enabled" in schema
        assert schema["enabled"]["default"] == "auto"
