#!/usr/bin/env bash
# Nae Nae — install or bootstrap
#
# One-liner install (no git clone needed):
#   curl -fsSL https://raw.githubusercontent.com/gpxl/naenae/main/install.sh | bash
#
# Re-run from an existing clone to update the launchd registration:
#   bash install.sh
#
# Options:
#   --check         Run dependency/config checks only; no changes. Exit 0=ok, 1=issues.
#   --defer-start   Install plist but do NOT load it into launchd.
#   --help          Show this help.
#
# Environment overrides:
#   NAENAE_HOME=/other/path   Override data directory.
#   SKIP_DEP_CHECK=1          Skip claude/bd presence checks (escape hatch).
#
set -euo pipefail

# ── Argument parsing ──────────────────────────────────────────────────────────
CHECK_ONLY=0
DEFER_START=0

for arg in "$@"; do
  case "$arg" in
    --check)        CHECK_ONLY=1 ;;
    --defer-start)  DEFER_START=1 ;;
    --help|-h)
      sed -n '2,14p' "$0" | sed 's/^# \?//'
      exit 0
      ;;
    *)
      echo "Unknown option: $arg  (try --help)" >&2
      exit 1
      ;;
  esac
done

# ── Bootstrap detection ───────────────────────────────────────────────────────
# When piped from curl, BASH_SOURCE[0] is empty and we are NOT inside the repo.
# Detect this by checking for the package directory in the resolved script dir.
_src="${BASH_SOURCE[0]:-}"
SCRIPT_DIR="$(cd "$(dirname "${_src:-$0}")" 2>/dev/null && pwd || pwd)"

if [[ ! -f "$SCRIPT_DIR/naenae/app.py" ]]; then
  INSTALL_DIR="${NAENAE_INSTALL_DIR:-$HOME/.naenae/src}"
  echo "=== Nae Nae Bootstrap ==="
  if [[ -d "$INSTALL_DIR/.git" ]]; then
    echo "→ Updating existing clone at $INSTALL_DIR…"
    git -C "$INSTALL_DIR" pull --ff-only
  else
    echo "→ Cloning to $INSTALL_DIR…"
    git clone https://github.com/gpxl/naenae.git "$INSTALL_DIR"
  fi
  # Re-run from the cloned repo; set NAENAE_HOME so data goes to ~/.naenae/
  exec env NAENAE_HOME="$HOME/.naenae" bash "$INSTALL_DIR/install.sh" "$@"
fi

# ── Local install (running from inside the repo) ──────────────────────────────
# Data dir defaults to SCRIPT_DIR so existing dev installs keep their data in
# place. Override with: NAENAE_HOME=/other/path bash install.sh
NAENAE_HOME="${NAENAE_HOME:-$HOME/.naenae}"

PLIST_NAME="com.gpxl.naenae"
PLIST_DEST="$HOME/Library/LaunchAgents/$PLIST_NAME.plist"
LOG_DIR="$NAENAE_HOME/logs"
LAUNCHD_LOG="$LOG_DIR/launchd.log"
CONFIG_FILE="$NAENAE_HOME/config.yaml"
TEMPLATE="$SCRIPT_DIR/config.yaml.template"

# ── Check mode header ─────────────────────────────────────────────────────────
if [[ "$CHECK_ONLY" -eq 1 ]]; then
  echo "=== Nae Nae — Dependency Check ==="
else
  echo "=== Nae Nae Installer ==="
  echo "   Source : $SCRIPT_DIR"
  echo "   Data   : $NAENAE_HOME"
fi

# ── Python check ──────────────────────────────────────────────────────────────
PYTHON=$(command -v python3 || true)
if [[ -z "$PYTHON" ]]; then
  echo "❌ python3 not found. Install Python 3.9+ and try again."
  exit 1
fi

PY_VER=$("$PYTHON" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
PY_MAJOR=$(echo "$PY_VER" | cut -d. -f1)
PY_MINOR=$(echo "$PY_VER" | cut -d. -f2)

if [[ "$PY_MAJOR" -lt 3 || ("$PY_MAJOR" -eq 3 && "$PY_MINOR" -lt 9) ]]; then
  echo "❌ Python 3.9+ required (found $PY_VER)."
  exit 1
fi

echo "✓ Python $PY_VER found at $PYTHON"

# ── Dependency checks (claude + bd) ──────────────────────────────────────────
SKIP_DEP_CHECK="${SKIP_DEP_CHECK:-0}"
DEP_ERRORS=0

if [[ "$SKIP_DEP_CHECK" -eq 0 ]]; then
  CLAUDE_BIN=$(command -v claude 2>/dev/null || true)
  if [[ -z "$CLAUDE_BIN" ]]; then
    echo "❌ 'claude' CLI not found in PATH."
    echo "   Install: npm install -g @anthropic-ai/claude-code"
    echo "   Then re-run: bash install.sh"
    DEP_ERRORS=1
  else
    echo "✓ claude found at $CLAUDE_BIN"
  fi

  BD_BIN=$(command -v bd 2>/dev/null || true)
  if [[ -z "$BD_BIN" ]]; then
    echo "❌ 'bd' (beads) CLI not found in PATH."
    echo "   Install: npm install -g beads-cli"
    echo "   Then re-run: bash install.sh"
    DEP_ERRORS=1
  else
    echo "✓ bd found at $BD_BIN"
  fi

  # Ghostty: recommended (non-blocking) — used as the control-window terminal
  if [[ -d "/Applications/Ghostty.app" ]]; then
    echo "✓ Ghostty.app found"
  else
    echo "⚠  Ghostty.app not found — Terminal.app will be used as fallback."
    echo "   Recommended: install Ghostty from https://ghostty.org"
    echo "   (avoids blank-window race when attaching to agent sessions)"
  fi

  if [[ "$DEP_ERRORS" -ne 0 ]]; then
    echo ""
    echo "Fix the missing tools above, then re-run install.sh."
    echo "To skip these checks (not recommended): SKIP_DEP_CHECK=1 bash install.sh"
    exit 1
  fi
else
  # SKIP_DEP_CHECK=1 — still try to locate binaries for PATH injection
  echo "⚠  Skipping claude/bd checks (SKIP_DEP_CHECK=1)"
  CLAUDE_BIN=$(command -v claude 2>/dev/null || true)
  BD_BIN=$(command -v bd 2>/dev/null || true)
fi

# ── Config existence check ────────────────────────────────────────────────────
if [[ "$CHECK_ONLY" -eq 1 ]]; then
  # Config check
  if [[ ! -f "$CONFIG_FILE" ]]; then
    echo "❌ Config not found at $CONFIG_FILE"
    echo "   Run 'bash install.sh' (without --check) to create it."
    exit 1
  fi

  # Placeholder check
  if grep -q "PLACEHOLDER_PROJECT_PATH" "$CONFIG_FILE" 2>/dev/null; then
    echo "❌ Config still contains placeholder path."
    echo "   Edit $CONFIG_FILE and replace PLACEHOLDER_PROJECT_PATH."
    exit 1
  fi

  echo "✓ Config exists and has no placeholders"
  echo ""
  echo "✅ All checks passed."
  exit 0
fi

# ── Install path (not --check) ────────────────────────────────────────────────

# Install Python dependencies
echo "→ Installing Python dependencies…"
"$PYTHON" -m pip install --quiet --break-system-packages -r "$SCRIPT_DIR/requirements.txt"
echo "✓ Dependencies installed"

# Create data directories
mkdir -p "$LOG_DIR" "$NAENAE_HOME/reports"

# Copy template config if none exists yet
FRESH_CONFIG=0
if [[ ! -f "$CONFIG_FILE" ]] && [[ -f "$TEMPLATE" ]]; then
  cp "$TEMPLATE" "$CONFIG_FILE"
  FRESH_CONFIG=1
  echo "✓ Config created at $CONFIG_FILE"
elif [[ ! -f "$CONFIG_FILE" ]]; then
  echo "⚠️  No config found at $CONFIG_FILE — create one before starting."
fi

# ── Build dynamic launchd PATH ────────────────────────────────────────────────
# launchd inherits a minimal PATH that often omits npm/node bin dirs.
# We prepend the directories containing claude and bd so agents can find them.
BASE_LAUNCHD_PATH="/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$HOME/.local/bin"

EXTRA_DIRS=""
if [[ -n "$CLAUDE_BIN" ]]; then
  CLAUDE_DIR=$(dirname "$CLAUDE_BIN")
  EXTRA_DIRS="$CLAUDE_DIR"
fi
if [[ -n "$BD_BIN" ]]; then
  BD_DIR=$(dirname "$BD_BIN")
  if [[ "$BD_DIR" != "$CLAUDE_DIR" ]] 2>/dev/null; then
    EXTRA_DIRS="$EXTRA_DIRS:$BD_DIR"
  fi
fi

# Deduplicate path components using awk
RAW_PATH="${EXTRA_DIRS:+$EXTRA_DIRS:}$BASE_LAUNCHD_PATH"
LAUNCHD_PATH=$(echo "$RAW_PATH" | tr ':' '\n' | awk '!seen[$0]++' | tr '\n' ':' | sed 's/:$//')

# ── Generate launchd plist ────────────────────────────────────────────────────
PLIST_FILE="$SCRIPT_DIR/$PLIST_NAME.plist"

cat > "$PLIST_FILE" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$PLIST_NAME</string>
  <key>ProgramArguments</key>
  <array>
    <string>$SCRIPT_DIR/NaeNae.app/Contents/MacOS/NaeNae</string>
  </array>
  <key>WorkingDirectory</key>
  <string>$SCRIPT_DIR</string>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>$LAUNCHD_LOG</string>
  <key>StandardErrorPath</key>
  <string>$LAUNCHD_LOG</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>$LAUNCHD_PATH</string>
    <key>HOME</key>
    <string>$HOME</string>
    <key>NAENAE_HOME</key>
    <string>$NAENAE_HOME</string>
  </dict>
</dict>
</plist>
PLIST

echo "✓ Plist generated at $PLIST_FILE"

# ── Install plist ─────────────────────────────────────────────────────────────
mkdir -p "$HOME/Library/LaunchAgents"
cp "$PLIST_FILE" "$PLIST_DEST"
echo "✓ Plist installed to $PLIST_DEST"

# ── Auto-defer on fresh config ────────────────────────────────────────────────
if [[ "$FRESH_CONFIG" -eq 1 ]]; then
  DEFER_START=1
fi

# ── Load or defer ─────────────────────────────────────────────────────────────
if [[ "$DEFER_START" -eq 0 ]]; then
  # Unload existing service if running
  if launchctl list | grep -q "$PLIST_NAME" 2>/dev/null; then
    echo "→ Unloading existing Nae Nae service…"
    launchctl unload "$PLIST_DEST" 2>/dev/null || true
  fi

  launchctl load "$PLIST_DEST"
  echo "✓ Nae Nae service loaded"
  echo ""
  echo "✅ Nae Nae is now running!"
  echo "   Look for '● Nae Nae' in your macOS menu bar."
  echo "   Config : $CONFIG_FILE"
  echo "   Logs   : $LAUNCHD_LOG"
else
  echo ""
  if [[ "$FRESH_CONFIG" -eq 1 ]]; then
    echo "⚠  Config just created — edit it before starting Nae Nae."
  else
    echo "⚠  Deferred start (--defer-start)."
  fi
  echo "   1. Edit config:  open $CONFIG_FILE"
  echo "   2. Verify setup: bash $SCRIPT_DIR/install.sh --check"
  echo "   3. Then start:   launchctl load $PLIST_DEST"
fi

echo ""
echo "To uninstall:"
echo "  launchctl unload $PLIST_DEST"
echo "  rm $PLIST_DEST"
