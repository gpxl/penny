"""Penny plugin architecture — protocol and registry."""

from __future__ import annotations

import importlib
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .preflight import PreflightIssue
from .tasks import Task


@dataclass
class UISection:
    """A UI section contributed by a plugin to the popover.

    build_view() is called once during loadView to create the NSView.
    rebuild(data) is called on every updateWithData_ refresh cycle.
    """

    name: str
    sort_order: int = 50
    build_view: Callable[[], Any] = field(default=lambda: None)
    rebuild: Callable[[dict[str, Any]], None] = field(default=lambda data: None)


class PennyPlugin(ABC):
    """Base class for all Penny plugins."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique plugin identifier (e.g. 'beads')."""
        ...

    @property
    @abstractmethod
    def description(self) -> str:
        """Human-readable description shown in UI/config."""
        ...

    def is_available(self) -> bool:
        """Auto-detect if plugin's dependencies are present.

        Called before activation. Return False to prevent loading
        (e.g. required binary not in PATH, required directory missing).
        """
        return True

    def preflight_checks(self, config: dict[str, Any]) -> list[PreflightIssue]:
        """Contribute validation checks to the preflight system."""
        return []

    def on_activate(self, app: Any) -> None:  # noqa: B027
        """Called when the plugin is activated. `app` is the PennyApp delegate."""

    def on_deactivate(self) -> None:  # noqa: B027
        """Called when the plugin is deactivated."""

    def get_tasks(self, projects: list[dict[str, Any]]) -> list[Task]:
        """Supply tasks to the task queue."""
        return []

    def on_agent_spawned(self, task: Task, record: dict[str, Any], plugin_state: dict[str, Any]) -> None:  # noqa: B027
        """Called after core spawns an agent. plugin_state is mutable; core persists it."""

    def on_agent_completed(self, record: dict[str, Any], plugin_state: dict[str, Any]) -> None:  # noqa: B027
        """Called when core detects agent completion."""

    def get_completed_tasks(
        self, projects: list[dict[str, Any]], plugin_state: dict[str, Any]
    ) -> list[Task]:
        """Return ONLY newly-seen externally-completed tasks.

        Update plugin_state to record seen IDs so the same tasks are never
        returned twice. Default: [].
        """
        return []

    def filter_tasks(
        self,
        tasks: list[Task],
        state: dict[str, Any],
        config: dict[str, Any],
    ) -> list[Task]:
        """Filter/prioritize tasks. Default: pass through unchanged."""
        return tasks

    def get_task_description(self, task: Task) -> str | None:
        """Fetch full description for a task. Return None if not handled."""
        return None

    def get_agent_prompt_template(self) -> str | None:
        """Return a custom agent prompt template, or None to use the default."""
        return None

    def ui_sections(self) -> list[UISection]:
        """Contribute UI sections to the popover."""
        return []

    def config_schema(self) -> dict[str, Any]:
        """Declare plugin-specific config keys and defaults."""
        return {}

    def cli_commands(self) -> list[Any]:
        """Register CLI subcommands."""
        return []

    def handle_action(self, action: str, payload: Any) -> bool:
        """Handle a plugin-specific action dispatched from the UI.

        Return True if handled, False to pass to the next plugin.
        """
        return False


class PluginManager:
    """Discovers, loads, and manages Penny plugins."""

    def __init__(self) -> None:
        self._plugins: dict[str, PennyPlugin] = {}
        self._active: dict[str, PennyPlugin] = {}

    @property
    def active_plugins(self) -> list[PennyPlugin]:
        return list(self._active.values())

    @property
    def all_plugins(self) -> dict[str, PennyPlugin]:
        return dict(self._plugins)

    def discover(self) -> None:
        """Discover plugins from penny/plugins/ directory."""
        plugins_dir = Path(__file__).parent / "plugins"
        if not plugins_dir.exists():
            return

        for path in sorted(plugins_dir.glob("*_plugin.py")):
            module_name = f"penny.plugins.{path.stem}"
            try:
                module = importlib.import_module(module_name)
                plugin_cls = getattr(module, "Plugin", None)
                if plugin_cls and isinstance(plugin_cls, type) and issubclass(plugin_cls, PennyPlugin):
                    instance = plugin_cls()
                    self._plugins[instance.name] = instance
            except Exception as exc:
                print(f"[penny] Failed to load plugin {path.stem}: {exc}", flush=True)

    def activate(self, name: str, app: Any, config: dict[str, Any]) -> bool:
        """Activate a discovered plugin. Returns True on success."""
        plugin = self._plugins.get(name)
        if plugin is None:
            return False
        if name in self._active:
            return True
        try:
            plugin.on_activate(app)
            self._active[name] = plugin
            return True
        except Exception as exc:
            print(f"[penny] Failed to activate plugin {name}: {exc}", flush=True)
            return False

    def deactivate(self, name: str) -> None:
        """Deactivate a plugin."""
        plugin = self._active.pop(name, None)
        if plugin is not None:
            try:
                plugin.on_deactivate()
            except Exception as exc:
                print(f"[penny] Error deactivating plugin {name}: {exc}", flush=True)

    def sync_with_config(self, app: Any, config: dict[str, Any]) -> None:
        """Load/unload plugins based on config and availability.

        Config format:
            plugins:
              beads:
                enabled: true | false | auto
        """
        plugins_cfg = config.get("plugins", {})

        for name, plugin in self._plugins.items():
            pcfg = plugins_cfg.get(name, {})
            if isinstance(pcfg, bool):
                pcfg = {"enabled": pcfg}
            enabled = pcfg.get("enabled", "auto")

            should_activate = False
            if enabled is True or str(enabled).lower() == "true":
                should_activate = True
            elif enabled is False or str(enabled).lower() == "false":
                should_activate = False
            else:
                # "auto" — activate if dependencies are present
                should_activate = plugin.is_available()

            if should_activate and name not in self._active:
                self.activate(name, app, config)
            elif not should_activate and name in self._active:
                self.deactivate(name)

    # ── Aggregation methods ───────────────────────────────────────────────

    def get_all_preflight_checks(self, config: dict[str, Any]) -> list[PreflightIssue]:
        """Collect preflight checks from all active plugins."""
        issues: list[PreflightIssue] = []
        for plugin in self._active.values():
            try:
                issues.extend(plugin.preflight_checks(config))
            except Exception as exc:
                print(f"[penny] preflight error in plugin {plugin.name}: {exc}", flush=True)
        return issues

    def get_all_tasks(self, projects: list[dict[str, Any]]) -> list[Task]:
        """Collect tasks from all active plugins."""
        all_tasks: list[Task] = []
        for plugin in self._active.values():
            try:
                all_tasks.extend(plugin.get_tasks(projects))
            except Exception as exc:
                print(f"[penny] get_tasks error in plugin {plugin.name}: {exc}", flush=True)
        return all_tasks

    def notify_agent_spawned(self, task: Task, record: dict[str, Any], state: dict[str, Any]) -> None:
        """Broadcast agent-spawned event to all active plugins."""
        for plugin in self._active.values():
            plugin_state = state.setdefault("plugin_state", {}).setdefault(plugin.name, {})
            try:
                plugin.on_agent_spawned(task, record, plugin_state)
            except Exception as exc:
                print(f"[penny] on_agent_spawned error in plugin {plugin.name}: {exc}", flush=True)

    def notify_agent_completed(self, record: dict[str, Any], state: dict[str, Any]) -> None:
        """Broadcast agent-completed event to all active plugins."""
        for plugin in self._active.values():
            plugin_state = state.setdefault("plugin_state", {}).setdefault(plugin.name, {})
            try:
                plugin.on_agent_completed(record, plugin_state)
            except Exception as exc:
                print(f"[penny] on_agent_completed error in plugin {plugin.name}: {exc}", flush=True)

    def get_all_completed_tasks(self, projects: list[dict[str, Any]], state: dict[str, Any]) -> list[Task]:
        """Collect externally completed tasks from all active plugins.

        Each plugin receives its own namespaced plugin_state so it can track
        which task IDs it has already reported.
        """
        all_completed: list[Task] = []
        for plugin in self._active.values():
            plugin_state = state.setdefault("plugin_state", {}).setdefault(plugin.name, {})
            try:
                all_completed.extend(plugin.get_completed_tasks(projects, plugin_state))
            except Exception as exc:
                print(f"[penny] get_completed_tasks error in plugin {plugin.name}: {exc}", flush=True)
        return all_completed

    def filter_all_tasks(
        self,
        tasks: list[Task],
        state: dict[str, Any],
        config: dict[str, Any],
    ) -> list[Task]:
        """Run tasks through each active plugin's filter in sequence."""
        for plugin in self._active.values():
            try:
                tasks = plugin.filter_tasks(tasks, state, config)
            except Exception as exc:
                print(f"[penny] filter_tasks error in plugin {plugin.name}: {exc}", flush=True)
        return tasks

    def get_task_description(self, task: Task) -> str:
        """Ask each active plugin for a task description; first non-None wins."""
        for plugin in self._active.values():
            try:
                desc = plugin.get_task_description(task)
                if desc is not None:
                    return desc
            except Exception as exc:
                print(f"[penny] get_task_description error in plugin {plugin.name}: {exc}", flush=True)
        return f"Task {task.task_id}: {task.title}"

    def get_agent_prompt_template(self, task: Task) -> str | None:
        """Ask each active plugin for a prompt template; first non-None wins."""
        for plugin in self._active.values():
            try:
                tmpl = plugin.get_agent_prompt_template()
                if tmpl is not None:
                    return tmpl
            except Exception as exc:
                print(f"[penny] prompt template error in plugin {plugin.name}: {exc}", flush=True)
        return None

    def get_all_ui_sections(self) -> list[UISection]:
        """Collect UI sections from all active plugins, sorted by sort_order."""
        sections: list[UISection] = []
        for plugin in self._active.values():
            try:
                sections.extend(plugin.ui_sections())
            except Exception as exc:
                print(f"[penny] ui_sections error in plugin {plugin.name}: {exc}", flush=True)
        sections.sort(key=lambda s: s.sort_order)
        return sections

    def dispatch_action(self, action: str, payload: Any) -> bool:
        """Dispatch an action to active plugins. Returns True if any handled it."""
        for plugin in self._active.values():
            try:
                if plugin.handle_action(action, payload):
                    return True
            except Exception as exc:
                print(f"[penny] action error in plugin {plugin.name}: {exc}", flush=True)
        return False
