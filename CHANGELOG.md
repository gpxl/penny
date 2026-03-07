# Changelog

All notable changes to this project will be documented in this file. The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] - 2026-03-07

### Added

- macOS menu bar app (NSStatusItem + NSPopover) with Claude Max token monitoring
- Token usage reading from `~/.claude/stats-cache.json` every 5 minutes
- 90th-percentile capacity prediction with unused budget estimation
- Autonomous agent spawning via tmux when predicted unused capacity >= threshold with <= 2 days remaining
- Beads (`bd`) task integration — discovers ready tasks from configured projects
- Plugin architecture with Beads as the first plugin
- Live `/status` scraping via pexpect + pyte terminal emulator for accurate budget
- Session tracking with usage history
- Live dashboard HTTP server at `127.0.0.1:7432` with real-time state API
- HTML report generation with SVG usage chart
- Onboarding wizard for first-run setup
- Pre-flight validation checks (tools, auth, config)
- Full `penny` CLI with all menubar actions (`start`, `stop`, `run`, `agents`, `tasks`, etc.)
- `penny help` / `penny -h` / `penny version` / `penny -v` commands
- `penny open` to open dashboard in browser
- KeepAlive UI toggle (per-session vs persistent service)
- `~/Applications/Penny.app` symlink creation during install
- Config hot-reload — changes to `config.yaml` take effect without restart
- Comprehensive integration and end-to-end test suite
- `release.sh` script for Homebrew tap publishing
- Claude API outage detection and warning UI

### Changed

- Renamed project from NaeNae to Penny
- Replaced RUMPS with PyObjC `NSStatusItem` + `NSPopover` (no menu-based UI)
- Replaced batch + `execlp` agent spawning with interactive tmux `send-keys` injection
- Preferred terminal changed to Ghostty; tmux used for agent sessions
- Pagination, task row redesign, and opacity fix in popover UI

### Fixed

- Process identity shown as `python3` instead of `Penny`
- Stats rotation and stale menu data
- Documents folder permission prompt on macOS
- Session timing anchored to fixed billing period boundaries
- `ObjCPointerWarning` (replaced CGColor with NSBox separator)
- Refresh button width and content shift
- Formula-based Homebrew distribution
- Plist sync for `KeepAlive`/`RunAtLoad` config changes
- Onboarding and install edge cases
- Deduplicated completed tasks; replaced empty period history with session history
- Session hours remaining now computed from live reset label
- Removed `GITHUB_TOKEN`/`GH_TOKEN` from environment allowlist

### Security

- Spawned agent environments use an explicit allowlist (`HOME`, `PATH`, `USER`, `SHELL`, `LANG`, etc.) — credentials (`ANTHROPIC_API_KEY`, `AWS_*`, `DATABASE_URL`) are never inherited by agents
- Temporary prompt and runner files created with `0o600` permissions (owner-read/write only)
- Dashboard server binds to `127.0.0.1` only — not accessible from network
- Config loaded with `yaml.safe_load()` — no arbitrary code execution from config files
