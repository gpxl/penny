# Penny

macOS menu bar app that monitors your Claude Max token usage, predicts unused weekly capacity, and autonomously spawns Claude Code agents on [Beads](https://github.com/steveyegge/beads) tasks before your billing period resets.

---

## Resources

- [CHANGELOG](CHANGELOG.md) — version history
- [Security Policy](SECURITY.md) — threat model and vulnerability reporting
- [Contributing](CONTRIBUTING.md) — dev setup and PR process
- [API Reference](docs/API.md) — dashboard HTTP API for scripting

---

## What it does

1. **Token monitoring** — reads `~/.claude/stats-cache.json` every 5 minutes and displays current and projected weekly usage in the menu bar.
2. **Capacity prediction** — uses a 90th-percentile model to estimate how much of your Claude Max budget will go unused by the end of the week.
3. **Autonomous agent spawning** — when predicted unused capacity is ≥ 30% and ≤ 2 days remain, it automatically runs `claude --dangerously-skip-permissions -p` for each ready Beads task across your configured projects.
4. **Completion tracking** — detects when spawned agents finish and sends macOS notifications.
5. **Weekly report** — generates a self-contained HTML report with an SVG usage chart.

## Who it's for

Claude Max subscribers who also use [Beads](https://github.com/steveyegge/beads) (`bd`) for task management and want to put spare weekly capacity to work automatically.

---

## Requirements

**Required**

| Requirement | Notes |
|---|---|
| macOS 12+ | Menu bar requires macOS |
| Python 3.9+ | `python3 --version` to check |
| `claude` CLI (authenticated) | `npm install -g @anthropic-ai/claude-code` |
| Claude Max subscription | Required for the usage stats this app reads |

**Optional**

| Requirement | Notes |
|---|---|
| `bd` CLI | `brew install beads` or `npm install -g @beads/bd` — enables automatic agent task spawning; Penny runs without it as a usage monitor only |

---

## Installation

### Option 1 — curl one-liner (recommended)

```bash
curl -fsSL https://raw.githubusercontent.com/gpxl/penny/main/install.sh | bash
```

This clones the repo to `~/.penny/src/` and runs the installer automatically.

### Option 2 — pipx

```bash
pipx install git+https://github.com/gpxl/penny.git
```

Then register the launchd service manually:

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/gpxl/penny/main/install.sh)
```

### Option 3 — Homebrew tap

```bash
brew tap gpxl/penny
brew install penny
brew services start penny
```

> See [Homebrew tap](#homebrew-tap) below for the publish workflow.

---

## First-run setup

1. **Install claude CLI**
   ```bash
   npm install -g @anthropic-ai/claude-code
   ```

2. **Authenticate with Anthropic**
   ```bash
   claude auth login
   ```

3. **Edit config** — the installer creates `~/.penny/config.yaml` from the template. Replace the placeholder path:
   ```bash
   open ~/.penny/config.yaml
   ```

4. **Verify setup**
   ```bash
   bash ~/.penny/src/install.sh --check
   ```

5. **Start the service**
   ```bash
   launchctl load ~/Library/LaunchAgents/com.gpxl.penny.plist
   ```

> The installer auto-defers step 5 when it creates a fresh config, so you won't accidentally start with unconfigured placeholders.

### Optional: Enable task automation with Beads

Penny monitors token usage without Beads. To also enable autonomous agent spawning on Beads tasks:

1. **Install beads CLI**
   ```bash
   brew install beads
   # or: npm install -g @beads/bd
   ```

2. **Init beads in your project(s)**
   ```bash
   cd ~/Documents/GitHub/your-repo
   bd init
   ```

Penny auto-detects `bd` in PATH and activates the plugin automatically.

---

## Config reference

Config file: `~/.penny/config.yaml` (or `$PENNY_HOME/config.yaml`)

| Key | Type | Default | Description |
|---|---|---|---|
| `projects[].path` | string | — | Absolute or `~/`-relative path to a git repo with `.beads/` |
| `projects[].priority` | int | — | Lower = higher priority; tasks from lower-numbered projects spawn first |
| `trigger.min_capacity_percent` | int | 30 | Spawn only if ≥ N% of weekly budget predicted unused |
| `trigger.max_days_remaining` | int | 2 | Spawn only if ≤ N days remain in the billing week |
| `work.agent_permissions` | string | `"full"` | Agent permission mode: `full`, `scoped`, or `off` |
| `work.allowed_tools` | list | — | Tool allowlist when `agent_permissions: scoped` |
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
- Config still has `PLACEHOLDER_PROJECT_PATH` → edit `~/.penny/config.yaml`
- No `.beads/` directory in a project → run `bd init` inside the repo

### `claude` or `bd` not found under launchd

launchd uses a minimal PATH that often omits npm binary directories. Re-running `install.sh` after installing the tools injects their directories into the plist automatically:

```bash
bash ~/.penny/src/install.sh
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
tail -f ~/.penny/logs/launchd.log          # service stdout/stderr
tail -f ~/.penny/logs/agent-*.log          # per-agent logs
```

### Uninstall

```bash
launchctl unload ~/Library/LaunchAgents/com.gpxl.penny.plist
rm ~/Library/LaunchAgents/com.gpxl.penny.plist
rm -rf ~/.penny/src   # if installed via curl
# Optionally remove data:
rm -rf ~/.penny
```

---

## Homebrew tap

Homebrew taps must live in a **separate GitHub repository** named `homebrew-<tap-name>`. The formula file in this repo (`Formula/penny.rb`) is a reference copy only.

### One-time tap repo setup

1. Create `gpxl/homebrew-penny` on GitHub (public repo).
2. Clone it locally:
   ```bash
   git clone https://github.com/gpxl/homebrew-penny ~/homebrew-penny
   mkdir -p ~/homebrew-penny/Formula
   ```
3. After your first release (see below), copy the updated formula and push:
   ```bash
   cp Formula/penny.rb ~/homebrew-penny/Formula/penny.rb
   cd ~/homebrew-penny && git add Formula/penny.rb && git commit -m "penny 0.1.0" && git push
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
5. Updates `Formula/penny.rb` with the new url and sha256
6. Prints copy-paste instructions to update the tap repo

Then follow the printed instructions to push the tap.

**Dry-run (no push):**
```bash
bash scripts/release.sh --dry-run 0.2.0
```

**Regenerating dependency sha256s** (only needed when pinned dep versions change):
```bash
brew update-python-resources gpxl/penny/penny
```

---

## Security & Autonomous Agent Warning

Penny spawns Claude agents on your codebases automatically. Understand what this means before enabling.

### Agent permission modes

Control how much autonomy agents have via `work.agent_permissions` in `config.yaml`:

| Mode | Flag passed to `claude` | Behaviour |
|---|---|---|
| `full` (default) | `--dangerously-skip-permissions` | Agents operate without any permission prompts — maximum autonomy |
| `scoped` | `--allowed-tools <list>` | Agents may only use the tools listed in `work.allowed_tools` |
| `off` | *(no spawn)* | Monitoring only — Penny tracks tasks and capacity but never spawns agents |

**Example — scoped mode:**
```yaml
work:
  agent_permissions: "scoped"
  allowed_tools:
    - Read
    - Edit
    - Write
    - Glob
    - Grep
    - "Bash(git:*)"
    - "Bash(bd:*)"
```

**`full` mode implications:**
- **No permission prompts.** Claude agents will read, write, and delete files; run shell commands; create git branches; commit code; and open pull requests — all without asking for confirmation.
- **Runs on your actual projects.** Agents work inside the directories listed in `config.yaml`. Changes are real and can be pushed to remote repositories.
- **Triggered automatically.** When token capacity thresholds are met, Penny spawns agents on its own schedule — no user action required.

### How to limit scope further

| Config key | What it controls |
|---|---|
| `work.agent_permissions` | Permission mode: `full`, `scoped`, or `off` |
| `work.allowed_tools` | Tool allowlist when `agent_permissions: scoped` |
| `work.max_agents_per_run` | Cap the number of agents spawned per 4-hour cycle (default: 2) |
| `work.task_priority_levels` | Restrict which Beads priority labels are eligible (default: P1, P2, P3) |
| `trigger.min_capacity_percent` | Raise this to require a larger unused-budget buffer before spawning |
| `trigger.max_days_remaining` | Lower this to spawn only near end-of-week |

### Recommended before first use

1. Review all `bd ready` tasks in your configured projects — only tasks marked "ready" will be spawned.
2. Set conservative thresholds in `config.yaml` until you are comfortable with the behaviour.
3. Consider starting with `agent_permissions: scoped` or `agent_permissions: off` before enabling `full`.
4. Consider keeping `work.max_agents_per_run: 1` initially.

---

## Architecture

| Module | Purpose |
|---|---|
| `penny/app.py` | PyObjC/AppKit app, menus, timers, orchestration |
| `penny/analysis.py` | Stats parsing, 90th-percentile budget estimation, capacity prediction |
| `penny/preflight.py` | Startup validation (claude, bd, config, stats cache) |
| `penny/tasks.py` | `bd ready` task discovery, priority sorting, filtering |
| `penny/spawner.py` | `claude --dangerously-skip-permissions -p` process management |
| `penny/report.py` | Self-contained HTML report with SVG weekly usage chart |
| `penny/state.py` | JSON state persistence (`$PENNY_HOME/state.json`) |
| `penny/paths.py` | Resolves `PENNY_HOME` env var → `~/.penny/` |

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
