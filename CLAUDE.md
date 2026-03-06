# Penny — Claude Code Instructions

## UX Rule (CRITICAL)
Prefer configuration through config.yaml and the menubar/dashboard UI.
Users should NOT need terminal commands for normal operation.
- Service control → config.yaml `service:` section + popover toggles
- Project setup → config.yaml (opened via Preferences button)
- Restart after stop → ~/Applications/Penny.app (Spotlight/Finder) or `penny start`
- Terminal is acceptable ONLY for the one-time install.sh setup step

## Key Files
| File | Purpose |
|------|---------|
| penny/app.py | NSApplicationDelegate, config loading, service sync, spawning |
| penny/popover_vc.py | All UI — programmatic NSStackView, no NIB |
| penny/paths.py | data_dir() → PENNY_HOME or ~/.penny |
| penny/dashboard.py | Lazy HTTP server at 127.0.0.1:7432 |
| install.sh | One-time install + plist generation |
| config.yaml | Runtime config (projects, triggers, work, service) |
| scripts/penny | CLI: penny start|stop|status |

## Architecture Notes
- Config path: data_dir() / "config.yaml"
- Plist locations (both must stay in sync):
  - Source: $SCRIPT_DIR/com.gpxl.penny.plist
  - Active: ~/Library/LaunchAgents/com.gpxl.penny.plist
- SCRIPT_DIR readable from plist WorkingDirectory key at runtime
- Config loaded at startup in _load_and_refresh() (app.py); no hot-reload
- self._vc._app = self → VC has a reference to the app delegate

## Tech Stack
Python 3.9+, PyObjC, AppKit/Foundation, yaml, launchd (no RUMPS)
