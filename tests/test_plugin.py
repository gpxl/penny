"""Unit tests for penny/plugin.py — PluginManager, PennyPlugin, UISection."""

from __future__ import annotations

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
