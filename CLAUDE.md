# Penny — Claude Code Instructions

## UX Rule (CRITICAL)
Prefer configuration through config.yaml and the menubar/dashboard UI.
Users should NOT need terminal commands for normal operation.
- Service control → config.yaml `service:` section + popover toggles
- Project setup → config.yaml (opened via Preferences button)
- Restart after stop → ~/Applications/Penny.app (Spotlight/Finder) or `penny start`
- Terminal is acceptable ONLY for the one-time install.sh setup step

## Key Files
| File | Purpose |
|------|---------|
| penny/app.py | NSApplicationDelegate, config loading, service sync, spawning |
| penny/popover_vc.py | All UI — programmatic NSStackView, no NIB |
| penny/paths.py | data_dir() → PENNY_HOME or ~/.penny |
| penny/dashboard.py | Lazy HTTP server at 127.0.0.1:7432 |
| penny/update_checker.py | GitHub release checker, version comparison, state helpers |
| install.sh | One-time install + plist generation |
| config.yaml | Runtime config (projects, triggers, work, service) |
| scripts/penny | CLI: penny start|stop|status|update |

## Architecture Notes
- Config path: data_dir() / "config.yaml"
- Plist locations (both must stay in sync):
  - Source: $SCRIPT_DIR/com.gpxl.penny.plist
  - Active: ~/Library/LaunchAgents/com.gpxl.penny.plist
- SCRIPT_DIR readable from plist WorkingDirectory key at runtime
- Config hot-reloads automatically — _checkConfig_ polls mtime every 5s, _hot_reload_config applies changes
- self._vc._app = self → VC has a reference to the app delegate

## Tech Stack
Python 3.9+, PyObjC, AppKit/Foundation, yaml, launchd (no RUMPS)

## Code Quality Agent

After implementing any non-trivial change, delegate quality verification to the
code quality agent instead of running checks manually.

### When to delegate

| Trigger | Example |
|---------|---------|
| New module created | Added `penny/rate_limiter.py` |
| Logic change in existing module | Modified `penny/spawner.py` |
| Bug fix | Fixed off-by-one in `penny/analysis.py` |
| Refactor | Extracted helper from `penny/app.py` |

UI-only changes to `popover_vc.py`, `ui_components.py`, or `onboarding.py`
do **not** need delegation — those modules are excluded from coverage.

### How to delegate

Use the Claude Code `Agent` tool with `subagent_type="code-quality"`. Pass only
the changed files — the agent's system prompt comes from the spec automatically:

```
Changed files: penny/spawner.py, penny/tasks.py
```

Do **not** use `isolation="worktree"` — the agent intentionally writes test
files to the working tree; isolation would strand those files on a separate
branch.

> ⚠️ Sub-agents cannot spawn code-quality. If you are running as a sub-agent,
> do **not** run `pytest` manually or attempt to call code-quality. Simply
> return your results. The top-level session will delegate to code-quality
> after you return.

### Interpreting the result

The agent ends its response with a `CODE QUALITY RESULT: PASS` or
`CODE QUALITY RESULT: FAIL` block. On PASS, close the beads issue and continue.
On FAIL, read the details — if pre-existing tests broke, investigate before
closing; if coverage is the issue, the agent will have added tests already.

## Commit Agent

Stages, commits, pushes branch, and opens a PR on GitHub using Conventional
Commits format. Runs tests and lint before pushing.

### When to invoke

| Trigger | Example |
|---------|---------|
| User says "commit", "push", "open a PR" | `"commit and push"`, `"open a PR"` |

### How to invoke

Use the Claude Code `Agent` tool with `subagent_type="commit"`:

```
Commit and push the current changes.
```

### Interpreting the result

The agent ends with `COMMIT RESULT: PASS` or `COMMIT RESULT: FAIL`.
On PASS, the branch is pushed and a PR is open. Run the release agent next.
On FAIL, no push was made — read the details to fix the issue.

## Release Agent

Merges a ready PR to main, then cuts a release (changelog, version sync,
tag, push, GitHub Release). Checks CI status before merging.

### When to invoke

| Trigger | Example |
|---------|---------|
| User says "cut a release" | `"cut a release"`, `"release"`, `"merge and release"` |

### How to invoke

Use the Claude Code `Agent` tool with `subagent_type="release"`:

```
Cut a new release for Penny.
```

### Interpreting the result

The agent ends with `RELEASE RESULT: PASS` or `RELEASE RESULT: FAIL`.
On PASS, the PR is merged, tag is pushed, and the GitHub Release is live.
On FAIL, read the details — the PR may have failing checks or need attention.
