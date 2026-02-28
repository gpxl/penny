#!/usr/bin/env bash
# Watcher — one-shot install + launchd registration
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_PATH="$SCRIPT_DIR/watcher/app.py"
PLIST_NAME="com.gerlando.watcher"
PLIST_DEST="$HOME/Library/LaunchAgents/$PLIST_NAME.plist"
LOG_DIR="$SCRIPT_DIR/logs"
LAUNCHD_LOG="$LOG_DIR/launchd.log"

echo "=== Watcher Installer ==="

# Check Python 3.9+
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

# Install dependencies
echo "→ Installing Python dependencies…"
"$PYTHON" -m pip install --quiet -r "$SCRIPT_DIR/requirements.txt"
echo "✓ Dependencies installed"

# Create directories
mkdir -p "$LOG_DIR" "$SCRIPT_DIR/reports"

# Generate launchd plist
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
    <string>$PYTHON</string>
    <string>-m</string>
    <string>watcher.app</string>
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
    <string>/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$HOME/.local/bin</string>
    <key>HOME</key>
    <string>$HOME</string>
  </dict>
</dict>
</plist>
PLIST

echo "✓ Plist generated at $PLIST_FILE"

# Unload existing if running
if launchctl list | grep -q "$PLIST_NAME" 2>/dev/null; then
  echo "→ Unloading existing Watcher service…"
  launchctl unload "$PLIST_DEST" 2>/dev/null || true
fi

# Install plist
mkdir -p "$HOME/Library/LaunchAgents"
cp "$PLIST_FILE" "$PLIST_DEST"
echo "✓ Plist installed to $PLIST_DEST"

# Load service
launchctl load "$PLIST_DEST"
echo "✓ Watcher service loaded"

echo ""
echo "✅ Watcher is now running!"
echo "   Look for '● Watcher' in your macOS menu bar."
echo "   Logs: $LAUNCHD_LOG"
echo "   Config: $SCRIPT_DIR/config.yaml"
echo ""
echo "To uninstall:"
echo "  launchctl unload $PLIST_DEST"
echo "  rm $PLIST_DEST"
