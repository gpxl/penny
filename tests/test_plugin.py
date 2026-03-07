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
