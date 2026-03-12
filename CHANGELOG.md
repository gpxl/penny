# Changelog

All notable changes to this project will be documented in this file. The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
