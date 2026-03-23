# Branch Workflow (MANDATORY)

## NEVER push directly to main

All changes must go through pull requests. Branch protection enforces this.

## NEVER merge PRs without user approval

After creating a PR, **stop and report the PR URL**. Do not merge.
The user will review and decide when to merge. This applies to all agents
(commit, release, code-quality) — none may call `gh pr merge` autonomously.

The only exception is the release agent merging a **release PR** (e.g.,
`release/vX.Y.Z`) that it created itself, after CI passes.

### Workflow

1. Create a branch from main:
   ```bash
   git checkout -b <prefix>/<descriptive-name>
   ```

2. Make changes, commit to the branch

3. Push the branch:
   ```bash
   git push -u origin <prefix>/<descriptive-name>
   ```

4. Create a PR:
   ```bash
   gh pr create --title "..." --body "..."
   ```

5. **Stop.** Report the PR URL and wait for user to approve/merge.

### Branch Naming

| Actor | Pattern | Example |
|-------|---------|---------|
| Claude Code | `claude/<description>` | `claude/add-scan-endpoint` |
| Developer | `feat/<description>`, `fix/<description>` | `fix/multiline-parsing` |

### CI Required Checks

All must pass before merging:

| Check | What It Validates |
|-------|-------------------|
| `Lint` | ruff passes with 0 errors |
| `Test (Python 3.11)` | All pytest suites pass |
| `Test (Python 3.12)` | All pytest suites pass |
