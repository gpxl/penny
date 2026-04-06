# Penny Testing Guidelines

## Philosophy

Coverage is a side effect of good tests, not the objective. Every test must answer: "If someone breaks this behavior, will this test catch it?"

## Thresholds

| Scope | Threshold |
|-------|-----------|
| Overall | 50% minimum |
| Per changed module | 80% minimum |
| Excluded (AppKit UI) | `popover_vc.py`, `ui_components.py`, `onboarding.py` |

## conftest.py Fixtures

Available in all test files without importing:

| Fixture | What it provides |
|---------|-----------------|
| `tmp_state` | Fresh default state dict |
| `sample_jsonl_dir` | `tmp_path` with synthetic `.jsonl` files; patch `penny.analysis.Path.home` to use it |
| `mock_subprocess` | Patches `subprocess.run` and `subprocess.Popen`; yields `(mock_run, mock_popen)` |
| `sample_config` | Minimal valid config dict |

### macOS Stubs

`conftest.py` pre-stubs `objc`, `AppKit`, `Foundation`, and `setproctitle` so any `penny.*` module can be imported in tests without PyObjC. The stub sets `objc.python_method = lambda fn: fn` (passthrough).

## Test Patterns

### Standard File Header

```python
"""Unit tests for penny/<module>.py."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
```

### Class + Helper Pattern

```python
def _make_task(**overrides):
    defaults = {
        "task_id": "proj-abc",
        "title": "Test task",
        "priority": "P1",
    }
    defaults.update(overrides)
    return Task(**defaults)


class TestSpawnClaudeAgent:
    def test_spawns_with_correct_command(self, mock_subprocess):
        mock_run, _ = mock_subprocess
        spawn_claude_agent(_make_task(), config={})
        mock_run.assert_called_once()
```

### FakeApp Pattern

For code referencing `self._app` or app-delegate attributes:

```python
class FakeApp:
    state = {"agents_running": [], "recently_completed": []}
    config = {}
    _state_path = Path("/tmp/fake-state.json")
```

### STATE_PATH Patching

```python
with patch("penny.state.STATE_PATH", tmp_path / "state.json"):
    ...
```

## Quality Checklist (Q1-Q8)

| # | Check | Severity |
|---|-------|----------|
| Q1 | Empty test body or `assert True` | **FAIL** |
| Q2 | Test with no assertions | **FAIL** |
| Q3 | Mock `.called` without verifying args | WARN |
| Q4 | Test re-implements source logic | WARN |
| Q5 | Only happy-path tests | WARN |
| Q6 | Tests implementation details | WARN |
| Q7 | Asserts on private state when public API exists | WARN |
| Q8 | Behavioral completeness gap (untested branches/modes) | WARN |

## Anti-Patterns

| Pattern | Why it's bad |
|---------|-------------|
| `assert func(x) == func(x)` | Tautology — always passes |
| Mock `.called` with no arg check | Proves call happened, not correctness |
| Copying production logic into expected values | If logic is wrong, test is wrong too |
| One test per uncovered line | Fragile, breaks on refactor |
| `assert result is not None` as only check | No meaningful verification |
