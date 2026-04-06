---
name: penny-close
description: Session close protocol for Penny. Run before completing work to ensure tests pass, lint is clean, and changes are committed and pushed via PR.
user_invocable: true
---

# Penny Close

Session close protocol. Run this before saying "done" or "complete".

## Checklist

Execute each step in order. Do not skip steps.

### 1. Lint

```bash
ruff check penny/ tests/
```

Fix any failures before proceeding.

### 2. Tests

```bash
python -m pytest tests/ --cov=penny --cov-report=term-missing --cov-fail-under=50 -v
```

Fix any failures before proceeding.

### 3. Check Changes

```bash
git status
git diff --stat
```

If no changes to commit, skip to step 7.

### 4. Stage and Commit

Stage specific files (never `git add -A`):

```bash
git add <specific files>
git commit -m "$(cat <<'EOF'
<type>: <description>

Co-Authored-By: Claude <noreply@anthropic.com>
EOF
)"
```

### 5. Sync Beads

```bash
bd sync
```

### 6. Push

If not on a feature branch, create one first:

```bash
git checkout -b claude/<description>
```

Push and create PR:

```bash
git push -u origin <branch-name>
gh pr create --title "<title>" --body "$(cat <<'EOF'
## Summary
<bullet points>

## Test plan
- [x] ruff lint clean
- [x] pytest passes (coverage >= 50%)
- [ ] Manual verification

Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

### 7. Close Issues

Close any completed beads issues:

```bash
bd close <id1> <id2> ...
bd sync
```

### 8. Report

```
SESSION CLOSE: COMPLETE
- Lint: clean
- Tests: passed
- Branch: <branch-name>
- PR: <PR URL>
- Issues closed: <list>
```

**Work is NOT complete until `git push` succeeds.**
