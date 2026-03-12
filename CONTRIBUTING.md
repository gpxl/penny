# Contributing

## Prerequisites

- macOS 12+
- Python 3.9+
- `claude` CLI (authenticated via `claude auth login`)

## Dev Setup

```bash
git clone https://github.com/gpxl/penny.git
cd penny
pip install -e ".[dev]"
# or:
pip install -e . && pip install pytest ruff
```

## Running Tests

```bash
pytest
```

This runs all unit and integration tests in `tests/`.

## Code Style

Penny uses [ruff](https://docs.astral.sh/ruff/) with `line-length = 100` and `target-version = "py39"`. Run before committing:

```bash
ruff check penny/
```

## Branch Naming

| Prefix | Use for |
|--------|---------|
| `feat/<description>` | New features |
| `fix/<description>` | Bug fixes |
| `chore/<description>` | Maintenance, dependency updates |
| `docs/<description>` | Documentation only |

## Commit Messages

Use [Conventional Commits](https://www.conventionalcommits.org/) format:

```
feat: add support for multiple config files
fix: prevent duplicate task spawning
chore: bump ruff to 0.4.0
docs: add API reference for /api/state
test: cover spawner credential isolation
security: restrict env passthrough allowlist
```

## Pull Requests

Before opening a PR:

1. All tests must pass: `pytest`
2. Lint must pass: `ruff check penny/`
3. Add an entry under `[Unreleased]` in `CHANGELOG.md`
4. For non-trivial changes, bump the version in both `penny/__init__.py` and `pyproject.toml`

## Plugin Architecture

Plugin infrastructure exists in the codebase (`penny/plugin.py`, `penny/plugins/`) but is dormant in the current release. The Beads integration lives on the `feature/beads-plugin` branch. New integrations can be added as plugins following the `PennyPlugin` ABC in `penny/plugin.py`.
