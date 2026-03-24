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

    @property
    def hidden(self) -> bool:
        """If True, plugin is not shown in UI or activated via config.

        Use for plugins that are not ready for end-user consumption.
        """
        return False

    def is_available(self) -> bool:
        """Auto-detect if plugin's dependencies are present.

        Called before activation. Return False to prevent loading
        (e.g. required binary not in PATH, required directory missing).
        """
        return True

    def preflight_checks(self, config: dict[str, Any]) -> list[PreflightIssue]:
        """Contribute validation checks to the preflight system."""
        return []

    @abstractmethod
    def on_activate(self, app: Any) -> None:
        """Called when the plugin is activated. `app` is the PennyApp delegate."""

    def on_first_activated(self, app: Any) -> None:  # noqa: B027
        """Called once the very first time the plugin transitions to active.

        Use this to show a one-time welcome notification (e.g., when the user
        installs 'bd' after Penny is already running and the plugin auto-detects it).
        """

    def setup_project(self, project_path: Path) -> bool:
        """Set up plugin dependencies in a project directory.

        Called during onboarding for each added project.
        Return True if setup was performed, False otherwise.
        """
        return False

    def default_allowed_tools(self) -> list[str]:
        """Tools to append to the scoped-mode allowed list during onboarding.

        Return [] to contribute nothing.
        """
        return []

    @abstractmethod
    def on_deactivate(self) -> None:
        """Called when the plugin is deactivated."""

    def get_tasks(self, projects: list[dict[str, Any]]) -> list[Task]:
        """Supply tasks to the task queue."""
        return []

    @abstractmethod
    def on_agent_spawned(self, task: Task, record: dict[str, Any], plugin_state: dict[str, Any]) -> None:
        """Called after core spawns an agent. plugin_state is mutable; core persists it."""

    @abstractmethod
    def on_agent_completed(self, record: dict[str, Any], plugin_state: dict[str, Any]) -> None:
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

    def install_command(self) -> str | None:
        """Return a shell command to install this plugin's dependencies.

        Called when the user clicks "Install" in the settings panel.
        Return None if the plugin cannot be installed automatically.
        """
        return None

    def config_schema(self) -> dict[str, Any]:
        """Declare plugin-specific config keys and defaults."""
        return {}

    def cli_commands(self) -> list[dict[str, Any]]:
        """Register CLI subcommands exposed via `penny help` and the dashboard API.

        Each item must be a dict with at minimum:
          {"name": "tasks", "description": "List ready tasks"}
        Optional keys: "api_path", "method", "arg" (arg name if one positional arg needed).
        """
        return []

    def handle_action(self, action: str, payload: Any) -> bool:
        """Handle a plugin-specific action dispatched from the UI.

        Return True if handled, False to pass to the next plugin.
        """
        return False

    def dashboard_card_html(self, state: dict[str, Any], config: dict[str, Any]) -> str | None:
        """Return an HTML snippet to render as a card in the live dashboard.

        Called on every /api/state poll. Return None to contribute nothing.
        The snippet is wrapped in a .card div automatically.

        Override get_dashboard_cards() instead to contribute multiple cards.
        """
        return None

    def get_dashboard_cards(
        self, state: dict[str, Any], config: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """Return a list of {name, html} dicts to render as cards in the dashboard.

        Override this to contribute multiple cards. Default wraps dashboard_card_html().
        """
        html = self.dashboard_card_html(state, config)
        if html:
            return [{"name": self.name, "html": html}]
        return []

    def dashboard_api_handler(
        self, method: str, path_suffix: str, payload: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Handle a dashboard API request routed to this plugin.

        Called for requests matching /api/plugin/<name>/<path_suffix>.
        Return a JSON-serializable dict on success, None if not handled.
        The caller returns HTTP 200 on non-None, 404 on None.
        """
        return None

    def report_section(self, state: dict[str, Any], config: dict[str, Any]) -> str | None:
        """Return an HTML section to append to the static status report.

        Return None to contribute nothing.
        """
        return None


class PluginManager:
    """Discovers, loads, and manages Penny plugins."""

    def __init__(self) -> None:
        self._plugins: dict[str, PennyPlugin] = {}
        self._active: dict[str, PennyPlugin] = {}
        self._load_errors: list[PreflightIssue] = []
        # Track which plugins have ever been activated so we can detect
        # the first-time activation (e.g., user installed bd after launch).
        self._ever_activated: set[str] = set()

    @property
    def active_plugins(self) -> list[PennyPlugin]:
        return list(self._active.values())

    @property
    def all_plugins(self) -> dict[str, PennyPlugin]:
        return dict(self._plugins)

    @property
    def load_errors(self) -> list[PreflightIssue]:
        """PreflightIssues raised during plugin discovery (import or API violations)."""
        return list(self._load_errors)

    def _plugins_dir(self) -> Path:
        """Return the directory where built-in plugins are discovered."""
        return Path(__file__).parent / "plugins"

    def _record_load_error(self, source: str, message: str, fix_hint: str) -> None:
        full_msg = f"Plugin '{source}' {message}"
        print(f"[penny] {full_msg}", flush=True)
        self._load_errors.append(PreflightIssue(severity="error", message=full_msg, fix_hint=fix_hint))

    def _validate_plugin_instance(self, instance: PennyPlugin, source: str) -> list[str]:
        """Return a list of API violation strings. Empty list means the plugin is valid."""
        errors: list[str] = []

        try:
            name = instance.name
            if not isinstance(name, str) or not name.strip():
                errors.append(f"'name' must return a non-empty string (got {name!r})")
        except Exception as exc:
            errors.append(f"'name' property raised an exception: {exc}")

        try:
            desc = instance.description
            if not isinstance(desc, str):
                errors.append(f"'description' must return a string (got {type(desc).__name__})")
        except Exception as exc:
            errors.append(f"'description' property raised an exception: {exc}")

        return errors

    def discover(self) -> None:
        """Discover plugins from penny/plugins/ directory."""
        plugins_dir = self._plugins_dir()
        if not plugins_dir.exists():
            return

        for path in sorted(plugins_dir.glob("*_plugin.py")):
            module_name = f"penny.plugins.{path.stem}"
            source = path.name
            try:
                module = importlib.import_module(module_name)
            except Exception as exc:
                self._record_load_error(
                    source,
                    f"failed to import: {exc}",
                    f"Check {source} for import errors or missing dependencies.",
                )
                continue

            plugin_cls = getattr(module, "Plugin", None)
            if not (plugin_cls and isinstance(plugin_cls, type) and issubclass(plugin_cls, PennyPlugin)):
                self._record_load_error(
                    source,
                    "does not expose a valid Plugin class (must subclass PennyPlugin)",
                    f"Ensure {source} defines a class named 'Plugin' that inherits from PennyPlugin.",
                )
                continue

            try:
                instance = plugin_cls()
            except TypeError as exc:
                self._record_load_error(
                    source,
                    f"could not be instantiated: {exc}",
                    f"Implement all abstract methods of PennyPlugin in {source}.",
                )
                continue
            except Exception as exc:
                self._record_load_error(
                    source,
                    f"raised an unexpected error during instantiation: {exc}",
                    f"Check Plugin.__init__ in {source}.",
                )
                continue

            validation_errors = self._validate_plugin_instance(instance, source)
            if validation_errors:
                for err in validation_errors:
                    self._record_load_error(
                        source,
                        f"API violation — {err}",
                        f"Fix the Plugin class in {source} to comply with the PennyPlugin API.",
                    )
                continue

            self._plugins[instance.name] = instance

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
            is_new = name not in self._ever_activated
            self._ever_activated.add(name)
            if is_new:
                # First-ever activation — notify the user (e.g., beads just installed)
                try:
                    plugin.on_first_activated(app)
                except Exception as exc:
                    print(f"[penny] on_first_activated error in plugin {name}: {exc}", flush=True)
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
            if plugin.hidden:
                if name in self._active:
                    self.deactivate(name)
                continue

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
        """Collect preflight checks from all active plugins, plus any plugin load errors."""
        issues: list[PreflightIssue] = list(self._load_errors)
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

    def get_dashboard_cards(
        self, state: dict[str, Any], config: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """Collect {name, html} card snippets from all active plugins."""
        cards: list[dict[str, Any]] = []
        for plugin in self._active.values():
            try:
                cards.extend(plugin.get_dashboard_cards(state, config))
            except Exception as exc:
                print(f"[penny] get_dashboard_cards error in plugin {plugin.name}: {exc}", flush=True)
        return cards

    def handle_dashboard_route(
        self, plugin_name: str, method: str, path_suffix: str, payload: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Route /api/plugin/<name>/<suffix> to the named plugin.

        Returns the plugin's response dict, or None if not handled (→ 404).
        """
        plugin = self._active.get(plugin_name)
        if plugin is None:
            return None
        try:
            return plugin.dashboard_api_handler(method, path_suffix, payload)
        except Exception as exc:
            print(f"[penny] dashboard_api_handler error in plugin {plugin_name}: {exc}", flush=True)
            return None

    def get_report_sections(
        self, state: dict[str, Any], config: dict[str, Any]
    ) -> list[str]:
        """Collect HTML sections from all active plugins for the static report."""
        sections: list[str] = []
        for plugin in self._active.values():
            try:
                html = plugin.report_section(state, config)
                if html:
                    sections.append(html)
            except Exception as exc:
                print(f"[penny] report_section error in plugin {plugin.name}: {exc}", flush=True)
        return sections

    def get_all_cli_commands(self) -> list[dict[str, Any]]:
        """Collect CLI command specs from all active plugins."""
        commands: list[dict[str, Any]] = []
        for plugin in self._active.values():
            try:
                cmds = plugin.cli_commands()
                for cmd in cmds:
                    commands.append({**cmd, "plugin": plugin.name})
            except Exception as exc:
                print(f"[penny] cli_commands error in plugin {plugin.name}: {exc}", flush=True)
        return commands

    def setup_projects(self, paths: list[Path]) -> None:
        """Call setup_project on all available discovered plugins for each path."""
        for plugin in self._plugins.values():
            if not plugin.is_available():
                continue
            for path in paths:
                try:
                    plugin.setup_project(path)
                except Exception as exc:
                    print(f"[penny] plugin setup_project error ({plugin.name}): {exc}", flush=True)

    def get_default_allowed_tools(self) -> list[str]:
        """Collect extra allowed-tools contributions from all available plugins."""
        tools: list[str] = []
        for plugin in self._plugins.values():
            if plugin.is_available():
                try:
                    tools.extend(plugin.default_allowed_tools())
                except Exception:
                    pass
        return tools
