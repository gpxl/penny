# Changelog

All notable changes to this project will be documented in this file. The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.6.0b1] - 2026-04-03

### Added

- Per-project and per-session token usage tracking in dashboard
- Session titles and sortable columns in Projects card
- Per-project health signals: error rate monitoring with contextual messages
- Health warning indicator (⚠) in menu bar icon for active alerts
- Budget projection alerts — warns when projected to use 85%+ of weekly budget
- Sustained session anomaly detection — flags projects burning tokens at 3x+ their active-hour baseline

### Changed

- Health alerts redesigned to be budget-aware and actionable — removed noisy absolute burn rate thresholds, cross-project comparison, session velocity/duration alerts, and 1-minute spike extrapolation
- Session History and Projects cards now use the global time window selector instead of independent filters
- Dashboard charts transition smoothly on data updates instead of redrawing from scratch
- Quick health scan simplified to error-rate detection only

### Fixed

- Batch session detection to eliminate N×scan startup bottleneck
- Project health dots use billing-period health, not selected window
- Scoped health baselines to current billing period
- Dashboard project accordions no longer auto-open for red items

## [0.5.0] - 2026-03-26

### Added

- Simplified clock arc animation to direct ease-out with all radial motion clockwise

### Changed

- Made CLAUDE.md self-contained for OSS portability

## [0.4.0] - 2026-03-25

### Added

- Sonnet independent reset schedule tracking (separate weekly reset from all-models reset)
- Dashboard settings page with live config API and plugin install streaming
- Compact popover layout with inline reset labels
- Pixel art penny icon set as application icon
- Loadout plugin with event-driven skill coverage tracking, PATH fix, and scan endpoint

### Fixed

- Auto-close popover on outside interaction via Transient behavior and watchdog
- Skip consent dialog when onboarding already granted full access
- Shrink popover whitespace, show reset times, hide loadout plugin row
- Record declined full-agent consent so dialog does not reappear on restart
- Constrain bar row height to 18pt with vertical centering
- Dispatch config patch via `_checkConfig_` to fix show_sonnet toggle
- Replace sleep yield with poll loop in `applyConfigPatch_` handler
- Force `NSStatusBarButton` redraw after image dimension change
- Avoid deadlock in dashboard config patch dispatch
- Use `Any` type annotation on `applyConfigPatch_` to fix PyObjC selector dispatch
- Guard animation timer against overwriting menubar in idle/done phases
- Bypass animation guard on config changes so menubar settings apply immediately
- Apply config patch side effects directly from in-memory config
- Center coin icon on transparent canvas to fix NSAlert dialog size
- Show penny icon in NSAlert dialogs for accessory-mode app
- Use gravity-areas distribution to remove extra vertical space in root stack
- Reduce initial frame height and relayout on load
- Refresh menubar immediately on config hot-reload
- Tooltip text says "Resets at" not "Resets"
- Parse multi-line `/status` Usage tab format correctly

### Changed

- Plugin architecture rules for core/plugin separation documented
- Simplified CLAUDE.md to template includes

## [0.3.0b1] - 2026-03-18

### Added

- Release agent auto-evaluates whether a release is needed before cutting one

### Fixed

- Update button now opens Terminal with the `penny update` command
- Squash-merge release PRs to avoid duplicate release commits on main

## [0.2.0b1] - 2026-03-18

### Added

- Auto-install missing `pexpect`/`pyte` dependencies at startup via `penny/deps.py`
- Commit agent for automated conventional commits that push branches and open PRs

### Fixed

- Outage warning now only shown for confirmed API errors — previously Penny showed "Calibrating" during Claude API outages
- Removed JSONL budget estimation fallback; `build_prediction()` always uses live `/status` data

### Changed

- Updated commit and release agents for PR-based workflow

## [0.1.0] - 2026-03-12

Initial public release.

### Added

- macOS menu bar app with real-time Claude Code token monitoring
- Session and weekly budget tracking with progress bars and reset countdowns
- 90th-percentile capacity prediction with unused budget estimation
- Live analytics dashboard at `127.0.0.1:7432` with model breakdown, cache efficiency, tool usage, session history, and activity by hour
- Self-contained HTML report with weekly usage history
- `penny` CLI for service control (`start`, `stop`, `restart`, `status`), data access (`refresh`, `open`, `logs`), configuration (`prefs`), and self-updating (`update`)
- `penny update` command for self-updating via git pull + install.sh
- In-app update checker with GitHub Release detection (checks ~1x/day)
- Update available banner in popover UI with Update and Dismiss buttons
- macOS notification when a new version is available
- Onboarding wizard for first-run setup
- Pre-flight validation checks (tools, auth, config)
- Config hot-reload — changes to `config.yaml` take effect within 5 seconds
- One-line installer via curl with native launcher compilation and launchd registration

### Security

- Dashboard server binds to `127.0.0.1` only — no external access
- Config loaded with `yaml.safe_load()`
- All data stays local — nothing is transmitted externally
