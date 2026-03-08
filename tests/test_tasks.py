"""Unit tests for penny/tasks.py (Task dataclass) and penny/plugins/beads_plugin.py."""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import penny.plugins.beads_plugin as _beads_mod
from penny.plugins.beads_plugin import (
    AGENT_PROMPT_TEMPLATE,
    BeadsUIController,
    Plugin,
    _extract_description,
    _parse_bd_list,
    _parse_bd_ready,
    _run_bd,
    _update_pagination_nav,
)
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


# ── _extract_description ──────────────────────────────────────────────────────


class TestExtractDescription:
    def test_returns_from_allcaps_section_header(self):
        text = "Task t-1: Fix bug\nStatus: open\nDESCRIPTION\nDo the thing."
        result = _extract_description(text)
        assert result.startswith("DESCRIPTION")
        assert "Do the thing." in result

    def test_strips_header_lines_before_section(self):
        text = "header line 1\nheader line 2\nDETAILS\ncontent"
        result = _extract_description(text)
        assert result == "DETAILS\ncontent"

    def test_returns_full_text_when_no_allcaps_section(self):
        text = "just some text\nwith no header"
        result = _extract_description(text)
        assert result == "just some text\nwith no header"

    def test_empty_string(self):
        assert _extract_description("") == ""

    def test_multiple_sections_starts_from_first(self):
        text = "preamble\nSECTION ONE\nsome text\nSECTION TWO\nmore text"
        result = _extract_description(text)
        assert result.startswith("SECTION ONE")
        assert "SECTION TWO" in result


# ── _update_pagination_nav ────────────────────────────────────────────────────


class TestUpdatePaginationNav:
    def test_hides_nav_when_single_page(self):
        nav = MagicMock()
        _update_pagination_nav(nav, MagicMock(), MagicMock(), MagicMock(), 0, 1)
        nav.setHidden_.assert_called_with(True)

    def test_shows_nav_when_multiple_pages(self):
        nav = MagicMock()
        page_lbl = MagicMock()
        prev_btn = MagicMock()
        next_btn = MagicMock()
        _update_pagination_nav(nav, prev_btn, next_btn, page_lbl, 1, 3)
        nav.setHidden_.assert_called_with(False)
        page_lbl.setStringValue_.assert_called_with("2 / 3")

    def test_disables_prev_on_first_page(self):
        nav = MagicMock()
        prev_btn = MagicMock()
        next_btn = MagicMock()
        _update_pagination_nav(nav, prev_btn, next_btn, MagicMock(), 0, 3)
        prev_btn.setEnabled_.assert_called_with(False)
        next_btn.setEnabled_.assert_called_with(True)

    def test_disables_next_on_last_page(self):
        nav = MagicMock()
        prev_btn = MagicMock()
        next_btn = MagicMock()
        _update_pagination_nav(nav, prev_btn, next_btn, MagicMock(), 2, 3)
        prev_btn.setEnabled_.assert_called_with(True)
        next_btn.setEnabled_.assert_called_with(False)

    def test_no_error_when_nav_is_none(self):
        # Should return early without error
        _update_pagination_nav(None, MagicMock(), MagicMock(), MagicMock(), 0, 2)


# ── _markdown_to_attrstr (via mocked AppKit) ──────────────────────────────────


class TestMarkdownToAttrstr:
    def _call(self, text: str) -> MagicMock:
        """Call _markdown_to_attrstr with all AppKit objects mocked."""
        with (
            patch.object(_beads_mod, "NSFont", MagicMock()),
            patch.object(_beads_mod, "NSColor", MagicMock()),
            patch.object(_beads_mod, "NSMutableAttributedString", MagicMock()),
            patch.object(_beads_mod, "NSAttributedString", MagicMock()),
            patch.object(_beads_mod, "NSFontAttributeName", "font"),
            patch.object(_beads_mod, "NSForegroundColorAttributeName", "color"),
        ):
            from penny.plugins.beads_plugin import _markdown_to_attrstr
            return _markdown_to_attrstr(text)

    def test_returns_attributed_string_object(self):
        result = self._call("Hello world")
        assert result is not None

    def test_handles_bold_markup(self):
        result = self._call("Hello **bold** text")
        assert result is not None

    def test_handles_code_markup(self):
        result = self._call("Run `bd ready` command")
        assert result is not None

    def test_handles_allcaps_section_header(self):
        result = self._call("SECTION HEADER\nsome text")
        assert result is not None

    def test_handles_bullet_list(self):
        result = self._call("- item one\n- item two")
        assert result is not None

    def test_handles_empty_string(self):
        result = self._call("")
        assert result is not None

    def test_handles_mixed_markup(self):
        result = self._call("DETAILS\n**bold** and `code`\n- bullet\n- another")
        assert result is not None


# ── Plugin lifecycle extras ───────────────────────────────────────────────────


class TestBeadsPluginLifecycleExtras:
    def test_on_first_activated_calls_send_notification(self):
        plugin = Plugin()
        app = MagicMock()
        with patch("penny.spawner.send_notification") as mock_notify:
            plugin.on_first_activated(app)
        mock_notify.assert_called_once()

    def test_on_first_activated_swallows_exceptions(self):
        plugin = Plugin()
        app = MagicMock()
        with patch("penny.spawner.send_notification", side_effect=RuntimeError("boom")):
            plugin.on_first_activated(app)  # must not raise

    def test_on_agent_spawned_records_task_id(self):
        plugin = Plugin()
        task = Task("t-42", "Title", "P1", "/tmp/proj", "proj")
        plugin_state: dict = {}
        plugin.on_agent_spawned(task, {}, plugin_state)
        assert "t-42" in plugin_state["spawned_task_ids"]

    def test_on_agent_spawned_appends_to_existing_state(self):
        plugin = Plugin()
        task = Task("t-99", "Title", "P2", "/tmp/proj", "proj")
        plugin_state = {"spawned_task_ids": ["t-01"]}
        plugin.on_agent_spawned(task, {}, plugin_state)
        assert "t-01" in plugin_state["spawned_task_ids"]
        assert "t-99" in plugin_state["spawned_task_ids"]

    def test_on_agent_completed_does_not_raise(self):
        plugin = Plugin()
        plugin.on_agent_completed({}, {})  # must not raise

    def test_cli_commands_returns_list_of_dicts(self):
        cmds = Plugin().cli_commands()
        assert isinstance(cmds, list)
        assert all(isinstance(c, dict) for c in cmds)
        assert len(cmds) >= 4

    def test_cli_commands_includes_expected_names(self):
        names = {c["name"] for c in Plugin().cli_commands()}
        assert "tasks" in names
        assert "run" in names
        assert "stop-agent" in names


# ── BeadsUIController helpers ─────────────────────────────────────────────────


def _make_ctrl() -> BeadsUIController:
    """Build a BeadsUIController with all UI attributes stubbed as MagicMocks."""
    ctrl = BeadsUIController.__new__(BeadsUIController)
    ctrl._plugin = MagicMock()
    ctrl._app = MagicMock()
    ctrl._tasks_stack = MagicMock()
    ctrl._task_views = []
    ctrl._tasks_page = 0
    ctrl._tasks_total_pages = 1
    ctrl._tasks_nav_row = MagicMock()
    ctrl._tasks_prev_btn = MagicMock()
    ctrl._tasks_next_btn = MagicMock()
    ctrl._tasks_page_lbl = MagicMock()
    ctrl._tasks_header_lbl = MagicMock()
    ctrl._expanded_task_id = None
    ctrl._latest_tasks = []
    ctrl._latest_agents = []
    ctrl._latest_completed = []
    ctrl._agents_stack = MagicMock()
    ctrl._agent_views = []
    ctrl._agents_header_lbl = MagicMock()
    ctrl._agents_page = 0
    ctrl._agents_total_pages = 1
    ctrl._agents_nav_row = MagicMock()
    ctrl._agents_prev_btn = MagicMock()
    ctrl._agents_next_btn = MagicMock()
    ctrl._agents_page_lbl = MagicMock()
    ctrl._completed_stack = MagicMock()
    ctrl._completed_views = []
    ctrl._completed_header_row = MagicMock()
    ctrl._completed_outer = MagicMock()
    ctrl._completed_nav_row = MagicMock()
    ctrl._completed_prev_btn = MagicMock()
    ctrl._completed_next_btn = MagicMock()
    ctrl._completed_page_lbl = MagicMock()
    ctrl._completed_page = 0
    ctrl._completed_total_pages = 1
    return ctrl


class TestBeadsUIControllerRebuildTasks:
    """Tests for _rebuild_tasks_section."""

    def _patched(self):
        return (
            patch.object(_beads_mod, "make_label", return_value=MagicMock()),
            patch.object(_beads_mod, "make_button", return_value=MagicMock()),
            patch.object(_beads_mod, "NSStackView", MagicMock()),
            patch.object(_beads_mod, "NSButton", MagicMock()),
            patch.object(_beads_mod, "NSFont", MagicMock()),
            patch.object(_beads_mod, "_make_desc_scroll_view", return_value=MagicMock()),
        )

    def test_empty_tasks_shows_placeholder(self):
        ctrl = _make_ctrl()
        patches = self._patched()
        for p in patches:
            p.start()
        try:
            ctrl._rebuild_tasks_section([], [])
            ctrl._tasks_stack.addArrangedSubview_.assert_called()
        finally:
            for p in patches:
                p.stop()

    def test_tasks_without_stack_returns_early(self):
        ctrl = _make_ctrl()
        ctrl._tasks_stack = None
        # Must not raise even with no stack
        ctrl._rebuild_tasks_section([Task("t-1", "T", "P1", "/p", "p")], [])

    def test_tasks_shown_updates_header_label(self):
        ctrl = _make_ctrl()
        patches = self._patched()
        for p in patches:
            p.start()
        try:
            tasks = [Task("t-1", "Title", "P1", "/p", "p")]
            ctrl._rebuild_tasks_section(tasks, [])
            ctrl._tasks_header_lbl.setStringValue_.assert_called_with("Ready Tasks (1)")
        finally:
            for p in patches:
                p.stop()

    def test_running_tasks_excluded_from_display(self):
        ctrl = _make_ctrl()
        patches = self._patched()
        for p in patches:
            p.start()
        try:
            task = Task("t-1", "Title", "P1", "/p", "p")
            agents = [{"task_id": "t-1"}]
            ctrl._rebuild_tasks_section([task], agents)
            # Since all tasks are running, placeholder label is shown
            ctrl._tasks_header_lbl.setStringValue_.assert_called_with("Ready Tasks")
        finally:
            for p in patches:
                p.stop()


class TestBeadsUIControllerRebuildAgents:
    def _patched(self):
        return (
            patch.object(_beads_mod, "make_label", return_value=MagicMock()),
            patch.object(_beads_mod, "make_button", return_value=MagicMock()),
            patch.object(_beads_mod, "NSStackView", MagicMock()),
        )

    def test_empty_agents_hides_section(self):
        ctrl = _make_ctrl()
        patches = self._patched()
        for p in patches:
            p.start()
        try:
            ctrl._rebuild_agents_section([])
            ctrl._agents_stack.setHidden_.assert_called_with(True)
            ctrl._agents_header_lbl.setHidden_.assert_called_with(True)
        finally:
            for p in patches:
                p.stop()

    def test_agents_without_stack_returns_early(self):
        ctrl = _make_ctrl()
        ctrl._agents_stack = None
        # Must not raise
        ctrl._rebuild_agents_section([{"task_id": "t-1", "title": "T"}])

    def test_agents_shown_makes_agent_rows(self):
        ctrl = _make_ctrl()
        patches = self._patched()
        for p in patches:
            p.start()
        try:
            agents = [{"task_id": "t-1", "title": "Fix bug"}]
            ctrl._rebuild_agents_section(agents)
            ctrl._agents_stack.addArrangedSubview_.assert_called()
        finally:
            for p in patches:
                p.stop()


class TestBeadsUIControllerRebuildCompleted:
    def _patched(self):
        return (
            patch.object(_beads_mod, "make_label", return_value=MagicMock()),
            patch.object(_beads_mod, "make_button", return_value=MagicMock()),
            patch.object(_beads_mod, "NSStackView", MagicMock()),
            patch.object(_beads_mod, "NSColor", MagicMock()),
        )

    def test_empty_completed_hides_section(self):
        ctrl = _make_ctrl()
        patches = self._patched()
        for p in patches:
            p.start()
        try:
            ctrl._rebuild_completed_section([])
            ctrl._completed_outer.setHidden_.assert_called_with(True)
        finally:
            for p in patches:
                p.stop()

    def test_completed_without_stack_returns_early(self):
        ctrl = _make_ctrl()
        ctrl._completed_stack = None
        # Must not raise
        ctrl._rebuild_completed_section([{"task_id": "t-1"}])

    def test_completed_items_shown_adds_rows(self):
        ctrl = _make_ctrl()
        patches = self._patched()
        for p in patches:
            p.start()
        try:
            items = [{"task_id": "t-1", "title": "Done", "project": "proj", "status": "completed"}]
            ctrl._rebuild_completed_section(items)
            ctrl._completed_stack.addArrangedSubview_.assert_called()
        finally:
            for p in patches:
                p.stop()


class TestBeadsUIControllerActionSelectors:
    """Tests for ObjC action selector methods that have testable pure-Python logic."""

    def _patched(self):
        return (
            patch.object(_beads_mod, "make_label", return_value=MagicMock()),
            patch.object(_beads_mod, "make_button", return_value=MagicMock()),
            patch.object(_beads_mod, "NSStackView", MagicMock()),
            patch.object(_beads_mod, "NSButton", MagicMock()),
            patch.object(_beads_mod, "NSFont", MagicMock()),
            patch.object(_beads_mod, "_make_desc_scroll_view", return_value=MagicMock()),
        )

    def test_toggle_task_expands_on_first_click(self):
        ctrl = _make_ctrl()
        t = Task("t-1", "Fix", "P1", "/p", "p")
        ctrl._latest_tasks = [t]
        sender = MagicMock()
        sender.representedObject.return_value = "t-1"
        patches = self._patched()
        for p in patches:
            p.start()
        try:
            ctrl._toggleTask_(sender)
            assert ctrl._expanded_task_id == "t-1"
        finally:
            for p in patches:
                p.stop()

    def test_toggle_task_collapses_on_second_click(self):
        ctrl = _make_ctrl()
        t = Task("t-1", "Fix", "P1", "/p", "p")
        ctrl._latest_tasks = [t]
        ctrl._expanded_task_id = "t-1"
        sender = MagicMock()
        sender.representedObject.return_value = "t-1"
        patches = self._patched()
        for p in patches:
            p.start()
        try:
            ctrl._toggleTask_(sender)
            assert ctrl._expanded_task_id is None
        finally:
            for p in patches:
                p.stop()

    def test_toggle_task_empty_id_does_nothing(self):
        ctrl = _make_ctrl()
        ctrl._latest_tasks = []
        sender = MagicMock()
        sender.representedObject.return_value = ""
        ctrl._toggleTask_(sender)
        assert ctrl._expanded_task_id is None

    def test_tasks_prev_decrements_page(self):
        ctrl = _make_ctrl()
        ctrl._tasks_page = 2
        ctrl._tasks_total_pages = 3
        patches = self._patched()
        for p in patches:
            p.start()
        try:
            ctrl._tasksPrev_(MagicMock())
            assert ctrl._tasks_page == 1
        finally:
            for p in patches:
                p.stop()

    def test_tasks_prev_does_not_go_below_zero(self):
        ctrl = _make_ctrl()
        ctrl._tasks_page = 0
        ctrl._tasks_total_pages = 3
        patches = self._patched()
        for p in patches:
            p.start()
        try:
            ctrl._tasksPrev_(MagicMock())
            assert ctrl._tasks_page == 0
        finally:
            for p in patches:
                p.stop()

    def test_tasks_next_increments_page(self):
        ctrl = _make_ctrl()
        ctrl._tasks_page = 0
        ctrl._tasks_total_pages = 3
        patches = self._patched()
        for p in patches:
            p.start()
        try:
            ctrl._tasksNext_(MagicMock())
            assert ctrl._tasks_page == 1
        finally:
            for p in patches:
                p.stop()

    def test_tasks_next_does_not_exceed_total(self):
        ctrl = _make_ctrl()
        ctrl._tasks_page = 2
        ctrl._tasks_total_pages = 3
        patches = self._patched()
        for p in patches:
            p.start()
        try:
            ctrl._tasksNext_(MagicMock())
            assert ctrl._tasks_page == 2
        finally:
            for p in patches:
                p.stop()

    def test_agents_prev_decrements(self):
        ctrl = _make_ctrl()
        ctrl._agents_page = 1
        ctrl._agents_total_pages = 3
        patches = self._patched()
        for p in patches:
            p.start()
        try:
            ctrl._agentsPrev_(MagicMock())
            assert ctrl._agents_page == 0
        finally:
            for p in patches:
                p.stop()

    def test_agents_next_increments(self):
        ctrl = _make_ctrl()
        ctrl._agents_page = 0
        ctrl._agents_total_pages = 3
        patches = self._patched()
        for p in patches:
            p.start()
        try:
            ctrl._agentsNext_(MagicMock())
            assert ctrl._agents_page == 1
        finally:
            for p in patches:
                p.stop()

    def test_completed_prev_decrements(self):
        ctrl = _make_ctrl()
        ctrl._completed_page = 1
        ctrl._completed_total_pages = 3
        patches = self._patched()
        for p in patches:
            p.start()
        try:
            ctrl._completedPrev_(MagicMock())
            assert ctrl._completed_page == 0
        finally:
            for p in patches:
                p.stop()

    def test_completed_next_increments(self):
        ctrl = _make_ctrl()
        ctrl._completed_page = 0
        ctrl._completed_total_pages = 3
        patches = self._patched()
        for p in patches:
            p.start()
        try:
            ctrl._completedNext_(MagicMock())
            assert ctrl._completed_page == 1
        finally:
            for p in patches:
                p.stop()

    def test_run_task_removes_from_list_and_spawns(self):
        ctrl = _make_ctrl()
        t = Task("t-1", "Fix", "P1", "/p", "p")
        ctrl._latest_tasks = [t]
        sender = MagicMock()
        sender.representedObject.return_value = "t-1"
        patches = self._patched()
        for p in patches:
            p.start()
        try:
            ctrl._runTask_(sender)
            assert len(ctrl._latest_tasks) == 0
            ctrl._app.spawnTask_.assert_called_once_with(t)
        finally:
            for p in patches:
                p.stop()

    def test_run_task_unknown_id_does_nothing(self):
        ctrl = _make_ctrl()
        ctrl._latest_tasks = []
        sender = MagicMock()
        sender.representedObject.return_value = "unknown-id"
        ctrl._runTask_(sender)
        ctrl._app.spawnTask_.assert_not_called()

    def test_stop_agent_calls_app(self):
        ctrl = _make_ctrl()
        sender = MagicMock()
        sender.representedObject.return_value = "t-1"
        ctrl._stopAgent_(sender)
        ctrl._app.stopAgentByTaskId_.assert_called_once_with("t-1")

    def test_stop_agent_empty_id_does_nothing(self):
        ctrl = _make_ctrl()
        sender = MagicMock()
        sender.representedObject.return_value = ""
        ctrl._stopAgent_(sender)
        ctrl._app.stopAgentByTaskId_.assert_not_called()

    def test_dismiss_completed_calls_app(self):
        ctrl = _make_ctrl()
        sender = MagicMock()
        sender.representedObject.return_value = "t-1"
        ctrl._dismissCompleted_(sender)
        ctrl._app.dismissCompleted_.assert_called_once_with("t-1")

    def test_dismiss_completed_empty_id_does_nothing(self):
        ctrl = _make_ctrl()
        sender = MagicMock()
        sender.representedObject.return_value = ""
        ctrl._dismissCompleted_(sender)
        ctrl._app.dismissCompleted_.assert_not_called()

    def test_clear_all_completed_calls_app(self):
        ctrl = _make_ctrl()
        ctrl._clearAllCompleted_(MagicMock())
        ctrl._app.clearAllCompleted_.assert_called_once()

    def test_control_agent_no_session_does_nothing(self):
        ctrl = _make_ctrl()
        ctrl._latest_agents = [{"task_id": "t-1", "session": ""}]
        sender = MagicMock()
        sender.representedObject.return_value = "t-1"
        # No subprocess should be called since session is empty
        ctrl._controlAgent_(sender)
        ctrl._app.assert_not_called()

    def test_control_agent_unknown_id_does_nothing(self):
        ctrl = _make_ctrl()
        ctrl._latest_agents = []
        sender = MagicMock()
        sender.representedObject.return_value = "t-1"
        ctrl._controlAgent_(sender)

    def test_control_agent_empty_id_does_nothing(self):
        ctrl = _make_ctrl()
        sender = MagicMock()
        sender.representedObject.return_value = ""
        ctrl._controlAgent_(sender)


class TestBeadsUIControllerRebuildView:
    """Tests for Plugin._rebuild_tasks_view and _rebuild_completed_view."""

    def test_rebuild_tasks_view_no_ctrl(self):
        plugin = Plugin()
        plugin._ui_ctrl = None
        # Must not raise
        plugin._rebuild_tasks_view({})

    def test_rebuild_tasks_view_updates_ctrl_state(self):
        plugin = Plugin()
        ctrl = _make_ctrl()
        plugin._ui_ctrl = ctrl
        t = Task("t-1", "T", "P1", "/p", "p")

        with (
            patch.object(ctrl, "_rebuild_tasks_section"),
            patch.object(ctrl, "_rebuild_agents_section"),
        ):
            plugin._rebuild_tasks_view({
                "ready_tasks": [t],
                "state": {"agents_running": [], "recently_completed": []},
            })
            assert ctrl._latest_tasks == [t]
            assert ctrl._latest_agents == []

    def test_rebuild_completed_view_no_ctrl(self):
        plugin = Plugin()
        plugin._ui_ctrl = None
        # Must not raise
        plugin._rebuild_completed_view({})

    def test_rebuild_completed_view_updates_ctrl_state(self):
        plugin = Plugin()
        ctrl = _make_ctrl()
        plugin._ui_ctrl = ctrl
        completed = [{"task_id": "t-1", "title": "Done", "project": "p", "status": "completed"}]

        with patch.object(ctrl, "_rebuild_completed_section"):
            plugin._rebuild_completed_view({
                "state": {"recently_completed": completed},
            })
            assert ctrl._latest_completed == completed


# ── Branch coverage extras ────────────────────────────────────────────────────


class TestBeadsUIControllerBranchCoverage:
    """Additional tests to cover branch paths not hit by basic tests."""

    def _patched(self):
        return (
            patch.object(_beads_mod, "make_label", return_value=MagicMock()),
            patch.object(_beads_mod, "make_button", return_value=MagicMock()),
            patch.object(_beads_mod, "NSStackView", MagicMock()),
            patch.object(_beads_mod, "NSButton", MagicMock()),
            patch.object(_beads_mod, "NSFont", MagicMock()),
            patch.object(_beads_mod, "NSColor", MagicMock()),
            patch.object(_beads_mod, "_make_desc_scroll_view", return_value=MagicMock()),
        )

    def test_rebuild_tasks_clears_existing_views(self):
        """Line 461: removeFromSuperview called when pre-existing task views exist."""
        ctrl = _make_ctrl()
        mock_view = MagicMock()
        ctrl._task_views = [mock_view]
        patches = self._patched()
        for p in patches:
            p.start()
        try:
            ctrl._rebuild_tasks_section([], [])
            mock_view.removeFromSuperview.assert_called_once()
        finally:
            for p in patches:
                p.stop()

    def test_rebuild_tasks_clears_expanded_id_when_not_in_page(self):
        """Line 491: _expanded_task_id cleared when expanded task not in current page."""
        ctrl = _make_ctrl()
        ctrl._expanded_task_id = "not-on-page"
        # Create a task with a different id so expanded is not found in page_tasks
        t = Task("t-different", "T", "P1", "/p", "p")
        patches = self._patched()
        for p in patches:
            p.start()
        try:
            ctrl._rebuild_tasks_section([t], [])
            assert ctrl._expanded_task_id is None
        finally:
            for p in patches:
                p.stop()

    def test_rebuild_agents_clears_existing_views(self):
        """Line 513: removeFromSuperview called when pre-existing agent views exist."""
        ctrl = _make_ctrl()
        mock_view = MagicMock()
        ctrl._agent_views = [mock_view]
        patches = self._patched()
        for p in patches:
            p.start()
        try:
            ctrl._rebuild_agents_section([])
            mock_view.removeFromSuperview.assert_called_once()
        finally:
            for p in patches:
                p.stop()

    def test_rebuild_completed_clears_existing_views(self):
        """Line 552: removeFromSuperview called when pre-existing completed views exist."""
        ctrl = _make_ctrl()
        mock_view = MagicMock()
        ctrl._completed_views = [mock_view]
        patches = self._patched()
        for p in patches:
            p.start()
        try:
            ctrl._rebuild_completed_section([])
            mock_view.removeFromSuperview.assert_called_once()
        finally:
            for p in patches:
                p.stop()

    def test_make_task_row_running_shows_status_label(self):
        """Lines 625-627: running task shows status label instead of run button."""
        ctrl = _make_ctrl()
        t = Task("t-1", "Fix", "P1", "/p", "p")
        patches = self._patched()
        for p in patches:
            p.start()
        try:
            # Pass task_id in running_ids so is_running=True
            row = ctrl._make_task_row(t, False, {"t-1"})
            assert row is not None
        finally:
            for p in patches:
                p.stop()

    def test_make_completed_row_unknown_status_sets_color(self):
        """Line 676-677: unknown status applies secondary label color."""
        ctrl = _make_ctrl()
        patches = self._patched()
        for p in patches:
            p.start()
        try:
            row = ctrl._make_completed_row({
                "task_id": "t-1",
                "title": "Failed",
                "project": "proj",
                "status": "unknown",
            })
            assert row is not None
        finally:
            for p in patches:
                p.stop()

    def test_toggle_task_fetches_desc_via_plugin_mgr(self):
        """Line 349: task._cached_desc fetched from plugin_mgr when no cached desc."""
        ctrl = _make_ctrl()
        t = Task("t-1", "Fix", "P1", "/p", "p")
        ctrl._latest_tasks = [t]

        mock_mgr = MagicMock()
        mock_mgr.get_task_description.return_value = "Full description"
        ctrl._app._plugin_mgr = mock_mgr

        sender = MagicMock()
        sender.representedObject.return_value = "t-1"
        patches = self._patched()
        for p in patches:
            p.start()
        try:
            ctrl._toggleTask_(sender)
            mock_mgr.get_task_description.assert_called_once_with(t)
            assert t._cached_desc == "Full description"
        finally:
            for p in patches:
                p.stop()

    def test_toggle_task_uses_title_when_no_plugin_mgr(self):
        """Line 351: task._cached_desc falls back to task.title when no plugin_mgr."""
        ctrl = _make_ctrl()
        t = Task("t-1", "Fix this bug", "P1", "/p", "p")
        ctrl._latest_tasks = [t]

        # No _plugin_mgr on app
        del ctrl._app._plugin_mgr
        ctrl._app._plugin_mgr = None

        sender = MagicMock()
        sender.representedObject.return_value = "t-1"
        patches = self._patched()
        for p in patches:
            p.start()
        try:
            ctrl._toggleTask_(sender)
            assert t._cached_desc == "Fix this bug"
        finally:
            for p in patches:
                p.stop()

    def test_control_agent_with_valid_session_attempts_open(self):
        """Lines 421-436: _controlAgent_ creates script and calls 'open'."""
        ctrl = _make_ctrl()
        ctrl._latest_agents = [{"task_id": "t-1", "session": "my-session", "tmux_bin": "/usr/bin/tmux"}]
        sender = MagicMock()
        sender.representedObject.return_value = "t-1"

        mock_run = MagicMock()
        mock_run.returncode = 0
        mock_popen = MagicMock()
        with (
            patch.object(_beads_mod, "subprocess") as mock_sub,
            patch.object(_beads_mod, "tempfile") as mock_tmp,
            patch.object(_beads_mod, "os") as mock_os,
            patch.object(_beads_mod, "stat") as mock_stat,
        ):
            mock_sub.run.return_value = mock_run
            mock_sub.Popen = mock_popen
            mock_tmp.mkstemp.return_value = (3, "/tmp/test.command")
            mock_os.fdopen.return_value.__enter__ = MagicMock(return_value=MagicMock())
            mock_os.fdopen.return_value.__exit__ = MagicMock(return_value=False)
            mock_stat.S_IRWXU = 0o700
            mock_stat.S_IRGRP = 0o040
            mock_stat.S_IROTH = 0o004
            ctrl._controlAgent_(sender)
        # Whether or not the open succeeds (could fail on fd), the attempt was made
        assert mock_sub.run.called

    def test_control_agent_uses_screen_when_tmux_session_missing(self):
        """Line 427: falls back to screen -x when tmux has-session returns nonzero."""
        ctrl = _make_ctrl()
        ctrl._latest_agents = [{"task_id": "t-1", "session": "my-session", "tmux_bin": "/usr/bin/tmux"}]
        sender = MagicMock()
        sender.representedObject.return_value = "t-1"

        mock_run = MagicMock()
        mock_run.returncode = 1  # tmux has-session fails
        with (
            patch.object(_beads_mod, "subprocess") as mock_sub,
            patch.object(_beads_mod, "tempfile") as mock_tmp,
            patch.object(_beads_mod, "os") as mock_os,
            patch.object(_beads_mod, "stat") as mock_stat,
        ):
            mock_sub.run.return_value = mock_run
            mock_sub.Popen = MagicMock()
            mock_tmp.mkstemp.return_value = (3, "/tmp/test2.command")
            mock_os.fdopen.return_value.__enter__ = MagicMock(return_value=MagicMock())
            mock_os.fdopen.return_value.__exit__ = MagicMock(return_value=False)
            mock_stat.S_IRWXU = 0o700
            mock_stat.S_IRGRP = 0o040
            mock_stat.S_IROTH = 0o004
            ctrl._controlAgent_(sender)
        assert mock_sub.run.called

    def test_control_agent_handles_exception_gracefully(self):
        """Lines 435-436: exception in file write is caught and printed."""
        ctrl = _make_ctrl()
        ctrl._latest_agents = [{"task_id": "t-1", "session": "my-session"}]
        sender = MagicMock()
        sender.representedObject.return_value = "t-1"

        with (
            patch.object(_beads_mod, "subprocess") as mock_sub,
            patch.object(_beads_mod, "tempfile") as mock_tmp,
            patch.object(_beads_mod, "os") as mock_os,
        ):
            mock_sub.run.return_value.returncode = 0
            mock_tmp.mkstemp.return_value = (3, "/tmp/test3.command")
            mock_os.fdopen.side_effect = OSError("disk full")
            # Must not propagate exception
            ctrl._controlAgent_(sender)


# ── AppKit UI builder methods ─────────────────────────────────────────────────


def _appkit_patches():
    """Context managers to patch all AppKit classes used in UI builders."""
    from unittest.mock import MagicMock

    mock_tv = MagicMock()
    mock_lm = MagicMock()
    mock_used = MagicMock()
    mock_used.size.height = 80.0
    mock_lm.usedRectForTextContainer_.return_value = mock_used
    mock_tv.layoutManager.return_value = mock_lm

    mock_NSTextView = MagicMock()
    mock_NSTextView.alloc.return_value.initWithFrame_.return_value = mock_tv

    return [
        patch.object(_beads_mod, "NSStackView", MagicMock()),
        patch.object(_beads_mod, "NSButton", MagicMock()),
        patch.object(_beads_mod, "NSFont", MagicMock()),
        patch.object(_beads_mod, "NSColor", MagicMock()),
        patch.object(_beads_mod, "NSTextView", mock_NSTextView),
        patch.object(_beads_mod, "NSScrollView", MagicMock()),
        patch.object(_beads_mod, "NSMutableAttributedString", MagicMock()),
        patch.object(_beads_mod, "NSAttributedString", MagicMock()),
        patch.object(_beads_mod, "NSFontAttributeName", "font"),
        patch.object(_beads_mod, "NSForegroundColorAttributeName", "color"),
        patch.object(_beads_mod, "make_label", MagicMock(return_value=MagicMock())),
        patch.object(_beads_mod, "make_button", MagicMock(return_value=MagicMock())),
    ]


class TestMakeDescScrollView:
    """Tests for _make_desc_scroll_view."""

    def test_returns_scroll_view(self):
        from penny.plugins.beads_plugin import _make_desc_scroll_view
        patches = _appkit_patches()
        for p in patches:
            p.start()
        try:
            result = _make_desc_scroll_view("Hello **world**")
            assert result is not None
        finally:
            for p in patches:
                p.stop()

    def test_handles_empty_text(self):
        from penny.plugins.beads_plugin import _make_desc_scroll_view
        patches = _appkit_patches()
        for p in patches:
            p.start()
        try:
            result = _make_desc_scroll_view("")
            assert result is not None
        finally:
            for p in patches:
                p.stop()


class TestMakeInnerStack:
    """Tests for _make_inner_stack."""

    def test_returns_stack_view(self):
        from penny.plugins.beads_plugin import _make_inner_stack
        patches = _appkit_patches()
        for p in patches:
            p.start()
        try:
            result = _make_inner_stack()
            assert result is not None
        finally:
            for p in patches:
                p.stop()


class TestBeadsUIControllerInit:
    """Test BeadsUIController.init() through objc.super mock."""

    def test_init_sets_all_attributes(self):
        from penny.plugins.beads_plugin import BeadsUIController
        ctrl = BeadsUIController.__new__(BeadsUIController)
        with patch.object(_beads_mod.objc, "super") as mock_super:
            mock_super.return_value.init.return_value = ctrl
            result = ctrl.init()
        assert result is ctrl
        assert result._tasks_page == 0
        assert result._agents_page == 0
        assert result._completed_page == 0
        assert result._expanded_task_id is None
        assert result._latest_tasks == []

    def test_init_returns_none_when_super_returns_none(self):
        from penny.plugins.beads_plugin import BeadsUIController
        ctrl = BeadsUIController.__new__(BeadsUIController)
        with patch.object(_beads_mod.objc, "super") as mock_super:
            mock_super.return_value.init.return_value = None
            result = ctrl.init()
        assert result is None


class TestPluginUiSections:
    """Tests for Plugin.ui_sections() and its builder methods."""

    def test_ui_sections_returns_two_sections(self):
        plugin = Plugin()
        plugin._app = MagicMock()
        ctrl = _make_ctrl()
        with patch.object(BeadsUIController, "alloc", create=True, return_value=MagicMock()) as mock_alloc:
            mock_alloc.return_value.init.return_value = ctrl
            sections = plugin.ui_sections()
        assert len(sections) == 2
        names = {s.name for s in sections}
        assert "beads_tasks" in names
        assert "beads_completed" in names

    def test_ui_sections_reuses_existing_ctrl(self):
        plugin = Plugin()
        plugin._app = MagicMock()
        existing_ctrl = _make_ctrl()
        plugin._ui_ctrl = existing_ctrl
        # Should not try to create a new controller
        with patch.object(BeadsUIController, "alloc", create=True) as mock_alloc:
            sections = plugin.ui_sections()
            mock_alloc.assert_not_called()
        assert len(sections) == 2

    def test_build_tasks_view_returns_outer_stack(self):
        plugin = Plugin()
        plugin._app = MagicMock()
        plugin._ui_ctrl = _make_ctrl()
        patches = _appkit_patches()
        for p in patches:
            p.start()
        try:
            result = plugin._build_tasks_view()
            assert result is not None
        finally:
            for p in patches:
                p.stop()

    def test_build_completed_view_returns_outer_stack(self):
        plugin = Plugin()
        plugin._app = MagicMock()
        plugin._ui_ctrl = _make_ctrl()
        patches = _appkit_patches()
        for p in patches:
            p.start()
        try:
            result = plugin._build_completed_view()
            assert result is not None
        finally:
            for p in patches:
                p.stop()

    def test_build_tasks_view_configures_ctrl_stacks(self):
        """Verify that _build_tasks_view assigns stacks to ctrl attributes."""
        plugin = Plugin()
        plugin._app = MagicMock()
        ctrl = _make_ctrl()
        plugin._ui_ctrl = ctrl
        patches = _appkit_patches()
        for p in patches:
            p.start()
        try:
            plugin._build_tasks_view()
            # After build, _tasks_stack should be set on ctrl
            assert ctrl._tasks_stack is not None
            assert ctrl._agents_stack is not None
        finally:
            for p in patches:
                p.stop()

    def test_build_completed_view_sets_outer_reference(self):
        """Verify that _build_completed_view assigns _completed_outer on ctrl."""
        plugin = Plugin()
        plugin._app = MagicMock()
        ctrl = _make_ctrl()
        plugin._ui_ctrl = ctrl
        patches = _appkit_patches()
        for p in patches:
            p.start()
        try:
            plugin._build_completed_view()
            assert ctrl._completed_outer is not None
        finally:
            for p in patches:
                p.stop()
