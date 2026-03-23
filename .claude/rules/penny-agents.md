# Penny Agent Workflow

## Agents

Penny has three agents for automated workflows:

| Agent | Trigger | What it does |
|-------|---------|-------------|
| `code-quality` | After logic changes | Runs tests, checks coverage, lints |
| `commit` | "commit", "push", "open a PR" | Commits, pushes branch, opens PR — **never merges** |
| `release` | "release", "cut a release" | Evaluates if release needed, bumps version, tags, publishes |

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

## Workflow Summary

```
code change → code-quality agent → commit agent (push + PR) → user merges → release agent (evaluate)
```

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
