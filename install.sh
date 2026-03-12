#!/usr/bin/env bash
# Penny — install or bootstrap
#
# One-liner install (no git clone needed):
#   curl -fsSL https://raw.githubusercontent.com/gpxl/penny/main/install.sh | bash
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
#   PENNY_HOME=/other/path   Override data directory.
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
      if [[ -f "$0" ]]; then
        sed -n '2,14p' "$0" | sed 's/^# \?//'
      else
        echo "Penny installer — run: bash install.sh [--check] [--defer-start]"
      fi
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

if [[ ! -f "$SCRIPT_DIR/penny/app.py" ]]; then
  INSTALL_DIR="${PENNY_INSTALL_DIR:-$HOME/.penny/src}"
  echo "=== Penny Bootstrap ==="

  # git is required for cloning/updating
  if ! command -v git &>/dev/null; then
    echo "❌ 'git' not found. Install Xcode Command Line Tools and try again:"
    echo "   xcode-select --install"
    exit 1
  fi

  if [[ -d "$INSTALL_DIR/.git" ]]; then
    echo "→ Updating existing clone at $INSTALL_DIR…"
    git -C "$INSTALL_DIR" pull --ff-only
  else
    echo "→ Cloning to $INSTALL_DIR…"
    git clone https://github.com/gpxl/penny.git "$INSTALL_DIR"
  fi
  # Re-run from the cloned repo; set PENNY_HOME so data goes to ~/.penny/
  exec env PENNY_HOME="$HOME/.penny" bash "$INSTALL_DIR/install.sh" "$@"
fi

# ── Local install (running from inside the repo) ──────────────────────────────
# Data dir defaults to SCRIPT_DIR so existing dev installs keep their data in
# place. Override with: PENNY_HOME=/other/path bash install.sh
PENNY_HOME="${PENNY_HOME:-$HOME/.penny}"

PLIST_NAME="com.gpxl.penny"
PLIST_DEST="$HOME/Library/LaunchAgents/$PLIST_NAME.plist"
LOG_DIR="$PENNY_HOME/logs"
LAUNCHD_LOG="$LOG_DIR/launchd.log"
CONFIG_FILE="$PENNY_HOME/config.yaml"
TEMPLATE="$SCRIPT_DIR/config.yaml.template"

# ── Check mode header ─────────────────────────────────────────────────────────
if [[ "$CHECK_ONLY" -eq 1 ]]; then
  echo "=== Penny — Dependency Check ==="
else
  echo "=== Penny Installer ==="
  echo "   Source : $SCRIPT_DIR"
  echo "   Data   : $PENNY_HOME"
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
  # Node.js / npm are required for claude and bd
  NODE_BIN=$(command -v node 2>/dev/null || true)
  NPM_BIN=$(command -v npm 2>/dev/null || true)
  if [[ -z "$NODE_BIN" || -z "$NPM_BIN" ]]; then
    echo "❌ Node.js / npm not found."
    echo "   Install Node.js from https://nodejs.org (LTS recommended)"
    DEP_ERRORS=1
  else
    echo "✓ Node.js $(node --version) / npm $(npm --version) found"
  fi

  CLAUDE_BIN=$(command -v claude 2>/dev/null || true)
  if [[ -z "$CLAUDE_BIN" ]]; then
    echo "❌ 'claude' CLI not found in PATH."
    echo "   Install: npm install -g @anthropic-ai/claude-code"
    echo "   Then authenticate: claude auth login"
    echo "   Then re-run: bash install.sh"
    DEP_ERRORS=1
  else
    echo "✓ claude found at $CLAUDE_BIN"
    # Auth hint — check for auth.json presence (non-blocking)
    if [[ ! -f "$HOME/.claude/auth.json" ]]; then
      echo "⚠  Claude authentication not detected."
      echo "   Run: claude auth login"
      echo "   (Without auth, agent spawning will time out after 45 s)"
    fi
  fi

  BD_BIN=$(command -v bd 2>/dev/null || true)
  if [[ -z "$BD_BIN" ]]; then
    echo "⚡ Optional: 'bd' (beads) CLI not found."
    echo "   Install beads for task management: npm install -g @beads/bd"
    echo "   Penny will detect and activate the beads plugin automatically when you install it."
  else
    echo "✓ bd found at $BD_BIN — Beads detected, task management plugin will activate automatically."
  fi

  # tmux/screen — only needed for plugin-based agent spawning (informational)
  TMUX_BIN=$(command -v tmux 2>/dev/null || true)
  SCREEN_BIN=$(command -v screen 2>/dev/null || true)
  if [[ -n "$TMUX_BIN" ]]; then
    echo "✓ tmux found at $TMUX_BIN (used by agent spawning plugins)"
  elif [[ -n "$SCREEN_BIN" ]]; then
    echo "✓ screen found (used by agent spawning plugins)"
  else
    echo "ℹ  tmux/screen not found — only needed if you enable agent spawning plugins later."
  fi

  # Ghostty — only needed for agent spawning (informational)
  if [[ -d "/Applications/Ghostty.app" ]]; then
    echo "✓ Ghostty.app found"
  else
    echo "ℹ  Ghostty.app not found — only needed for agent spawning plugins."
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

  # Placeholder check (warning only — projects are optional for monitoring)
  if grep -q "PLACEHOLDER_PROJECT_PATH" "$CONFIG_FILE" 2>/dev/null; then
    echo "⚠  Config contains placeholder project path (optional — only needed for agent plugins)."
  fi

  echo "✓ Config exists and has no placeholders"
  echo ""
  echo "✅ All checks passed."
  exit 0
fi

# ── Install path (not --check) ────────────────────────────────────────────────

# Install Python dependencies
echo "→ Installing Python dependencies…"
if ! "$PYTHON" -m pip install --break-system-packages -r "$SCRIPT_DIR/requirements.txt"; then
  echo "❌ Python dependency install failed. Check the errors above."
  exit 1
fi
echo "✓ Dependencies installed"

# ── Build native launcher binary ──────────────────────────────────────────────
LAUNCHER_SRC="$SCRIPT_DIR/launcher/main.c"
LAUNCHER_BIN="$SCRIPT_DIR/Penny.app/Contents/MacOS/Penny"
if [[ -f "$LAUNCHER_SRC" ]]; then
  # Rebuild if source is newer than binary, or binary doesn't exist / isn't Mach-O
  if [[ ! -f "$LAUNCHER_BIN" ]] || [[ "$LAUNCHER_SRC" -nt "$LAUNCHER_BIN" ]] || \
     ! file "$LAUNCHER_BIN" | grep -q "Mach-O"; then
    echo "→ Compiling native Penny launcher…"
    if clang "$LAUNCHER_SRC" -o "$LAUNCHER_BIN" -mmacosx-version-min=13.0 2>&1; then
      echo "✓ Launcher compiled"
      echo "→ Signing Penny.app…"
      codesign --sign - --force --deep "$SCRIPT_DIR/Penny.app" 2>/dev/null && \
        echo "✓ Penny.app signed (ad-hoc)" || echo "⚠️  codesign failed (non-fatal)"
    else
      echo "❌ Launcher compile failed — clang is required. Install Xcode Command Line Tools."
      exit 1
    fi
  else
    echo "✓ Launcher binary up to date"
  fi
fi

# Create data directories
mkdir -p "$LOG_DIR" "$PENNY_HOME/reports"

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
# Include the detected python3 directory so the launcher can find it via PATH.
PYTHON_DIR=$(dirname "$PYTHON")
EXTRA_DIRS="$PYTHON_DIR"
if [[ -n "$CLAUDE_BIN" ]]; then
  CLAUDE_DIR=$(dirname "$CLAUDE_BIN")
  EXTRA_DIRS="$EXTRA_DIRS:$CLAUDE_DIR"
fi
if [[ -n "$BD_BIN" ]]; then
  BD_DIR=$(dirname "$BD_BIN")
  EXTRA_DIRS="$EXTRA_DIRS:$BD_DIR"
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
    <string>$SCRIPT_DIR/Penny.app/Contents/MacOS/Penny</string>
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
    <key>PENNY_HOME</key>
    <string>$PENNY_HOME</string>
    <key>PYTHONPATH</key>
    <string>$SCRIPT_DIR</string>
  </dict>
</dict>
</plist>
PLIST

echo "✓ Plist generated at $PLIST_FILE"

# ── Install plist ─────────────────────────────────────────────────────────────
mkdir -p "$HOME/Library/LaunchAgents"
cp "$PLIST_FILE" "$PLIST_DEST"
echo "✓ Plist installed to $PLIST_DEST"

# ── ~/Applications symlink (Spotlight/Finder access) ────────────────────────
APPS_DIR="$HOME/Applications"
SYMLINK_DEST="$APPS_DIR/Penny.app"
mkdir -p "$APPS_DIR"
if [[ -L "$SYMLINK_DEST" && "$(readlink "$SYMLINK_DEST")" != "$SCRIPT_DIR/Penny.app" ]]; then
  rm "$SYMLINK_DEST"
fi
if [[ ! -e "$SYMLINK_DEST" ]]; then
  ln -s "$SCRIPT_DIR/Penny.app" "$SYMLINK_DEST"
  echo "✓ Penny.app linked to $SYMLINK_DEST (Spotlight or Finder)"
else
  echo "✓ ~/Applications/Penny.app in place"
fi

# ── penny CLI ────────────────────────────────────────────────────────────────
CLI_BIN_DIR="$HOME/.local/bin"
CLI_DEST="$CLI_BIN_DIR/penny"
mkdir -p "$CLI_BIN_DIR"
cp "$SCRIPT_DIR/scripts/penny" "$CLI_DEST"
chmod +x "$CLI_DEST"
echo "✓ penny CLI installed to $CLI_DEST"
echo "  Run: penny start | penny stop | penny status"
if ! echo "$PATH" | tr ':' '\n' | grep -qx "$CLI_BIN_DIR"; then
  echo "  ⚠  ~/.local/bin is not in your PATH — add it to ~/.zprofile:"
  echo "     export PATH=\"\$HOME/.local/bin:\$PATH\""
fi

# Fresh config works out of the box (monitoring only), no need to defer.

# ── Load or defer ─────────────────────────────────────────────────────────────
if [[ "$DEFER_START" -eq 0 ]]; then
  # Unload existing service if running (bootout for macOS 13+, unload as fallback)
  if launchctl list | grep -q "$PLIST_NAME" 2>/dev/null; then
    echo "→ Unloading existing Penny service…"
    launchctl bootout "gui/$(id -u)/$PLIST_NAME" 2>/dev/null || \
      launchctl unload "$PLIST_DEST" 2>/dev/null || true
  fi

  launchctl bootstrap "gui/$(id -u)" "$PLIST_DEST" 2>/dev/null || \
    launchctl load "$PLIST_DEST"
  echo "✓ Penny service loaded"
  echo ""
  echo "✅ Penny is now running!"
  echo "   Look for '● Penny' in your macOS menu bar."
  echo "   Config : $CONFIG_FILE"
  echo "   Logs   : $LAUNCHD_LOG"
else
  echo ""
  echo "⚠  Deferred start (--defer-start)."
  echo "   To start: launchctl bootstrap gui/\$(id -u) $PLIST_DEST"
  echo "   Config  : $CONFIG_FILE"
fi

echo ""
echo "To uninstall:"
echo "  launchctl bootout gui/\$(id -u)/$PLIST_NAME"
echo "  rm $PLIST_DEST"
