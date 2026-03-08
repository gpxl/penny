"""Unit tests for penny/plugin.py — PluginManager, PennyPlugin, UISection."""

from __future__ import annotations

import types as builtin_types
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from penny.plugin import PennyPlugin, PluginManager, UISection
from penny.preflight import PreflightIssue
from penny.tasks import Task

# ── Helpers ───────────────────────────────────────────────────────────────────


class StubPlugin(PennyPlugin):
    """Minimal concrete plugin for testing."""

    def __init__(self, plugin_name: str = "stub", available: bool = True):
        self._name = plugin_name
        self._available = available
        self.activated = False
        self.deactivated = False

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return f"Stub plugin: {self._name}"

    def is_available(self) -> bool:
        return self._available

    def on_activate(self, app: Any) -> None:
        self.activated = True

    def on_deactivate(self) -> None:
        self.deactivated = True

    def on_agent_spawned(self, task: Any, record: dict[str, Any], plugin_state: dict[str, Any]) -> None:
        pass

    def on_agent_completed(self, record: dict[str, Any], plugin_state: dict[str, Any]) -> None:
        pass


class ErrorPlugin(StubPlugin):
    """Plugin that raises on activate/deactivate."""

    def on_activate(self, app: Any) -> None:
        raise RuntimeError("activate boom")

    def on_deactivate(self) -> None:
        raise RuntimeError("deactivate boom")


class TaskPlugin(StubPlugin):
    """Plugin that returns tasks and handles actions."""

    def __init__(self, tasks: list[Task] | None = None, **kwargs: Any):
        super().__init__(**kwargs)
        self._tasks = tasks or []
        self._preflight: list[PreflightIssue] = []

    def get_tasks(self, projects: list[dict[str, Any]]) -> list[Task]:
        return list(self._tasks)

    def filter_tasks(
        self, tasks: list[Task], state: dict[str, Any], config: dict[str, Any]
    ) -> list[Task]:
        return [t for t in tasks if t.priority != "P4"]

    def get_task_description(self, task: Task) -> str | None:
        if task.task_id == "handled":
            return "Full description for handled task"
        return None

    def get_agent_prompt_template(self) -> str | None:
        return "custom template for {task_id}"

    def preflight_checks(self, config: dict[str, Any]) -> list[PreflightIssue]:
        return list(self._preflight)

    def ui_sections(self) -> list[UISection]:
        return [UISection(name="tasks", sort_order=10)]

    def handle_action(self, action: str, payload: Any) -> bool:
        return action == "my_action"


def _make_task(task_id: str = "t-1", priority: str = "P2") -> Task:
    return Task(
        task_id=task_id,
        title="Test task",
        priority=priority,
        project_path="/tmp/proj",
        project_name="proj",
    )


def _make_manager_with(*plugins: PennyPlugin) -> PluginManager:
    """Create a PluginManager with pre-registered plugins (not yet active)."""
    mgr = PluginManager()
    for p in plugins:
        mgr._plugins[p.name] = p
    return mgr


# ── UISection ─────────────────────────────────────────────────────────────────


class TestUISection:
    def test_defaults(self):
        section = UISection(name="test")
        assert section.name == "test"
        assert section.sort_order == 50
        assert section.build_view() is None
        assert section.rebuild({}) is None

    def test_custom_sort_order(self):
        section = UISection(name="top", sort_order=1)
        assert section.sort_order == 1


# ── PennyPlugin base class ───────────────────────────────────────────────────


class TestPennyPluginDefaults:
    def test_is_available_defaults_true(self):
        plugin = StubPlugin()
        assert plugin.is_available() is True

    def test_preflight_checks_defaults_empty(self):
        plugin = StubPlugin()
        assert plugin.preflight_checks({}) == []

    def test_get_tasks_defaults_empty(self):
        plugin = StubPlugin()
        assert plugin.get_tasks([]) == []

    def test_get_completed_tasks_defaults_empty(self):
        plugin = StubPlugin()
        assert plugin.get_completed_tasks([], {}) == []

    def test_on_agent_spawned_defaults_noop(self):
        plugin = StubPlugin()
        task = _make_task()
        plugin_state: dict = {}
        plugin.on_agent_spawned(task, {}, plugin_state)  # should not raise
        assert plugin_state == {}

    def test_on_agent_completed_defaults_noop(self):
        plugin = StubPlugin()
        plugin_state: dict = {}
        plugin.on_agent_completed({}, plugin_state)  # should not raise
        assert plugin_state == {}

    def test_filter_tasks_defaults_passthrough(self):
        plugin = StubPlugin()
        tasks = [_make_task("a"), _make_task("b")]
        assert plugin.filter_tasks(tasks, {}, {}) is tasks

    def test_get_task_description_defaults_none(self):
        plugin = StubPlugin()
        assert plugin.get_task_description(_make_task()) is None

    def test_get_agent_prompt_template_defaults_none(self):
        plugin = StubPlugin()
        assert plugin.get_agent_prompt_template() is None

    def test_ui_sections_defaults_empty(self):
        plugin = StubPlugin()
        assert plugin.ui_sections() == []

    def test_config_schema_defaults_empty(self):
        plugin = StubPlugin()
        assert plugin.config_schema() == {}

    def test_cli_commands_defaults_empty(self):
        plugin = StubPlugin()
        assert plugin.cli_commands() == []

    def test_handle_action_defaults_false(self):
        plugin = StubPlugin()
        assert plugin.handle_action("any", None) is False


# ── PluginManager.activate / deactivate ───────────────────────────────────────


class TestPluginManagerActivation:
    def test_activate_discovered_plugin(self):
        stub = StubPlugin()
        mgr = _make_manager_with(stub)
        assert mgr.activate("stub", MagicMock(), {}) is True
        assert stub.activated
        assert mgr.active_plugins == [stub]

    def test_activate_returns_false_for_unknown(self):
        mgr = PluginManager()
        assert mgr.activate("nonexistent", MagicMock(), {}) is False

    def test_activate_idempotent(self):
        stub = StubPlugin()
        mgr = _make_manager_with(stub)
        mgr.activate("stub", MagicMock(), {})
        assert mgr.activate("stub", MagicMock(), {}) is True
        assert mgr.active_plugins == [stub]

    def test_activate_handles_exception(self):
        err = ErrorPlugin(plugin_name="err")
        mgr = _make_manager_with(err)
        assert mgr.activate("err", MagicMock(), {}) is False
        assert mgr.active_plugins == []

    def test_deactivate_active_plugin(self):
        stub = StubPlugin()
        mgr = _make_manager_with(stub)
        mgr.activate("stub", MagicMock(), {})
        mgr.deactivate("stub")
        assert stub.deactivated
        assert mgr.active_plugins == []

    def test_deactivate_unknown_is_noop(self):
        mgr = PluginManager()
        mgr.deactivate("nope")  # should not raise

    def test_deactivate_handles_exception(self):
        err = ErrorPlugin(plugin_name="err")
        mgr = _make_manager_with(err)
        mgr._active["err"] = err  # force active
        mgr.deactivate("err")  # should not raise
        assert "err" not in mgr._active

    def test_all_plugins_returns_copy(self):
        stub = StubPlugin()
        mgr = _make_manager_with(stub)
        result = mgr.all_plugins
        assert result == {"stub": stub}
        result["stub"] = None  # mutating copy
        assert mgr.all_plugins["stub"] is stub


# ── PluginManager.sync_with_config ────────────────────────────────────────────


class TestSyncWithConfig:
    def test_enabled_true_activates(self):
        stub = StubPlugin()
        mgr = _make_manager_with(stub)
        config = {"plugins": {"stub": {"enabled": True}}}
        mgr.sync_with_config(MagicMock(), config)
        assert stub.activated
        assert mgr.active_plugins == [stub]

    def test_enabled_false_does_not_activate(self):
        stub = StubPlugin()
        mgr = _make_manager_with(stub)
        config = {"plugins": {"stub": {"enabled": False}}}
        mgr.sync_with_config(MagicMock(), config)
        assert not stub.activated
        assert mgr.active_plugins == []

    def test_enabled_false_deactivates_active(self):
        stub = StubPlugin()
        mgr = _make_manager_with(stub)
        mgr.activate("stub", MagicMock(), {})
        config = {"plugins": {"stub": {"enabled": False}}}
        mgr.sync_with_config(MagicMock(), config)
        assert stub.deactivated
        assert mgr.active_plugins == []

    def test_enabled_auto_uses_is_available_true(self):
        stub = StubPlugin(available=True)
        mgr = _make_manager_with(stub)
        config = {"plugins": {"stub": {"enabled": "auto"}}}
        mgr.sync_with_config(MagicMock(), config)
        assert stub.activated

    def test_enabled_auto_uses_is_available_false(self):
        stub = StubPlugin(available=False)
        mgr = _make_manager_with(stub)
        config = {"plugins": {"stub": {"enabled": "auto"}}}
        mgr.sync_with_config(MagicMock(), config)
        assert not stub.activated

    def test_boolean_shorthand_true(self):
        stub = StubPlugin()
        mgr = _make_manager_with(stub)
        config = {"plugins": {"stub": True}}
        mgr.sync_with_config(MagicMock(), config)
        assert stub.activated

    def test_boolean_shorthand_false(self):
        stub = StubPlugin()
        mgr = _make_manager_with(stub)
        config = {"plugins": {"stub": False}}
        mgr.sync_with_config(MagicMock(), config)
        assert not stub.activated

    def test_defaults_to_auto_when_no_config(self):
        stub = StubPlugin(available=True)
        mgr = _make_manager_with(stub)
        mgr.sync_with_config(MagicMock(), {})
        assert stub.activated

    def test_string_true_activates(self):
        stub = StubPlugin()
        mgr = _make_manager_with(stub)
        config = {"plugins": {"stub": {"enabled": "true"}}}
        mgr.sync_with_config(MagicMock(), config)
        assert stub.activated

    def test_string_false_does_not_activate(self):
        stub = StubPlugin()
        mgr = _make_manager_with(stub)
        config = {"plugins": {"stub": {"enabled": "false"}}}
        mgr.sync_with_config(MagicMock(), config)
        assert not stub.activated


# ── PluginManager.discover ────────────────────────────────────────────────────


class TestPluginManagerDiscover:
    def test_discover_finds_beads_plugin(self):
        mgr = PluginManager()
        mgr.discover()
        assert "beads" in mgr.all_plugins

    def test_discover_loads_no_duplicates(self):
        mgr = PluginManager()
        mgr.discover()
        mgr.discover()  # second call
        assert list(mgr.all_plugins.keys()).count("beads") == 1

    def test_discover_handles_missing_dir(self, tmp_path):
        mgr = PluginManager()
        with patch("penny.plugin.Path.__truediv__", return_value=tmp_path / "nope"):
            mgr.discover()
        # should not raise, just have no plugins from that path


# ── PluginManager aggregation methods ─────────────────────────────────────────


class TestAggregationMethods:
    def test_get_all_preflight_checks_collects(self):
        tp = TaskPlugin(plugin_name="tp")
        tp._preflight = [
            PreflightIssue(severity="warning", message="check 1", fix_hint="fix"),
        ]
        mgr = _make_manager_with(tp)
        mgr.activate("tp", MagicMock(), {})
        issues = mgr.get_all_preflight_checks({})
        assert len(issues) == 1
        assert issues[0].message == "check 1"

    def test_get_all_preflight_checks_handles_error(self):
        class BadPlugin(StubPlugin):
            def preflight_checks(self, config):
                raise RuntimeError("boom")

        bp = BadPlugin(plugin_name="bad")
        mgr = _make_manager_with(bp)
        mgr._active["bad"] = bp
        issues = mgr.get_all_preflight_checks({})
        assert issues == []

    def test_get_all_tasks_collects(self):
        t = _make_task("t-1")
        tp = TaskPlugin(plugin_name="tp", tasks=[t])
        mgr = _make_manager_with(tp)
        mgr.activate("tp", MagicMock(), {})
        tasks = mgr.get_all_tasks([])
        assert len(tasks) == 1
        assert tasks[0].task_id == "t-1"

    def test_get_all_tasks_handles_error(self):
        class BadPlugin(StubPlugin):
            def get_tasks(self, projects):
                raise RuntimeError("boom")

        bp = BadPlugin(plugin_name="bad")
        mgr = _make_manager_with(bp)
        mgr._active["bad"] = bp
        tasks = mgr.get_all_tasks([])
        assert tasks == []

    def test_get_all_completed_tasks_collects(self):
        class CompletingPlugin(StubPlugin):
            def get_completed_tasks(self, projects, plugin_state):
                return [_make_task("done-1"), _make_task("done-2")]

        cp = CompletingPlugin(plugin_name="cp")
        mgr = _make_manager_with(cp)
        mgr._active["cp"] = cp
        done = mgr.get_all_completed_tasks([], {})
        assert len(done) == 2
        assert {t.task_id for t in done} == {"done-1", "done-2"}

    def test_get_all_completed_tasks_empty_by_default(self):
        stub = StubPlugin()
        mgr = _make_manager_with(stub)
        mgr.activate("stub", MagicMock(), {})
        assert mgr.get_all_completed_tasks([], {}) == []

    def test_get_all_completed_tasks_handles_error(self):
        class BadPlugin(StubPlugin):
            def get_completed_tasks(self, projects, plugin_state):
                raise RuntimeError("boom")

        bp = BadPlugin(plugin_name="bad")
        mgr = _make_manager_with(bp)
        mgr._active["bad"] = bp
        assert mgr.get_all_completed_tasks([], {}) == []

    def test_notify_agent_spawned_broadcasts_to_plugins(self):
        spawned: list[str] = []

        class SpawnPlugin(StubPlugin):
            def on_agent_spawned(self, task, record, plugin_state):
                spawned.append(task.task_id)

        sp = SpawnPlugin(plugin_name="sp")
        mgr = _make_manager_with(sp)
        mgr._active["sp"] = sp
        task = _make_task("t-1")
        state: dict = {}
        mgr.notify_agent_spawned(task, {}, state)
        assert spawned == ["t-1"]

    def test_notify_agent_spawned_passes_plugin_namespace(self):
        received_state: list[dict] = []

        class SpawnPlugin(StubPlugin):
            def on_agent_spawned(self, task, record, plugin_state):
                plugin_state["seen"] = True
                received_state.append(plugin_state)

        sp = SpawnPlugin(plugin_name="sp")
        mgr = _make_manager_with(sp)
        mgr._active["sp"] = sp
        state: dict = {}
        mgr.notify_agent_spawned(_make_task(), {}, state)
        assert state["plugin_state"]["sp"]["seen"] is True

    def test_notify_agent_completed_broadcasts_to_plugins(self):
        completed: list[str] = []

        class CompletedPlugin(StubPlugin):
            def on_agent_completed(self, record, plugin_state):
                completed.append(record.get("task_id"))

        cp = CompletedPlugin(plugin_name="cp")
        mgr = _make_manager_with(cp)
        mgr._active["cp"] = cp
        state: dict = {}
        mgr.notify_agent_completed({"task_id": "t-2"}, state)
        assert completed == ["t-2"]

    def test_get_all_completed_tasks_passes_plugin_state(self):
        received: list[dict] = []

        class StatefulPlugin(StubPlugin):
            def get_completed_tasks(self, projects, plugin_state):
                received.append(plugin_state)
                return []

        sp = StatefulPlugin(plugin_name="sp")
        mgr = _make_manager_with(sp)
        mgr._active["sp"] = sp
        state: dict = {"plugin_state": {"sp": {"x": 1}}}
        mgr.get_all_completed_tasks([], state)
        assert received[0] == {"x": 1}

    def test_filter_all_tasks_chains_filters(self):
        tp = TaskPlugin(plugin_name="tp")
        mgr = _make_manager_with(tp)
        mgr.activate("tp", MagicMock(), {})
        tasks = [_make_task("a", "P2"), _make_task("b", "P4")]
        filtered = mgr.filter_all_tasks(tasks, {}, {})
        assert len(filtered) == 1
        assert filtered[0].task_id == "a"

    def test_filter_all_tasks_handles_error(self):
        class BadPlugin(StubPlugin):
            def filter_tasks(self, tasks, state, config):
                raise RuntimeError("boom")

        bp = BadPlugin(plugin_name="bad")
        mgr = _make_manager_with(bp)
        mgr._active["bad"] = bp
        tasks = [_make_task()]
        result = mgr.filter_all_tasks(tasks, {}, {})
        assert result == tasks  # original list returned on error

    def test_get_task_description_first_non_none_wins(self):
        tp = TaskPlugin(plugin_name="tp")
        mgr = _make_manager_with(tp)
        mgr.activate("tp", MagicMock(), {})
        task = _make_task("handled")
        desc = mgr.get_task_description(task)
        assert desc == "Full description for handled task"

    def test_get_task_description_fallback(self):
        stub = StubPlugin()
        mgr = _make_manager_with(stub)
        mgr.activate("stub", MagicMock(), {})
        task = _make_task("xyz")
        desc = mgr.get_task_description(task)
        assert "xyz" in desc
        assert "Test task" in desc

    def test_get_task_description_handles_error(self):
        class BadPlugin(StubPlugin):
            def get_task_description(self, task):
                raise RuntimeError("boom")

        bp = BadPlugin(plugin_name="bad")
        mgr = _make_manager_with(bp)
        mgr._active["bad"] = bp
        task = _make_task("t-1")
        desc = mgr.get_task_description(task)
        assert "t-1" in desc  # fallback

    def test_get_agent_prompt_template_first_non_none_wins(self):
        tp = TaskPlugin(plugin_name="tp")
        mgr = _make_manager_with(tp)
        mgr.activate("tp", MagicMock(), {})
        tmpl = mgr.get_agent_prompt_template(_make_task())
        assert tmpl == "custom template for {task_id}"

    def test_get_agent_prompt_template_none_when_no_plugins(self):
        mgr = PluginManager()
        assert mgr.get_agent_prompt_template(_make_task()) is None

    def test_get_agent_prompt_template_handles_error(self):
        class BadPlugin(StubPlugin):
            def get_agent_prompt_template(self):
                raise RuntimeError("boom")

        bp = BadPlugin(plugin_name="bad")
        mgr = _make_manager_with(bp)
        mgr._active["bad"] = bp
        assert mgr.get_agent_prompt_template(_make_task()) is None

    def test_get_all_ui_sections_sorted(self):
        class PluginA(StubPlugin):
            def ui_sections(self):
                return [UISection(name="late", sort_order=90)]

        class PluginB(StubPlugin):
            def ui_sections(self):
                return [UISection(name="early", sort_order=5)]

        a = PluginA(plugin_name="a")
        b = PluginB(plugin_name="b")
        mgr = _make_manager_with(a, b)
        mgr._active["a"] = a
        mgr._active["b"] = b
        sections = mgr.get_all_ui_sections()
        assert [s.name for s in sections] == ["early", "late"]

    def test_get_all_ui_sections_handles_error(self):
        class BadPlugin(StubPlugin):
            def ui_sections(self):
                raise RuntimeError("boom")

        bp = BadPlugin(plugin_name="bad")
        mgr = _make_manager_with(bp)
        mgr._active["bad"] = bp
        assert mgr.get_all_ui_sections() == []

    def test_dispatch_action_returns_true_when_handled(self):
        tp = TaskPlugin(plugin_name="tp")
        mgr = _make_manager_with(tp)
        mgr.activate("tp", MagicMock(), {})
        assert mgr.dispatch_action("my_action", None) is True

    def test_dispatch_action_returns_false_when_unhandled(self):
        tp = TaskPlugin(plugin_name="tp")
        mgr = _make_manager_with(tp)
        mgr.activate("tp", MagicMock(), {})
        assert mgr.dispatch_action("unknown_action", None) is False

    def test_dispatch_action_handles_error(self):
        class BadPlugin(StubPlugin):
            def handle_action(self, action, payload):
                raise RuntimeError("boom")

        bp = BadPlugin(plugin_name="bad")
        mgr = _make_manager_with(bp)
        mgr._active["bad"] = bp
        assert mgr.dispatch_action("anything", None) is False


# ── Plugin validation ─────────────────────────────────────────────────────────


class TestPluginValidation:
    def test_validate_valid_plugin_returns_no_errors(self):
        mgr = PluginManager()
        errors = mgr._validate_plugin_instance(StubPlugin(), "good_plugin.py")
        assert errors == []

    def test_validate_rejects_empty_name(self):
        class EmptyName(StubPlugin):
            @property
            def name(self) -> str:
                return ""

        mgr = PluginManager()
        errors = mgr._validate_plugin_instance(EmptyName(), "empty_name.py")
        assert len(errors) == 1
        assert "'name'" in errors[0]

    def test_validate_rejects_whitespace_only_name(self):
        class WhitespaceName(StubPlugin):
            @property
            def name(self) -> str:
                return "   "

        mgr = PluginManager()
        errors = mgr._validate_plugin_instance(WhitespaceName(), "ws_plugin.py")
        assert len(errors) == 1
        assert "'name'" in errors[0]

    def test_validate_rejects_non_string_name(self):
        class IntName(StubPlugin):
            @property
            def name(self):  # type: ignore[override]
                return 42

        mgr = PluginManager()
        errors = mgr._validate_plugin_instance(IntName(), "int_name.py")
        assert len(errors) == 1
        assert "'name'" in errors[0]

    def test_validate_rejects_non_string_description(self):
        class IntDesc(StubPlugin):
            @property
            def description(self):  # type: ignore[override]
                return 99

        mgr = PluginManager()
        errors = mgr._validate_plugin_instance(IntDesc(), "int_desc.py")
        assert len(errors) == 1
        assert "'description'" in errors[0]

    def test_validate_handles_exception_in_name(self):
        class BoomName(StubPlugin):
            @property
            def name(self) -> str:
                raise RuntimeError("boom")

        mgr = PluginManager()
        errors = mgr._validate_plugin_instance(BoomName(), "boom_plugin.py")
        assert len(errors) == 1
        assert "'name'" in errors[0]
        assert "boom" in errors[0]

    def test_validate_handles_exception_in_description(self):
        class BoomDesc(StubPlugin):
            @property
            def description(self) -> str:
                raise RuntimeError("desc boom")

        mgr = PluginManager()
        errors = mgr._validate_plugin_instance(BoomDesc(), "boom_desc.py")
        assert len(errors) == 1
        assert "'description'" in errors[0]

    def test_load_errors_initially_empty(self):
        mgr = PluginManager()
        assert mgr.load_errors == []

    def test_load_errors_returns_copy(self):
        mgr = PluginManager()
        mgr._load_errors.append(
            PreflightIssue(severity="error", message="test", fix_hint="fix")
        )
        copy = mgr.load_errors
        copy.clear()
        assert len(mgr.load_errors) == 1

    def test_get_all_preflight_checks_includes_load_errors(self):
        mgr = PluginManager()
        mgr._load_errors.append(
            PreflightIssue(
                severity="error",
                message="Plugin 'bad.py' failed to import: missing dep",
                fix_hint="Fix it.",
            )
        )
        issues = mgr.get_all_preflight_checks({})
        assert len(issues) == 1
        assert issues[0].message == "Plugin 'bad.py' failed to import: missing dep"

    def test_get_all_preflight_checks_combines_load_errors_and_plugin_checks(self):
        tp = TaskPlugin(plugin_name="tp")
        tp._preflight = [
            PreflightIssue(severity="warning", message="plugin warning", fix_hint="fix"),
        ]
        mgr = _make_manager_with(tp)
        mgr.activate("tp", MagicMock(), {})
        mgr._load_errors.append(
            PreflightIssue(severity="error", message="load error", fix_hint="fix load")
        )
        issues = mgr.get_all_preflight_checks({})
        messages = [i.message for i in issues]
        assert "load error" in messages
        assert "plugin warning" in messages


class TestDiscoverWithBadPlugins:
    """Tests for discover() handling of malformed plugin files."""

    def _fake_plugins_dir(self, tmp_path: Any, filenames: list[str]) -> Any:
        for name in filenames:
            (tmp_path / name).touch()
        return tmp_path

    def test_discover_stores_load_error_for_import_failure(self, tmp_path: Any) -> None:
        self._fake_plugins_dir(tmp_path, ["broken_plugin.py"])
        mgr = PluginManager()

        with patch.object(mgr, "_plugins_dir", return_value=tmp_path), \
             patch("penny.plugin.importlib.import_module", side_effect=ImportError("no dep")):
            mgr.discover()

        assert len(mgr.load_errors) == 1
        assert mgr.load_errors[0].severity == "error"
        assert "broken_plugin.py" in mgr.load_errors[0].message
        assert mgr.all_plugins == {}

    def test_discover_stores_load_error_when_no_plugin_class(self, tmp_path: Any) -> None:
        self._fake_plugins_dir(tmp_path, ["empty_plugin.py"])
        mgr = PluginManager()
        fake_module = builtin_types.SimpleNamespace()  # no Plugin attribute

        with patch.object(mgr, "_plugins_dir", return_value=tmp_path), \
             patch("penny.plugin.importlib.import_module", return_value=fake_module):
            mgr.discover()

        assert len(mgr.load_errors) == 1
        assert "empty_plugin.py" in mgr.load_errors[0].message
        assert "valid Plugin class" in mgr.load_errors[0].message

    def test_discover_stores_load_error_when_not_subclass(self, tmp_path: Any) -> None:
        self._fake_plugins_dir(tmp_path, ["wrong_plugin.py"])
        mgr = PluginManager()

        class NotAPlugin:
            pass

        fake_module = builtin_types.SimpleNamespace(Plugin=NotAPlugin)

        with patch.object(mgr, "_plugins_dir", return_value=tmp_path), \
             patch("penny.plugin.importlib.import_module", return_value=fake_module):
            mgr.discover()

        assert len(mgr.load_errors) == 1
        assert "wrong_plugin.py" in mgr.load_errors[0].message
        assert "valid Plugin class" in mgr.load_errors[0].message

    def test_discover_stores_load_error_when_instantiation_fails(self, tmp_path: Any) -> None:
        self._fake_plugins_dir(tmp_path, ["abstract_plugin.py"])
        mgr = PluginManager()

        # Subclass of PennyPlugin missing required abstract methods → TypeError on init
        class PartialPlugin(PennyPlugin):
            @property
            def name(self) -> str:
                return "partial"

            @property
            def description(self) -> str:
                return "Partial"

            # on_activate, on_deactivate, on_agent_spawned, on_agent_completed NOT implemented

        fake_module = builtin_types.SimpleNamespace(Plugin=PartialPlugin)

        with patch.object(mgr, "_plugins_dir", return_value=tmp_path), \
             patch("penny.plugin.importlib.import_module", return_value=fake_module):
            mgr.discover()

        assert len(mgr.load_errors) >= 1
        assert "abstract_plugin.py" in mgr.load_errors[0].message
        assert mgr.all_plugins == {}

    def test_discover_stores_load_error_for_api_violation(self, tmp_path: Any) -> None:
        self._fake_plugins_dir(tmp_path, ["badname_plugin.py"])
        mgr = PluginManager()

        class BadNamePlugin(StubPlugin):
            @property
            def name(self) -> str:
                return ""  # empty name is an API violation

        fake_module = builtin_types.SimpleNamespace(Plugin=BadNamePlugin)

        with patch.object(mgr, "_plugins_dir", return_value=tmp_path), \
             patch("penny.plugin.importlib.import_module", return_value=fake_module):
            mgr.discover()

        assert len(mgr.load_errors) == 1
        assert "badname_plugin.py" in mgr.load_errors[0].message
        assert "API violation" in mgr.load_errors[0].message
        assert mgr.all_plugins == {}

    def test_discover_does_not_register_plugin_with_api_violation(self, tmp_path: Any) -> None:
        self._fake_plugins_dir(tmp_path, ["baddesc_plugin.py"])
        mgr = PluginManager()

        class BadDescPlugin(StubPlugin):
            @property
            def description(self):  # type: ignore[override]
                return 42  # non-string description

        fake_module = builtin_types.SimpleNamespace(Plugin=BadDescPlugin)

        with patch.object(mgr, "_plugins_dir", return_value=tmp_path), \
             patch("penny.plugin.importlib.import_module", return_value=fake_module):
            mgr.discover()

        assert mgr.all_plugins == {}
        assert len(mgr.load_errors) == 1

    def test_discover_registers_valid_plugin_without_errors(self, tmp_path: Any) -> None:
        self._fake_plugins_dir(tmp_path, ["good_plugin.py"])
        mgr = PluginManager()
        fake_module = builtin_types.SimpleNamespace(Plugin=StubPlugin)

        with patch.object(mgr, "_plugins_dir", return_value=tmp_path), \
             patch("penny.plugin.importlib.import_module", return_value=fake_module):
            mgr.discover()

        assert mgr.load_errors == []
        assert "stub" in mgr.all_plugins

    def test_discover_bad_plugin_does_not_block_good_plugin(self, tmp_path: Any) -> None:
        self._fake_plugins_dir(tmp_path, ["aaa_bad_plugin.py", "zzz_good_plugin.py"])
        mgr = PluginManager()

        class BadPlugin(StubPlugin):
            @property
            def name(self) -> str:
                return ""

        bad_module = builtin_types.SimpleNamespace(Plugin=BadPlugin)
        good_module = builtin_types.SimpleNamespace(Plugin=StubPlugin)

        call_count = 0

        def fake_import(module_name: str) -> Any:
            nonlocal call_count
            call_count += 1
            if "aaa_bad" in module_name:
                return bad_module
            return good_module

        with patch.object(mgr, "_plugins_dir", return_value=tmp_path), \
             patch("penny.plugin.importlib.import_module", side_effect=fake_import):
            mgr.discover()

        assert len(mgr.load_errors) == 1
        assert "stub" in mgr.all_plugins  # good plugin still loaded


# ── Integration: discover + sync + aggregate ──────────────────────────────────


class TestPluginIntegration:
    def test_discover_sync_and_aggregate(self):
        """Full lifecycle: discover -> sync -> collect tasks."""
        mgr = PluginManager()
        mgr.discover()

        # Force beads plugin availability
        beads = mgr.all_plugins.get("beads")
        if beads is None:
            pytest.skip("beads plugin not discovered")

        with patch.object(beads, "is_available", return_value=True):
            config = {"plugins": {"beads": {"enabled": "auto"}}}
            mgr.sync_with_config(MagicMock(), config)

        assert "beads" in [p.name for p in mgr.active_plugins]

        # Deactivate
        config_off = {"plugins": {"beads": {"enabled": False}}}
        mgr.sync_with_config(MagicMock(), config_off)
        assert mgr.active_plugins == []

    def test_multiple_plugins_aggregate_tasks(self):
        """Multiple active plugins contribute tasks."""
        t1 = _make_task("t-1", "P1")
        t2 = _make_task("t-2", "P2")
        tp1 = TaskPlugin(plugin_name="p1", tasks=[t1])
        tp2 = TaskPlugin(plugin_name="p2", tasks=[t2])
        mgr = _make_manager_with(tp1, tp2)
        mgr.activate("p1", MagicMock(), {})
        mgr.activate("p2", MagicMock(), {})
        all_tasks = mgr.get_all_tasks([])
        assert len(all_tasks) == 2
        ids = {t.task_id for t in all_tasks}
        assert ids == {"t-1", "t-2"}

    def test_multiple_plugins_dispatch_first_handler_wins(self):
        """First plugin that handles an action wins."""
        tp1 = TaskPlugin(plugin_name="p1")  # handles "my_action"
        stub = StubPlugin(plugin_name="p2")  # handles nothing
        mgr = _make_manager_with(tp1, stub)
        mgr.activate("p1", MagicMock(), {})
        mgr.activate("p2", MagicMock(), {})
        assert mgr.dispatch_action("my_action", None) is True

    def test_error_in_one_plugin_does_not_block_others(self):
        """One plugin raising should not prevent others from contributing."""

        class BadTaskPlugin(StubPlugin):
            def get_tasks(self, projects):
                raise RuntimeError("boom")

        bad = BadTaskPlugin(plugin_name="bad")
        good = TaskPlugin(plugin_name="good", tasks=[_make_task("ok")])
        mgr = _make_manager_with(bad, good)
        mgr._active["bad"] = bad
        mgr._active["good"] = good
        tasks = mgr.get_all_tasks([])
        assert len(tasks) == 1
        assert tasks[0].task_id == "ok"


# ── PennyPlugin default hook returns ─────────────────────────────────────────


class TestPluginDefaultHooks:
    """Verify new extensibility hooks have safe defaults."""

    def test_cli_commands_returns_empty_list(self):
        plugin = StubPlugin()
        assert plugin.cli_commands() == []

    def test_dashboard_card_html_returns_none(self):
        plugin = StubPlugin()
        assert plugin.dashboard_card_html({}, {}) is None

    def test_dashboard_api_handler_returns_none(self):
        plugin = StubPlugin()
        assert plugin.dashboard_api_handler("GET", "anything", {}) is None

    def test_report_section_returns_none(self):
        plugin = StubPlugin()
        assert plugin.report_section({}, {}) is None


# ── PluginManager.get_dashboard_cards ────────────────────────────────────────


class TestGetDashboardCards:
    def test_empty_when_no_active_plugins(self):
        mgr = PluginManager()
        assert mgr.get_dashboard_cards({}, {}) == []

    def test_returns_card_when_plugin_contributes(self):
        class CardPlugin(StubPlugin):
            def dashboard_card_html(self, state, config):
                return "<h2>My Card</h2>"

        plugin = CardPlugin(plugin_name="card")
        mgr = PluginManager()
        mgr._active["card"] = plugin
        cards = mgr.get_dashboard_cards({}, {})
        assert len(cards) == 1
        assert cards[0]["name"] == "card"
        assert "<h2>My Card</h2>" in cards[0]["html"]

    def test_skips_none_returns(self):
        """Plugins returning None contribute no card."""
        plugin = StubPlugin(plugin_name="nocard")  # default returns None
        mgr = PluginManager()
        mgr._active["nocard"] = plugin
        assert mgr.get_dashboard_cards({}, {}) == []

    def test_error_in_plugin_does_not_propagate(self):
        class BoomCard(StubPlugin):
            def dashboard_card_html(self, state, config):
                raise RuntimeError("boom")

        plugin = BoomCard(plugin_name="boom")
        mgr = PluginManager()
        mgr._active["boom"] = plugin
        # Should not raise
        assert mgr.get_dashboard_cards({}, {}) == []

    def test_multiple_plugins_contribute_cards(self):
        class CardA(StubPlugin):
            def dashboard_card_html(self, state, config):
                return "<h2>Card A</h2>"

        class CardB(StubPlugin):
            def dashboard_card_html(self, state, config):
                return "<h2>Card B</h2>"

        mgr = PluginManager()
        mgr._active["a"] = CardA(plugin_name="a")
        mgr._active["b"] = CardB(plugin_name="b")
        cards = mgr.get_dashboard_cards({}, {})
        assert len(cards) == 2
        names = {c["name"] for c in cards}
        assert names == {"a", "b"}


# ── PluginManager.handle_dashboard_route ─────────────────────────────────────


class TestHandleDashboardRoute:
    def test_returns_none_when_plugin_not_active(self):
        mgr = PluginManager()
        result = mgr.handle_dashboard_route("missing", "POST", "action", {})
        assert result is None

    def test_routes_to_active_plugin(self):
        class RoutePlugin(StubPlugin):
            def dashboard_api_handler(self, method, path_suffix, payload):
                if path_suffix == "ping":
                    return {"pong": True}
                return None

        plugin = RoutePlugin(plugin_name="rp")
        mgr = PluginManager()
        mgr._active["rp"] = plugin
        result = mgr.handle_dashboard_route("rp", "GET", "ping", {})
        assert result == {"pong": True}

    def test_returns_none_when_plugin_does_not_handle(self):
        plugin = StubPlugin(plugin_name="p")  # default returns None
        mgr = PluginManager()
        mgr._active["p"] = plugin
        assert mgr.handle_dashboard_route("p", "GET", "unknown", {}) is None

    def test_error_returns_none(self):
        class ErrorRoute(StubPlugin):
            def dashboard_api_handler(self, method, path_suffix, payload):
                raise RuntimeError("boom")

        plugin = ErrorRoute(plugin_name="err")
        mgr = PluginManager()
        mgr._active["err"] = plugin
        assert mgr.handle_dashboard_route("err", "POST", "action", {}) is None


# ── PluginManager.get_report_sections ────────────────────────────────────────


class TestGetReportSections:
    def test_empty_when_no_active_plugins(self):
        mgr = PluginManager()
        assert mgr.get_report_sections({}, {}) == []

    def test_returns_section_html(self):
        class SectionPlugin(StubPlugin):
            def report_section(self, state, config):
                return "<h2>Plugin Section</h2><p>Content</p>"

        plugin = SectionPlugin(plugin_name="sp")
        mgr = PluginManager()
        mgr._active["sp"] = plugin
        sections = mgr.get_report_sections({}, {})
        assert len(sections) == 1
        assert "<h2>Plugin Section</h2>" in sections[0]

    def test_skips_none_returns(self):
        plugin = StubPlugin(plugin_name="ns")
        mgr = PluginManager()
        mgr._active["ns"] = plugin
        assert mgr.get_report_sections({}, {}) == []

    def test_error_does_not_propagate(self):
        class BoomSection(StubPlugin):
            def report_section(self, state, config):
                raise RuntimeError("boom")

        plugin = BoomSection(plugin_name="bs")
        mgr = PluginManager()
        mgr._active["bs"] = plugin
        assert mgr.get_report_sections({}, {}) == []

    def test_multiple_sections_collected(self):
        class SecA(StubPlugin):
            def report_section(self, state, config):
                return "<h2>A</h2>"

        class SecB(StubPlugin):
            def report_section(self, state, config):
                return "<h2>B</h2>"

        mgr = PluginManager()
        mgr._active["a"] = SecA(plugin_name="a")
        mgr._active["b"] = SecB(plugin_name="b")
        sections = mgr.get_report_sections({}, {})
        assert len(sections) == 2


# ── PluginManager.get_all_cli_commands ───────────────────────────────────────


class TestGetAllCliCommands:
    def test_empty_when_no_active_plugins(self):
        mgr = PluginManager()
        assert mgr.get_all_cli_commands() == []

    def test_collects_commands_with_plugin_name(self):
        class CmdPlugin(StubPlugin):
            def cli_commands(self):
                return [{"name": "mytask", "description": "Do something"}]

        plugin = CmdPlugin(plugin_name="cp")
        mgr = PluginManager()
        mgr._active["cp"] = plugin
        commands = mgr.get_all_cli_commands()
        assert len(commands) == 1
        assert commands[0]["name"] == "mytask"
        assert commands[0]["plugin"] == "cp"

    def test_error_does_not_propagate(self):
        class BoomCmd(StubPlugin):
            def cli_commands(self):
                raise RuntimeError("boom")

        plugin = BoomCmd(plugin_name="bc")
        mgr = PluginManager()
        mgr._active["bc"] = plugin
        assert mgr.get_all_cli_commands() == []

    def test_multiple_plugins_commands_aggregated(self):
        class CmdA(StubPlugin):
            def cli_commands(self):
                return [{"name": "cmd-a", "description": "A"}]

        class CmdB(StubPlugin):
            def cli_commands(self):
                return [{"name": "cmd-b", "description": "B"},
                        {"name": "cmd-c", "description": "C"}]

        mgr = PluginManager()
        mgr._active["a"] = CmdA(plugin_name="a")
        mgr._active["b"] = CmdB(plugin_name="b")
        commands = mgr.get_all_cli_commands()
        assert len(commands) == 3
        names = {c["name"] for c in commands}
        assert names == {"cmd-a", "cmd-b", "cmd-c"}


# ── dashboard _try_plugin_route / _meta ──────────────────────────────────────


class TestDashboardPluginRouting:
    """Test the _try_plugin_route and _meta helper functions."""

    def _make_app(self, active_plugins=None):
        """Create a minimal mock app with a plugin manager."""
        from penny.plugin import PluginManager
        app = MagicMock()
        app.state = {}
        app.config = {}
        app._plugin_mgr = PluginManager()
        if active_plugins:
            for plugin in active_plugins:
                app._plugin_mgr._active[plugin.name] = plugin
        return app

    def test_try_plugin_route_returns_none_for_non_plugin_path(self):
        from penny.dashboard import _try_plugin_route
        app = self._make_app()
        assert _try_plugin_route(app, "GET", "/api/state", {}) is None

    def test_try_plugin_route_routes_to_plugin(self):
        from penny.dashboard import _try_plugin_route

        class RoutePlugin(StubPlugin):
            def dashboard_api_handler(self, method, path_suffix, payload):
                return {"hello": "world"}

        plugin = RoutePlugin(plugin_name="rp")
        app = self._make_app([plugin])
        result = _try_plugin_route(app, "GET", "/api/plugin/rp/status", {})
        assert result == {"hello": "world"}

    def test_try_plugin_route_returns_none_for_inactive_plugin(self):
        from penny.dashboard import _try_plugin_route
        app = self._make_app()
        assert _try_plugin_route(app, "GET", "/api/plugin/missing/status", {}) is None

    def test_meta_returns_active_plugins_and_commands(self):
        from penny.dashboard import _meta

        class CmdPlugin(StubPlugin):
            def cli_commands(self):
                return [{"name": "tasks", "description": "List tasks"}]

        plugin = CmdPlugin(plugin_name="beads")
        app = self._make_app([plugin])
        meta = _meta(app)
        assert "beads" in meta["active_plugins"]
        assert any(c["name"] == "tasks" for c in meta["cli_commands"])

    def test_meta_returns_empty_when_no_plugin_mgr(self):
        from penny.dashboard import _meta
        app = MagicMock()
        del app._plugin_mgr
        meta = _meta(app)
        assert meta["active_plugins"] == []
        assert meta["cli_commands"] == []


# ── report.py plugin section injection ───────────────────────────────────────


class TestReportPluginSections:
    def _make_plugin_mgr(self, section_html: str | None = None):
        mgr = MagicMock()
        mgr.get_all_tasks.return_value = []
        mgr.get_task_description.return_value = ""
        mgr.get_report_sections.return_value = [section_html] if section_html else []
        return mgr

    def test_plugin_section_appended_to_report(self, tmp_path):
        from unittest.mock import patch

        import penny.report as rep

        plugin_section = "<h2>Beads Section</h2><p>Task data here</p>"
        mgr = self._make_plugin_mgr(plugin_section)

        with patch.object(rep, "REPORT_DIR", tmp_path):
            path = rep.generate_report({}, {}, plugin_mgr=mgr)
        content = path.read_text()
        assert "<h2>Beads Section</h2>" in content

    def test_no_plugin_section_when_mgr_is_none(self, tmp_path):
        from unittest.mock import patch

        import penny.report as rep

        with patch.object(rep, "REPORT_DIR", tmp_path):
            path = rep.generate_report({}, {}, plugin_mgr=None)
        content = path.read_text()
        # Should render without error (no plugin section injected)
        assert "Penny" in content

    def test_plugin_section_skipped_when_empty(self, tmp_path):
        from unittest.mock import patch

        import penny.report as rep

        mgr = self._make_plugin_mgr(section_html=None)  # returns []

        with patch.object(rep, "REPORT_DIR", tmp_path):
            path = rep.generate_report({}, {}, plugin_mgr=mgr)
        content = path.read_text()
        # No plugin card div should be injected
        assert "plugin-cards-container" not in content


# ── Plugin on_first_activated / _ever_activated ───────────────────────────────


class TestPluginFirstActivated:
    """Test the first-activation notification tracking in PluginManager."""

    class TrackedPlugin(StubPlugin):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.first_activated_called = False
            self.first_activated_app = None

        def on_first_activated(self, app):
            self.first_activated_called = True
            self.first_activated_app = app

    def test_on_first_activated_called_once(self):
        plugin = self.TrackedPlugin(plugin_name="tp")
        mgr = PluginManager()
        mgr._plugins["tp"] = plugin
        app = MagicMock()

        mgr.activate("tp", app, {})
        assert plugin.first_activated_called is True
        # Deactivate and re-activate — should NOT call again
        mgr.deactivate("tp")
        plugin.first_activated_called = False  # reset flag
        mgr.activate("tp", app, {})
        assert plugin.first_activated_called is False  # not called again

    def test_on_first_activated_receives_app(self):
        plugin = self.TrackedPlugin(plugin_name="tp2")
        mgr = PluginManager()
        mgr._plugins["tp2"] = plugin
        app = MagicMock()

        mgr.activate("tp2", app, {})
        assert plugin.first_activated_app is app

    def test_on_first_activated_error_does_not_break_activation(self):
        class BoomFirstActivated(StubPlugin):
            def on_first_activated(self, app):
                raise RuntimeError("boom")

        plugin = BoomFirstActivated(plugin_name="bfa")
        mgr = PluginManager()
        mgr._plugins["bfa"] = plugin
        mgr.activate("bfa", MagicMock(), {})
        # Plugin is still active despite the error
        assert "bfa" in mgr._active

    def test_ever_activated_persists_after_deactivation(self):
        plugin = self.TrackedPlugin(plugin_name="tp3")
        mgr = PluginManager()
        mgr._plugins["tp3"] = plugin
        app = MagicMock()

        mgr.activate("tp3", app, {})
        mgr.deactivate("tp3")
        assert "tp3" in mgr._ever_activated


# ── install.sh / config template smoke tests ──────────────────────────────────


class TestInstallAndConfigTemplate:
    """Test that install.sh and config template reflect optional beads."""

    def test_install_sh_bd_is_optional(self):
        from pathlib import Path
        install_sh = Path(__file__).parent.parent / "install.sh"
        content = install_sh.read_text()
        # bd should be marked as Optional, not as a hard error
        assert "Optional" in content or "optional" in content
        assert "DEP_ERRORS=1" not in content.split("BD_BIN")[1].split("CLAUDE_BIN")[0]

    def test_config_template_has_plugins_section(self):
        from pathlib import Path
        template = Path(__file__).parent.parent / "config.yaml.template"
        content = template.read_text()
        assert "plugins:" in content
        assert "beads:" in content

    def test_config_template_beads_optional_in_checklist(self):
        from pathlib import Path
        template = Path(__file__).parent.parent / "config.yaml.template"
        content = template.read_text()
        # Beads should be marked as optional
        assert "Optional" in content or "optional" in content

    def test_needs_onboarding_without_beads(self):
        """needs_onboarding() should not require beads to be installed."""
        from penny.onboarding import needs_onboarding
        # A config with a real project path should NOT need onboarding
        config = {"projects": [{"path": "/Users/me/project"}]}
        assert needs_onboarding(config) is False

    def test_needs_onboarding_no_plugins_key_required(self):
        """Onboarding check should not require plugins section in config."""
        from penny.onboarding import needs_onboarding
        config = {"projects": [{"path": "/Users/me/project"}]}  # no plugins key
        assert needs_onboarding(config) is False
