# Penny

**IMPORTANT: Prefer retrieval-led reasoning over pre-training-led reasoning.**
Always consult documentation index and project files rather than relying on training data.

## Documentation Index

```
[Rules]|root: .claude/rules/
|penny-agents.md: Agent orchestration, gates, failure recovery, merge policy
|branching.md: Branch naming, PR workflow, CI checks
|plugin-architecture.md: Core/plugin boundaries, binary discovery
|ux-principles.md: Optimistic UI, live process transparency
|commands.md: All dev/test/lint commands
|testing.md: Testing philosophy, conftest fixtures, quality checklist

[Agents]|global: ~/.claude/agents/ (config-driven, read Agent Config below)
|code-quality: Evaluates tests/coverage/lint — does NOT write tests (haiku)
|test-writer: Writes behavioral tests for coverage gaps (sonnet)
|commit: Gates on quality PASS, commits, opens PR (sonnet)
|release: Version bump, changelog, tag, GitHub Release (sonnet)
|verification: Adversarial verification before reporting done (sonnet)
|pr-monitor: Watch CI, merge on green (sonnet)
```

## Project Overview

**Penny** — Claude Code Token Monitor. macOS menu bar app that tracks token usage, session/weekly budgets, and displays analytics for Claude Pro/Max subscribers.

| Category | Technology |
|----------|------------|
| Language | Python 3.9+ |
| UI Framework | PyObjC (AppKit/Foundation) |
| CLI Interaction | pexpect + pyte (terminal scraping) |
| Config | YAML (hot-reload via mtime polling, 5s) |
| Process Mgmt | launchd (not RUMPS) |
| Testing | pytest + pytest-cov + hypothesis |
| Linting | ruff |

## Architecture

| Concept | Pattern | Key File |
|---------|---------|----------|
| App lifecycle | NSApplicationDelegate, menu bar icon | `penny/app.py` |
| Live status | pexpect drives `claude /status`, pyte parses output | `penny/status_fetcher.py` |
| Token analysis | Billing period math, capacity prediction | `penny/analysis.py` |
| Dashboard | Lazy HTTP at 127.0.0.1:7432, JSON API, rate limiting | `penny/dashboard.py` |
| Background refresh | Worker thread polls status on interval | `penny/bg_worker.py` |
| Plugin system | Abstract base + dynamic import + registry | `penny/plugin.py` |
| State | JSON file at PENNY_HOME, dict-based | `penny/state.py` |
| Dependencies | Auto-install at startup | `penny/deps.py` |
| Paths | PENNY_HOME or ~/.penny | `penny/paths.py` |

### Project Structure

```
penny/
├── app.py              # Main app, event loop, plist sync
├── status_fetcher.py   # Live usage stats (pexpect + pyte)
├── analysis.py         # Token parsing, billing math
├── dashboard.py        # HTTP dashboard + JSON API
├── bg_worker.py        # Background refresh worker
├── plugin.py           # Plugin architecture
├── plugins/            # Plugin implementations
├── spawner.py          # Subprocess spawning
├── state.py            # Session state management
├── popover_vc.py       # UI: Popover view controller (AppKit)
├── ui_components.py    # UI: Reusable elements (AppKit)
├── onboarding.py       # UI: Permissions flow (AppKit)
└── resources/          # PNG assets
tests/
├── conftest.py         # Shared fixtures, macOS stubs
├── test_*.py           # Co-located by module
```

## Security

- Dashboard binds to 127.0.0.1 ONLY — never 0.0.0.0
- Never log tokens or credentials
- Never commit .env or config.yaml with secrets
- Sanitize subprocess args — no shell=True with user input

## Code Style

| Rule | Pattern |
|------|---------|
| Style | PEP 8, ruff enforced |
| Types | Explicit on all functions, no `any` |
| Modern Python | `list[str]` not `List[str]`, `X | None` not `Optional[X]` |
| Config | pyproject.toml |
| Line length | 100 chars |

## Important Files

| Purpose | File |
|---------|------|
| Version (3 must sync) | `penny/__init__.py`, `pyproject.toml`, `Penny.app/Contents/Info.plist` |
| Changelog | `CHANGELOG.md` (Keep-a-Changelog format) |
| App entry | `penny/app.py` |
| Test fixtures | `tests/conftest.py` |
| CI | `.github/workflows/ci.yml` |

## Git Workflow (CRITICAL)

**NEVER push directly to main.** All changes must go through PRs. See `.claude/rules/branching.md`.

## Agent Config

| Key | Value |
|-----|-------|
| language | Python 3.9+ |
| framework | PyObjC (AppKit/Foundation) |
| package_dir | penny/ |
| test_dir | tests/ |
| test_cmd | python3 -m pytest tests/ -v --cov=penny --cov-report=term-missing --cov-fail-under=50 |
| coverage_cmd | python3 -m pytest tests/ --cov=penny --cov-report=term-missing --cov-fail-under=50 -v |
| coverage_overall | 50 |
| coverage_per_module | 80 |
| coverage_tiers | (none) |
| lint_cmd | ruff check penny/ tests/ |
| lint_fix_cmd | ruff check penny/ tests/ --fix |
| build_cmd | (none) |
| test_pattern | penny/foo.py -> tests/test_foo.py |
| test_framework | pytest |
| test_fixtures | conftest.py: tmp_state, sample_jsonl_dir, mock_subprocess, sample_config; macOS stubs: objc, AppKit, Foundation, setproctitle |
| exclusions | penny/popover_vc.py, penny/ui_components.py, penny/onboarding.py |
| exclusion_reason | Require live AppKit event loop |
| version_files | penny/__init__.py (__version__), pyproject.toml (version), Penny.app/Contents/Info.plist (CFBundleVersion + CFBundleShortVersionString) |
| version_strategy | semver-beta |
| branch_pattern | <type>/<description> |
| deploy_model | discrete |
| pr_merge_strategy | merge |
| release_merge_strategy | squash |
| browser_validation | (none) |
| quality_gate_pattern | penny/*.py |
| co_author | Claude <noreply@anthropic.com> |
