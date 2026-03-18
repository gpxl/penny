# Penny Agent Workflow

## Agents

Penny has three agents for automated workflows:

| Agent | Trigger | What it does |
|-------|---------|-------------|
| `code-quality` | After logic changes | Runs tests, checks coverage, lints |
| `commit` | "commit", "push", "open a PR" | Commits, pushes branch, opens PR |
| `release` | "release", "cut a release", or after merging a PR | Evaluates if release needed, merges PR, tags, publishes |

## Post-Merge Release Check (CRITICAL)

After merging any PR to main, **always invoke the release agent** to evaluate
whether a release should be cut. Do not ask the user — just run it.

The release agent will:
- Check if there are `feat:` or `fix:` commits since the last tag
- If yes → cut a release automatically
- If no → report `RELEASE RESULT: SKIP` (no action needed)

This ensures releases happen promptly after meaningful changes land on main.

## Workflow Summary

```
code change → code-quality agent → commit agent (push + PR) → merge → release agent (auto-evaluate)
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
