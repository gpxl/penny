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

## Chart Transitions (MANDATORY)

All dashboard charts must **transition** into new data, never redraw from scratch
on periodic updates. A full innerHTML re-render is only acceptable on first paint
or when the data structure changes (e.g. number of bars changes).

| Rule | Detail |
|------|--------|
| First paint | Full render via `innerHTML` — use `barGrowUp` animation for entry |
| Periodic update (same structure) | Animate existing elements using `animateAttr()` / CSS transitions |
| Structure change (bar count differs) | Full re-render is acceptable — chart must detect this |
| Time window change | Same as structure change — re-render with entry animation |
| Never | Replace `innerHTML` on a 30s poll when data shape is unchanged |

### Implementation pattern

For SVG bar charts:
1. Tag each bar with `data-bar="<index>"` on first render
2. Store bar count and max value on the SVG element (`data-bar-count`, `data-max`)
3. On update, compare bar count — if same, animate via `animateAttr(el, 'y', newY)` and `animateAttr(el, 'height', newH)`
4. If bar count changed, fall back to full re-render

For HTML metric displays:
- Use CSS `transition` on widths (`.seg-bar span { transition: width .4s; }`)
- Use `animateNumber()` for count-up effects on statistics

## Global Timeseries Filter

All time-scoped dashboard cards must respect the global `metricsFilter` selector.
No card may have its own independent time filter controls.

| Rule | Detail |
|------|--------|
| Single source | `metricsFilter` variable is the global time window |
| Filter bar | One set of buttons: Session, This week, This month, All time |
| All cards respond | Model & Cache, Activity by Hour, Top Tools, Projects, Session History |
| No per-card filters | Cards must not render their own time filter buttons |
| Filter change | `setMetricsFilter()` must update all time-scoped cards |
