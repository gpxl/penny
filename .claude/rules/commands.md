# Penny Commands Reference

## Development

```bash
# Run the app
python -m penny

# Check dependencies
python -c "import penny; print(penny.__version__)"
```

## Testing

```bash
# Full suite with coverage (matches CI)
python -m pytest tests/ --cov=penny --cov-report=term-missing --cov-fail-under=50 -v

# Single file
python -m pytest tests/test_spawner.py -v

# Stop on first failure
python -m pytest tests/ -x

# With hypothesis verbose
python -m pytest tests/ -v --hypothesis-show-statistics

# Coverage for specific module
python -m pytest tests/test_analysis.py --cov=penny.analysis --cov-report=term-missing -v
```

**Note:** Always use `python -m pytest`, never bare `pytest`.

## Linting

```bash
# Check for issues
ruff check penny/ tests/

# Auto-fix safe rules
ruff check penny/ tests/ --fix

# Format
ruff format penny/

# Check format without changing
ruff format penny/ --check
```

## Beads (Task Management)

```bash
bd ready                    # Find available work
bd show <id>                # View issue details
bd update <id> --status=in_progress  # Claim work
bd close <id>               # Complete work
bd close <id1> <id2> ...    # Close multiple
bd sync                     # Sync with git
bd create --title="..." --type=task --priority=2
```

## Git Workflow

```bash
# Create feature branch
git checkout -b claude/<description>

# Push and create PR
git push -u origin claude/<description>
gh pr create --title "..." --body "..."

# Never push directly to main
```

## CI Checks

All three must pass before merging:

| Check | Command |
|-------|---------|
| Lint | `ruff check penny/ tests/` |
| Test (3.11) | `python -m pytest tests/ -v --cov=penny --cov-fail-under=50` |
| Test (3.12) | `python -m pytest tests/ -v --cov=penny --cov-fail-under=50` |
