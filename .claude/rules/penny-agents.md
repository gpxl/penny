# Penny Agent Workflow

## Agents

Penny has four agents for automated workflows:

| Agent | Trigger | What it does |
|-------|---------|-------------|
| `code-quality` | After logic changes | Evaluates tests, coverage (80%/module), lint, test quality — **does not write tests** |
| `test-writer` | After code-quality FAIL | Writes/fixes tests for coverage gaps and quality failures |
| `commit` | "commit", "push", "open a PR" | Gates on code-quality PASS, verifies per-module coverage, commits, pushes, opens PR — **never merges** |
| `release` | "release", "cut a release" | Evaluates if release needed, bumps version, tags, publishes |

## Enforcement Rules (CRITICAL)

### Code-Quality Gate

The code-quality agent **must** run before the commit agent for any change
that touches `penny/*.py` (excluding UI modules). This is not optional.

| Rule | Detail |
|------|--------|
| **REQUIRED** | Run code-quality agent before invoking commit agent |
| **REQUIRED** | Include `CODE QUALITY RESULT: PASS` output when delegating to commit |
| **BLOCKED** | Commit agent will refuse to proceed without code-quality evidence |
| **EXCEPTION** | Changes that ONLY touch tests, docs, config, or CI are exempt |

### Failure Recovery

When code-quality reports FAIL:

1. Invoke the **test-writer** agent with the code-quality report
2. Re-run **code-quality** to verify the fixes
3. If PASS → proceed to commit
4. If FAIL again → stop and ask the user for guidance (max 1 retry)

### Warning Tracking

When code-quality reports PASS with test quality warnings (Q3-Q8):

1. Create beads tasks for each warning (`bd create --type=task --priority=3`)
2. Proceed to commit — warnings do not block
3. Warnings become backlog items tracked in beads for future cleanup

### Workflow

```
code change → code-quality (evaluate) → FAIL? → test-writer → code-quality (re-verify)
                                       → PASS  → bd create tasks for warnings → commit (gate + ship) → user merges → release
```

The commit agent independently verifies per-module coverage for changed
modules (80% threshold) as defense-in-depth. It will not proceed without
a prior `CODE QUALITY RESULT: PASS` for changes touching `penny/*.py`.

## Merge Policy (CRITICAL)

**No agent may merge a feature PR autonomously.** After pushing a branch and
opening a PR, stop and report the PR URL. The user decides when to merge.

The release agent may only merge **release PRs** (`release/vX.Y.Z`) that it
created itself, after CI passes.

## Post-Merge Release Check

After the user merges a PR to main, invoke the release agent to evaluate
whether a release should be cut.

The release agent will:
- Check if there are `feat:` or `fix:` commits since the last tag
- If yes → cut a release
- If no → report `RELEASE RESULT: SKIP` (no action needed)

## Key Files

| File | Purpose |
|------|---------|
| penny/app.py | NSApplicationDelegate, config loading, service sync, spawning |
| penny/popover_vc.py | All UI — programmatic NSStackView, no NIB |
| penny/paths.py | data_dir() → PENNY_HOME or ~/.penny |
| penny/deps.py | Auto-install missing dependencies at startup |
| penny/status_fetcher.py | Live /status scraping via pexpect + pyte |
| penny/dashboard.py | Lazy HTTP server at 127.0.0.1:7432 |
| install.sh | One-time install + plist generation |
| config.yaml | Runtime config (projects, triggers, work, service) |

## Architecture Notes

- Tech: Python 3.9+, PyObjC, AppKit/Foundation, yaml, launchd (no RUMPS)
- Config path: data_dir() / "config.yaml"
- Config hot-reloads automatically — polls mtime every 5s
- fetch_live_status() always returns LiveStatus (never None)
- outage=True only for confirmed API errors, not transient failures
