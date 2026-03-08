# Penny Plugin Development Guide

Penny's automation behaviour is entirely driven by **plugins**. The built-in
`beads` plugin is the reference implementation; everything it does is possible
for any third-party plugin too.

This guide is for developers who want to add new automation features to Penny.

---

## Table of Contents

1. [Architecture Overview](#architecture-overview)
2. [File Structure](#file-structure)
3. [Step-by-Step: Creating a New Plugin](#step-by-step-creating-a-new-plugin)
4. [Plugin Protocol Reference](#plugin-protocol-reference)
5. [Config Schema Declaration](#config-schema-declaration)
6. [UI Section API](#ui-section-api)
7. [Agent Prompt Customisation](#agent-prompt-customisation)
8. [Testing Plugins in Isolation](#testing-plugins-in-isolation)
9. [Example: Minimal Skeleton Plugin](#example-minimal-skeleton-plugin)
10. [Example: Beads Plugin as Reference](#example-beads-plugin-as-reference)

---

## Architecture Overview

```
PennyApp (app.py)
  └─ PluginManager (plugin.py)
       ├─ discover()          ← scans penny/plugins/*_plugin.py
       ├─ sync_with_config()  ← activate / deactivate based on config.yaml
       └─ active plugins
            ├─ get_all_tasks()
            ├─ filter_all_tasks()
            ├─ notify_agent_spawned() / notify_agent_completed()
            ├─ get_all_ui_sections()
            ├─ get_dashboard_cards()
            └─ …
```

`PluginManager` is the single aggregator. Penny never calls plugin methods
directly — it always goes through `PluginManager`, which fans out to all
active plugins and collects results.

---

## File Structure

```
penny/
└─ plugins/
   ├─ __init__.py          ← empty (required)
   ├─ beads_plugin.py      ← built-in reference implementation
   └─ myplugin_plugin.py   ← your plugin (name must end in _plugin.py)
```

Naming rules:
- File **must** match `*_plugin.py`.
- File **must** define a top-level class called exactly `Plugin` that
  subclasses `penny.plugin.PennyPlugin`.
- The `name` property on your class becomes the key used everywhere
  (config, state, logs). Keep it short and lowercase, e.g. `"mytracker"`.

---

## Step-by-Step: Creating a New Plugin

### 1. Create the file

```
penny/plugins/mytracker_plugin.py
```

### 2. Subclass `PennyPlugin`

```python
from penny.plugin import PennyPlugin

class Plugin(PennyPlugin):
    ...
```

### 3. Implement required abstract methods

`name`, `description`, `on_activate`, `on_deactivate`,
`on_agent_spawned`, `on_agent_completed`.

Everything else is optional and has a sensible no-op default.

### 4. Declare your config schema (optional but recommended)

```python
def config_schema(self) -> dict:
    return {
        "api_url": {"type": "string", "default": "https://example.com"},
    }
```

### 5. Configure in `config.yaml`

```yaml
plugins:
  mytracker:
    enabled: auto   # true | false | auto
    api_url: https://mytracker.example.com
```

### 6. Write tests

Create `tests/test_mytracker_plugin.py` and test your plugin in isolation
(see [Testing Plugins in Isolation](#testing-plugins-in-isolation)).

### 7. Restart Penny

Penny hot-reloads config every 5 s, but plugin discovery only runs at
startup. After adding a new file, restart via `penny stop && penny start`
or re-open `~/Applications/Penny.app`.

---

## Plugin Protocol Reference

All methods are defined in `penny/plugin.py`.

### Identity (required)

| Method | Signature | Called when |
|--------|-----------|-------------|
| `name` | `-> str` | Discovery, logging, config lookup |
| `description` | `-> str` | UI/config display |

Both must return non-empty strings or the plugin will be rejected at load
time with a `PreflightIssue`.

---

### Lifecycle (required)

#### `on_activate(app)`
Called when the plugin transitions from inactive → active.
`app` is the `PennyApp` delegate — use it to access `app.config`,
`app.state`, or `app.plugin_manager`. Store the reference if needed.

#### `on_deactivate()`
Called when the plugin is disabled in config or when Penny quits.
Release any held resources or references here.

#### `on_first_activated(app)` *(optional)*
Called once per install — the very first time `on_activate` succeeds.
Useful for showing a welcome notification (e.g., "Your tracker is ready!").
Not called on subsequent restarts.

---

### Availability (optional)

#### `is_available() -> bool`
Auto-detect whether the plugin's dependencies exist.
Called before activation. Return `False` when, for example, a required
binary is not in `PATH` or a required directory is missing.
If `enabled: auto` in config and `is_available()` returns `False`,
the plugin is silently skipped.

```python
def is_available(self) -> bool:
    import shutil
    return shutil.which("mytool") is not None
```

---

### Preflight Checks (optional)

#### `preflight_checks(config) -> list[PreflightIssue]`
Called once at startup, after activation, alongside core checks.
Return `PreflightIssue` objects for any problems that would prevent
the plugin from working.

```python
from penny.preflight import PreflightIssue

def preflight_checks(self, config):
    issues = []
    if not Path("~/.mytracker/token").expanduser().exists():
        issues.append(PreflightIssue(
            severity="warning",
            message="MyTracker auth token not found.",
            fix_hint="Run `mytool auth login` to authenticate.",
        ))
    return issues
```

`severity` is `"error"` (blocks agent spawning) or `"warning"` (shown
but not blocking).

---

### Task Discovery (optional)

#### `get_tasks(projects) -> list[Task]`
Supply tasks to Penny's task queue. Called every polling cycle
(default: every 60 s).

`projects` is the list of project dicts from `config.yaml`:
```python
[{"path": "/Users/alice/myproject", "priority": 1}, ...]
```

Return `Task` objects:
```python
from penny.tasks import Task

Task(
    task_id="abc-123",
    title="Fix the login bug",
    priority="P1",          # P0–P4
    project_path="/path/to/project",
    project_name="myproject",
    raw_line="optional raw text from source",
    metadata={"extra": "anything"},
)
```

Tasks from all active plugins are merged then passed through `filter_tasks`.

---

#### `filter_tasks(tasks, state, config) -> list[Task]`
Filter or re-order the merged task list before spawning decisions.
`state` is the full mutable Penny state dict.
Return the subset (or re-ordered list) that should be eligible for
agent spawning.

Default: return `tasks` unchanged.

---

#### `get_task_description(task) -> str | None`
Fetch the full description for a task (e.g., from a remote API).
First active plugin to return a non-`None` string wins.
Called just before the agent prompt is assembled.

---

#### `get_completed_tasks(projects, plugin_state) -> list[Task]`
Return tasks that have been completed **outside** Penny (e.g., merged
PRs, externally closed tickets). Called every polling cycle.

`plugin_state` is a mutable dict namespaced to your plugin — use it
to track which IDs you've already reported so you never return the
same task twice:

```python
def get_completed_tasks(self, projects, plugin_state):
    seen = set(plugin_state.get("seen_ids", []))
    new_tasks = []
    for t in self._fetch_closed_tasks(projects):
        if t.task_id not in seen:
            new_tasks.append(t)
            seen.add(t.task_id)
    plugin_state["seen_ids"] = list(seen)
    return new_tasks
```

---

### Agent Lifecycle (required)

#### `on_agent_spawned(task, record, plugin_state)`
Called immediately after Penny launches a Claude agent subprocess.

- `task` — the `Task` object that triggered the spawn.
- `record` — the agent state dict (session name, log path, start time, …).
- `plugin_state` — your plugin's mutable namespace in `state.json`.

Use this to record which tasks have been dispatched:
```python
def on_agent_spawned(self, task, record, plugin_state):
    plugin_state.setdefault("spawned_ids", []).append(task.task_id)
```

#### `on_agent_completed(record, plugin_state)`
Called when Penny detects that an agent session has ended.
`record` contains `exit_code`, `ended_at`, and `task_id` (if set).
Use this for cleanup, metrics, or notifications.

---

### Agent Prompt Customisation (optional)

#### `get_agent_prompt_template() -> str | None`
Return a Python format-string that will be used as the Claude agent
prompt. First active plugin to return non-`None` wins.

Available template variables (filled by `penny/spawner.py`):

| Variable | Value |
|----------|-------|
| `{task_id}` | Task identifier, e.g. `abc-123` |
| `{task_title}` | Short task title |
| `{task_description}` | Full description from `get_task_description` |
| `{priority}` | Priority string, e.g. `P1` |
| `{project_path}` | Absolute path to the project directory |

```python
def get_agent_prompt_template(self) -> str:
    return """\
You are working on {project_path}.

Task {task_id}: {task_title}
Priority: {priority}

{task_description}

Complete the task end-to-end, then commit and push your changes.
"""
```

If no plugin supplies a template, a built-in default is used.

---

### UI Section API (optional)

#### `ui_sections() -> list[UISection]`
Contribute one or more sections to the Penny popover (the dropdown
that appears when you click the menubar icon).

Each `UISection` has:
- `name` — display name / internal identifier.
- `sort_order` — integer; lower values appear higher (core sections use
  10, 20, 30; use ≥ 50 to append below them).
- `build_view` — `Callable[[], NSView]` called once during `loadView`.
- `rebuild` — `Callable[[dict], None]` called on every data refresh.

```python
from penny.plugin import UISection

def ui_sections(self):
    section = self._build_my_section()
    return [section]

def _build_my_section(self):
    import objc
    from AppKit import NSStackView, NSView, NSUserInterfaceLayoutOrientationVertical
    from penny.ui_components import make_label

    container: list[NSView] = []

    def build_view():
        stack = NSStackView.alloc().initWithFrame_(((0, 0), (260, 40)))
        stack.setOrientation_(NSUserInterfaceLayoutOrientationVertical)
        label = make_label("MyTracker", bold=True)
        self._status_label = make_label("Loading…", secondary=True)
        stack.addArrangedSubview_(label)
        stack.addArrangedSubview_(self._status_label)
        return stack

    def rebuild(data):
        count = data.get("my_task_count", 0)
        self._status_label.setStringValue_(f"{count} tasks ready")

    return UISection(
        name="mytracker",
        sort_order=60,
        build_view=build_view,
        rebuild=rebuild,
    )
```

**UI helpers available** (from `penny.ui_components`):

| Helper | Signature | Returns |
|--------|-----------|---------|
| `make_label` | `(text, size=13.0, bold=False, secondary=False)` | `NSTextField` (non-editable label) |
| `make_button` | `(title, target, action, small=True)` | `NSButton` |
| `ProgressBarView` | `NSView` subclass with `.setPct(float)` | colour-coded rounded progress bar |

**Layout constraints:**
- The popover is **280 px wide**. Your `NSView` should fill horizontally.
- Use `NSStackView` with vertical orientation for multi-row sections.
- Keep total height reasonable — the popover scrolls if content overflows.
- Call `self.setNeedsDisplay_(True)` after updating custom drawing views.

---

### Dashboard & Report (optional)

#### `dashboard_card_html(state, config) -> str | None`
Return an HTML snippet rendered as a card in the live HTML dashboard
(`penny report`). Called on every `/api/state` poll.

```python
def dashboard_card_html(self, state, config):
    count = len(state.get("agents_running", []))
    return f"<h3>MyTracker</h3><p>{count} agents running</p>"
```

The snippet is wrapped in a `.card` div automatically.

#### `dashboard_api_handler(method, path_suffix, payload) -> dict | None`
Handle HTTP requests routed to `/api/plugin/<name>/<suffix>`.
Return a JSON-serialisable dict (→ HTTP 200) or `None` (→ HTTP 404).

#### `report_section(state, config) -> str | None`
Return an HTML section appended to the static status report.

---

### CLI Commands (optional)

#### `cli_commands() -> list[dict]`
Register subcommands shown in `penny help` and callable via the
dashboard API.

```python
def cli_commands(self):
    return [
        {
            "name": "tasks",
            "description": "List ready tasks from MyTracker",
            "api_path": "/api/state",
            "method": "GET",
        },
        {
            "name": "run",
            "description": "Spawn an agent for a task",
            "api_path": "/api/run",
            "method": "POST",
            "arg": "task-id",
        },
    ]
```

---

### Action Dispatch (optional)

#### `handle_action(action, payload) -> bool`
Handle plugin-specific actions dispatched from the UI or dashboard.
Return `True` if handled (stops propagation), `False` to pass through.

```python
def handle_action(self, action, payload):
    if action != "mytracker_refresh":
        return False
    self._force_refresh()
    return True
```

---

## Config Schema Declaration

#### `config_schema() -> dict`
Declare all plugin-specific keys with types and defaults. Penny merges
these declarations with the active config to build help text and
validate user config.

```python
def config_schema(self):
    return {
        "enabled": {
            "type": "string",
            "default": "auto",
            "description": "Enable plugin: true, false, or auto (detect)",
        },
        "api_url": {
            "type": "string",
            "default": "https://example.com/api",
            "description": "Base URL for MyTracker API",
        },
        "max_tasks": {
            "type": "int",
            "default": 5,
            "description": "Maximum tasks to fetch per poll",
        },
    }
```

Keys declared here appear in the config template generated by
`penny init`. The `enabled` key is special — Penny reads it via
`PluginManager.sync_with_config` to decide whether to activate the
plugin.

Access user config inside plugin methods via the `config` dict passed
to many hooks, or via `self._app.config` (once `on_activate` stores
`self._app = app`):

```python
def on_activate(self, app):
    self._app = app

def get_tasks(self, projects):
    cfg = self._app.config.get("plugins", {}).get(self.name, {})
    max_tasks = cfg.get("max_tasks", 5)
    ...
```

---

## Testing Plugins in Isolation

Penny plugins are plain Python classes with no macOS GUI dependency.
You can unit-test them without PyObjC, AppKit, or a running app.

### Recommended approach

1. Use `unittest.mock.MagicMock()` as a fake `app` delegate.
2. Instantiate your `Plugin()` class directly.
3. Call lifecycle methods manually.
4. Assert on return values and state mutations.

```python
# tests/test_mytracker_plugin.py
from unittest.mock import MagicMock
import pytest
from penny.plugins.mytracker_plugin import Plugin


@pytest.fixture
def plugin():
    p = Plugin()
    p.on_activate(MagicMock())
    return p


def test_name(plugin):
    assert plugin.name == "mytracker"


def test_get_tasks_empty_projects(plugin):
    assert plugin.get_tasks([]) == []


def test_filter_tasks_respects_limit(plugin):
    from penny.tasks import Task
    tasks = [
        Task("t1", "Task 1", "P1", "/proj", "proj"),
        Task("t2", "Task 2", "P2", "/proj", "proj"),
        Task("t3", "Task 3", "P3", "/proj", "proj"),
    ]
    config = {"plugins": {"mytracker": {"max_tasks": 2}}}
    result = plugin.filter_tasks(tasks, {}, config)
    assert len(result) == 2


def test_on_agent_spawned_records_id(plugin):
    from penny.tasks import Task
    task = Task("t1", "Fix bug", "P1", "/proj", "proj")
    state = {}
    plugin.on_agent_spawned(task, {}, state)
    assert "t1" in state.get("spawned_ids", [])


def test_preflight_warns_when_tool_missing(monkeypatch):
    import shutil
    monkeypatch.setattr(shutil, "which", lambda _: None)
    p = Plugin()
    issues = p.preflight_checks({})
    assert any(i.severity in ("error", "warning") for i in issues)
```

Run with:
```bash
python -m pytest tests/test_mytracker_plugin.py -v
```

### Testing `get_completed_tasks` idempotency

```python
def test_completed_tasks_not_returned_twice(plugin):
    plugin_state = {}
    # First call returns new tasks
    first = plugin.get_completed_tasks([{"path": "/proj"}], plugin_state)
    # Second call must return empty (same IDs already recorded)
    second = plugin.get_completed_tasks([{"path": "/proj"}], plugin_state)
    for t in first:
        assert not any(x.task_id == t.task_id for x in second)
```

### Testing UI sections without AppKit

UI sections involve `build_view` and `rebuild` callables. If you want
to test them without AppKit you can check that:
- `build_view` is callable and returns an object.
- `rebuild` is callable without raising.

Mock the AppKit types or guard the import:
```python
def test_ui_sections_callable(monkeypatch):
    import sys
    # Stub out AppKit if running on Linux/CI
    sys.modules.setdefault("AppKit", MagicMock())
    sys.modules.setdefault("objc", MagicMock())
    p = Plugin()
    p.on_activate(MagicMock())
    sections = p.ui_sections()
    assert len(sections) == 1
    assert callable(sections[0].build_view)
    assert callable(sections[0].rebuild)
```

---

## Example: Minimal Skeleton Plugin

`penny/plugins/skeleton_plugin.py`

```python
"""Minimal skeleton plugin — copy and adapt this to build your own."""

from __future__ import annotations

from typing import Any

from ..plugin import PennyPlugin
from ..preflight import PreflightIssue
from ..tasks import Task


class Plugin(PennyPlugin):
    """Replace this docstring with your plugin's description."""

    def __init__(self) -> None:
        self._app: Any = None

    # ── Identity ──────────────────────────────────────────────────────────

    @property
    def name(self) -> str:
        return "skeleton"           # unique, lowercase, no spaces

    @property
    def description(self) -> str:
        return "Skeleton plugin template"

    # ── Availability ──────────────────────────────────────────────────────

    def is_available(self) -> bool:
        return True                 # always available

    # ── Lifecycle ─────────────────────────────────────────────────────────

    def on_activate(self, app: Any) -> None:
        self._app = app

    def on_deactivate(self) -> None:
        self._app = None

    # ── Tasks ─────────────────────────────────────────────────────────────

    def get_tasks(self, projects: list[dict[str, Any]]) -> list[Task]:
        return []                   # implement: fetch tasks from your source

    def on_agent_spawned(
        self, task: Task, record: dict[str, Any], plugin_state: dict[str, Any]
    ) -> None:
        plugin_state.setdefault("spawned_ids", []).append(task.task_id)

    def on_agent_completed(
        self, record: dict[str, Any], plugin_state: dict[str, Any]
    ) -> None:
        pass

    # ── Config ────────────────────────────────────────────────────────────

    def config_schema(self) -> dict[str, Any]:
        return {
            "enabled": {
                "type": "string",
                "default": "auto",
                "description": "Enable skeleton plugin: true, false, or auto",
            },
        }
```

---

## Example: Beads Plugin as Reference

`penny/plugins/beads_plugin.py` is the full reference implementation.
Key patterns to study:

| Pattern | Where |
|---------|-------|
| Binary availability check | `is_available()` — `shutil.which("bd")` |
| Preflight per-project checks | `preflight_checks()` — iterates `config["projects"]` |
| Running a subprocess | `_run_bd()` helper, captures stdout, timeout-safe |
| Parsing CLI output | `_parse_bd_ready()`, `_parse_bd_list()` with `re.compile` |
| Task deduplication | `filter_tasks()` — union of `spawned_ids` + `running_ids` |
| Idempotent completed tasks | `get_completed_tasks()` — `seen_closed_ids` in `plugin_state` |
| Custom agent prompt | `AGENT_PROMPT_TEMPLATE` constant + `get_agent_prompt_template()` |
| UI action dispatch | `handle_action()` — checks `action == "bd_command"` |
| CLI commands | `cli_commands()` — list of dicts with `name`, `description`, `api_path` |
| First-activation notification | `on_first_activated()` — one-time macOS notification |

The beads plugin deliberately avoids any UI sections (`ui_sections()`
returns `[]`) because all beads information is shown inline in the
core task list. Your plugin may add UI sections to show supplementary
data.

---

## Quick Reference: Method Call Order

```
startup
  discover()                  ← scans penny/plugins/*_plugin.py
  sync_with_config()
    is_available()            ← skip if False and enabled=auto
    on_activate(app)          ← store app reference
    on_first_activated(app)   ← first-install welcome (once only)
  get_all_preflight_checks()
    preflight_checks(config)

poll cycle (every ~60 s)
  get_all_tasks(projects)
    get_tasks(projects)
  filter_all_tasks(tasks, state, config)
    filter_tasks(tasks, state, config)
  [spawn agents as needed]
    get_task_description(task)
    get_agent_prompt_template()
    on_agent_spawned(task, record, plugin_state)
  notify_agent_completed(record, state)
    on_agent_completed(record, plugin_state)
  get_all_completed_tasks(projects, state)
    get_completed_tasks(projects, plugin_state)

ui refresh (on popover open / data update)
  get_all_ui_sections()
    ui_sections()             ← build_view() once, rebuild(data) every refresh

shutdown
  on_deactivate()
```
