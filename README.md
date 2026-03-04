# Nae Nae

macOS menu bar app that monitors your Claude Max token usage, predicts unused weekly capacity, and autonomously spawns Claude Code agents on [Beads](https://github.com/beads-cli/beads) tasks before your billing period resets.

---

## What it does

1. **Token monitoring** — reads `~/.claude/stats-cache.json` every 5 minutes and displays current and projected weekly usage in the menu bar.
2. **Capacity prediction** — uses a 90th-percentile model to estimate how much of your Claude Max budget will go unused by the end of the week.
3. **Autonomous agent spawning** — when predicted unused capacity is ≥ 30% and ≤ 2 days remain, it automatically runs `claude --dangerously-skip-permissions -p` for each ready Beads task across your configured projects.
4. **Completion tracking** — detects when spawned agents finish and sends macOS notifications.
5. **Weekly report** — generates a self-contained HTML report with an SVG usage chart.

## Who it's for

Claude Max subscribers who also use [Beads](https://github.com/beads-cli/beads) (`bd`) for task management and want to put spare weekly capacity to work automatically.

---

## Requirements

| Requirement | Notes |
|---|---|
| macOS 12+ | Menu bar requires macOS |
| Python 3.9+ | `python3 --version` to check |
| `claude` CLI (authenticated) | npm install -g @anthropic-ai/claude-code |
| `bd` CLI | npm install -g beads-cli |
| Claude Max subscription | Required for the usage stats this app reads |

---

## Installation

### Option 1 — curl one-liner (recommended)

```bash
curl -fsSL https://raw.githubusercontent.com/gpxl/naenae/main/install.sh | bash
```

This clones the repo to `~/.naenae/src/` and runs the installer automatically.

### Option 2 — pipx

```bash
pipx install git+https://github.com/gpxl/naenae.git
```

Then register the launchd service manually:

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/gpxl/naenae/main/install.sh)
```

### Option 3 — Homebrew tap

```bash
brew tap gpxl/naenae
brew install naenae
brew services start naenae
```

> See [Homebrew tap](#homebrew-tap) below for the publish workflow.

---

## First-run setup

Complete all steps before Nae Nae can run agents:

1. **Install claude CLI**
   ```bash
   npm install -g @anthropic-ai/claude-code
   ```

2. **Authenticate with Anthropic**
   ```bash
   claude auth login
   ```

3. **Install beads CLI**
   ```bash
   npm install -g beads-cli
   ```

4. **Init beads in your project(s)**
   ```bash
   cd ~/Documents/GitHub/your-repo
   bd init
   ```

5. **Edit config** — the installer creates `~/.naenae/config.yaml` from the template. Replace the placeholder path:
   ```bash
   open ~/.naenae/config.yaml
   ```

6. **Verify setup**
   ```bash
   bash ~/.naenae/src/install.sh --check
   ```

7. **Start the service**
   ```bash
   launchctl load ~/Library/LaunchAgents/com.gpxl.naenae.plist
   ```

> The installer auto-defers step 7 when it creates a fresh config, so you won't accidentally start with unconfigured placeholders.

---

## Config reference

Config file: `~/.naenae/config.yaml` (or `$NAENAE_HOME/config.yaml`)

| Key | Type | Default | Description |
|---|---|---|---|
| `projects[].path` | string | — | Absolute or `~/`-relative path to a git repo with `.beads/` |
| `projects[].priority` | int | — | Lower = higher priority; tasks from lower-numbered projects spawn first |
| `trigger.min_capacity_percent` | int | 30 | Spawn only if ≥ N% of weekly budget predicted unused |
| `trigger.max_days_remaining` | int | 2 | Spawn only if ≤ N days remain in the billing week |
| `work.max_agents_per_run` | int | 2 | Maximum agents to spawn per 4-hour cycle |
| `work.task_priority_levels` | list | [P1, P2, P3] | Beads priority labels to include |
| `notifications.spawn` | bool | true | macOS notification when agents are spawned |
| `notifications.completion` | bool | true | macOS notification when an agent finishes |
| `notifications.weekly_summary` | bool | true | Weekly summary notification |
| `stats_cache_path` | string | `~/.claude/stats-cache.json` | Path to Claude Code stats cache |

---

## Troubleshooting

### ⚠ in menu bar / "Setup Required" alert

Open **Setup Issues…** from the menu to see a full list with fix hints. Common causes:

- `claude` or `bd` not in launchd PATH → re-run `bash install.sh` after installing the tools
- Config still has `PLACEHOLDER_PROJECT_PATH` → edit `~/.naenae/config.yaml`
- No `.beads/` directory in a project → run `bd init` inside the repo

### `claude` or `bd` not found under launchd

launchd uses a minimal PATH that often omits npm binary directories. Re-running `install.sh` after installing the tools injects their directories into the plist automatically:

```bash
bash ~/.naenae/src/install.sh
```

### No tasks appearing

- Confirm `bd ready` returns tasks when run manually in the project directory.
- Check that `bd` is accessible (see above).
- Verify `trigger.min_capacity_percent` and `trigger.max_days_remaining` thresholds; use **Run Now** to force a cycle.

### Auth / "claude not authenticated" errors

```bash
claude auth login
claude --version   # should exit 0
```

### Config changes not taking effect

Changes to `config.yaml` are picked up on the next 4-hour cycle or immediately via **Run Now**. You do not need to restart the service.

### Viewing logs

```bash
tail -f ~/.naenae/logs/launchd.log          # service stdout/stderr
tail -f ~/.naenae/logs/agent-*.log          # per-agent logs
```

### Uninstall

```bash
launchctl unload ~/Library/LaunchAgents/com.gpxl.naenae.plist
rm ~/Library/LaunchAgents/com.gpxl.naenae.plist
rm -rf ~/.naenae/src   # if installed via curl
# Optionally remove data:
rm -rf ~/.naenae
```

---

## Homebrew tap

Homebrew taps must live in a **separate GitHub repository** named `homebrew-<tap-name>`. The formula file in this repo (`Formula/naenae.rb`) is a reference copy only.

### One-time tap repo setup

1. Create `gpxl/homebrew-naenae` on GitHub (public repo).
2. Clone it locally:
   ```bash
   git clone https://github.com/gpxl/homebrew-naenae ~/homebrew-naenae
   mkdir -p ~/homebrew-naenae/Formula
   ```
3. After your first release (see below), copy the updated formula and push:
   ```bash
   cp Formula/naenae.rb ~/homebrew-naenae/Formula/naenae.rb
   cd ~/homebrew-naenae && git add Formula/naenae.rb && git commit -m "naenae 0.1.0" && git push
   ```

### Publish workflow

Use `scripts/release.sh` to cut a release in one command:

```bash
bash scripts/release.sh 0.2.0
```

This script:
1. Validates the working tree is clean
2. Bumps the version in `pyproject.toml` and commits it
3. Tags `vX.Y.Z` and pushes branch + tag to GitHub
4. Downloads the release archive and computes its sha256
5. Updates `Formula/naenae.rb` with the new url and sha256
6. Prints copy-paste instructions to update the tap repo

Then follow the printed instructions to push the tap.

**Dry-run (no push):**
```bash
bash scripts/release.sh --dry-run 0.2.0
```

**Regenerating dependency sha256s** (only needed when pinned dep versions change):
```bash
brew update-python-resources gpxl/naenae/naenae
```

---

## Architecture

| Module | Purpose |
|---|---|
| `naenae/app.py` | PyObjC/AppKit app, menus, timers, orchestration |
| `naenae/analysis.py` | Stats parsing, 90th-percentile budget estimation, capacity prediction |
| `naenae/preflight.py` | Startup validation (claude, bd, config, stats cache) |
| `naenae/tasks.py` | `bd ready` task discovery, priority sorting, filtering |
| `naenae/spawner.py` | `claude --dangerously-skip-permissions -p` process management |
| `naenae/report.py` | Self-contained HTML report with SVG weekly usage chart |
| `naenae/state.py` | JSON state persistence (`$NAENAE_HOME/state.json`) |
| `naenae/paths.py` | Resolves `NAENAE_HOME` env var → `~/.naenae/` |

### Timers

| Interval | What runs |
|---|---|
| Every 5 min | `refresh_display` — updates menu bar UI only |
| Every 4 hrs | `run_analysis_cycle` — full stats + spawning cycle |

### Trigger logic

Agents are spawned when:
- `predicted_unused_capacity >= trigger.min_capacity_percent` AND
- `days_remaining_in_week <= trigger.max_days_remaining`

Override with the **Run Now** menu item.
