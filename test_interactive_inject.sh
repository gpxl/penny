#!/bin/bash
# Minimal test: inject an initial prompt into interactive claude via tmux send-keys.
# Expected: Ghostty opens, claude starts, prompt is injected after 4s, user can interact.
#
# Usage: bash test_interactive_inject.sh
# Exit the session with: Ctrl-C or type /exit in claude, then Ctrl-D or 'exit' in bash

SESSION="penny-inject-test"

# Clean up any leftover session from a previous run
tmux kill-session -t "$SESSION" 2>/dev/null || true

echo "Starting tmux session '$SESSION' with interactive claude..."

# Start a detached tmux session running claude in interactive mode (NOT -p)
tmux new-session -d -s "$SESSION" -x 220 -y 50 \
    claude --dangerously-skip-permissions

echo "Waiting 4s for claude to initialize..."
sleep 4

# Inject the initial prompt as if the user typed it and pressed Enter
PROMPT="Say exactly: hello from penny"
echo "Injecting prompt: '$PROMPT'"
tmux send-keys -t "$SESSION" "$PROMPT" Enter

echo ""
echo "Attaching to session — you should see claude responding."
echo "You can type follow-up messages. Press Ctrl-C or type /exit to quit."
echo ""

# Attach so the user can interact immediately
tmux attach -t "$SESSION"
