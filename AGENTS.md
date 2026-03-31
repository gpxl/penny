# Agent Instructions

This project uses **bd** (beads) for issue tracking. Run `bd onboard` to get started.

## Quick Reference

```bash
bd ready              # Find available work
bd show <id>          # View issue details
bd update <id> --status in_progress  # Claim work
bd close <id>         # Complete work
bd sync               # Sync with git
```

## Build/Lint/Test Commands

```bash
# Testing
python -m pytest tests/ -v                              # All tests
python -m pytest tests/ --cov=penny --cov-report=term-missing --cov-fail-under=50 -v  # With coverage
python -m pytest tests/test_spawner.py -v               # Single file
python -m pytest tests/ -x                              # Stop on first failure

# Linting
ruff check penny/ tests/                                # Check
ruff check penny/ tests/ --fix                          # Auto-fix
ruff format penny/                                      # Format

# Run app
python -m penny                                         # Start Penny
```

## Tech Stack

- **Language**: Python 3.9+
- **UI**: PyObjC (AppKit/Foundation) — no RUMPS
- **CLI Interaction**: pexpect + pyte (terminal scraping)
- **Config**: YAML (hot-reload via 5s mtime poll)
- **Testing**: pytest + pytest-cov + hypothesis
- **Linting**: ruff (E, F, I, UP, B rules)

## Code Style

- PEP 8 enforced by ruff
- Modern types: `list[str]`, `X | None` (not `List[str]`, `Optional[X]`)
- Type hints on all functions
- 100 char line width
- `from __future__ import annotations` in all files

## Agent Workflow

```
code change → code-quality (evaluate) → FAIL? → test-writer (fix) → code-quality (re-verify)
                                       → PASS  → commit agent → push + PR
                                                                → release agent (if qualifying commits)
```

| Agent | Trigger | Does | Does NOT |
|-------|---------|------|----------|
| code-quality | After logic changes | Evaluate coverage, lint, Q1-Q8 quality | Write tests |
| test-writer | After code-quality FAIL | Write/fix behavioral tests | Run coverage, lint |
| commit | "commit", "push", "PR" | Gate on PASS, commit, push, open PR | Merge PRs |
| release | "release", "cut release" | Bump version, tag, GitHub Release | Merge feature PRs |

## conftest.py Fixtures

| Fixture | What it provides |
|---------|-----------------|
| `tmp_state` | Fresh default state dict |
| `sample_jsonl_dir` | tmp_path with synthetic .jsonl files |
| `mock_subprocess` | Patches subprocess.run and Popen; yields (mock_run, mock_popen) |
| `sample_config` | Minimal valid config dict |

macOS stubs: conftest pre-stubs `objc`, `AppKit`, `Foundation`, `setproctitle` — any `penny.*` module can be imported in tests without PyObjC.

## Coverage

- **Overall threshold**: 50%
- **Per-module threshold**: 80% (for changed modules)
- **Excluded from coverage**: `popover_vc.py`, `ui_components.py`, `onboarding.py` (require live AppKit)

## Landing the Plane (Session Completion)

1. Create issues for remaining work (`bd create`)
2. Run quality gates: `ruff check penny/ tests/ && python -m pytest tests/ -v --cov=penny --cov-fail-under=50`
3. Close finished issues (`bd close <id>`)
4. Push to remote:
   ```bash
   bd sync
   git push
   git status  # MUST show "up to date with origin"
   ```

**Work is NOT complete until `git push` succeeds.**
