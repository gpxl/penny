# Plugin Architecture Rules

## Core / Plugin Separation (CRITICAL)

Penny's core (`app.py`, `install.sh`, `plugin.py`, `spawner.py`, `dashboard.py`) must never
reference specific plugins by name. Plugins are self-contained — they bring their own
dependency resolution, PATH handling, and binary discovery.

| Boundary | Core owns | Plugin owns |
|----------|-----------|-------------|
| Binary discovery | Generic plugin activation (`is_available()`) | Finding its own binary (e.g., searching nvm, pnpm, volta dirs) |
| PATH resolution | Providing the subprocess API | Building env/PATH for its own subprocesses |
| install.sh | `claude`, `bd`, `python3` — tools core depends on | Nothing — plugins must not add themselves to install.sh |
| Config schema | `plugins.<name>.enabled` toggle | All plugin-specific keys via `config_schema()` |
| State | `state["plugin_state"]` namespace | Everything inside its namespace |
| Dashboard | Routing `/api/plugin/<name>/` | Generating its own HTML cards and API responses |

### What goes in install.sh

Only tools that **Penny core** depends on to function:
- `python3` — runs the app
- `claude` — the CLI that core's spawner invokes
- `bd` — used by core's task scheduling

If a plugin needs a binary, the plugin's `is_available()` and helpers must locate it.
Never add plugin-specific binary detection or PATH entries to install.sh.

### Plugin binary discovery pattern

Plugins that wrap external CLIs should:
1. Use `shutil.which()` first (respects current PATH)
2. Check common install locations as fallback (e.g., `~/Library/pnpm/`, `~/.local/bin/`)
3. Build their own subprocess `env` with augmented PATH if the CLI depends on
   other binaries (e.g., `node` for Node.js CLI shims)
4. Report missing binaries via `preflight_checks()`, not by modifying core

### Plugin integration pattern

Plugins integrating with external systems must:
1. Use the system's own API/CLI — never read its internal files
2. Query at lifecycle boundaries (activation, agent completion) — not on every polling cycle
3. Cache results in `plugin_state` — `get_tasks()` must be fast (dict lookups only)
4. Handle failures gracefully — timeouts, missing binaries, invalid responses
