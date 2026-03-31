---
name: penny-verify
description: Quick verification workflow for Penny. Runs ruff lint, pytest, and confirms quality before committing.
user_invocable: true
---

# Penny Verify

Quick quality check during development. Run this before committing.

## Steps

Run these commands in sequence. Stop on first failure.

### 1. Lint

```bash
ruff check penny/ tests/
```

If lint fails, auto-fix safe rules:

```bash
ruff check penny/ tests/ --fix
ruff check penny/ tests/
```

Report any remaining lint errors and stop.

### 2. Tests with Coverage

```bash
python -m pytest tests/ --cov=penny --cov-report=term-missing --cov-fail-under=50 -v
```

Report any test failures and stop.

### 3. Summary

Report results:

```
VERIFY RESULT: PASS
- Lint: clean
- Tests: X passed
- Coverage: Y% overall
```

Or on failure:

```
VERIFY RESULT: FAIL
- <what failed and why>
```
