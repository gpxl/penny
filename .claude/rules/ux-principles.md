# UX Principles (CRITICAL)

## Optimistic UI

All user-initiated actions must update the UI immediately, before the server confirms.
If the server rejects the change, revert the UI and show an inline error.

| Pattern | Example |
|---------|---------|
| Toggle → instant flip | Keep Alive switch flips immediately, reverts on failure |
| Save → "Saving..." → "Saved" | Projects save button shows progress, then confirmation |
| Action → disabled + spinner | Install button → "Installing..." with spinner |

Never leave the user staring at an unchanged UI waiting for a response.

## Live Process Transparency

Any background process (plugin install, agent spawn, config sync) must stream
its output to the user in real-time. No black-box operations.

| Requirement | Detail |
|-------------|--------|
| Terminal output | Stream stdout/stderr line-by-line to a visible log panel |
| Progress | Show spinners, progress bars, or status text for async work |
| Errors | Surface inline — never silently swallow failures |
| Completion | Clear success/failure state with visual feedback |

### Implementation pattern

For any new background process:
1. Store output in a line buffer (`list[str]`) keyed by operation ID
2. Expose via polling endpoint (GET returns lines + status + offset)
3. Dashboard JS polls every 1s, appends lines to `<pre>` log viewer, auto-scrolls
4. On completion, show success/failure banner above the log
