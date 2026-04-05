# Dashboard HTTP API

Penny runs a local HTTP server at `127.0.0.1` (default port 7432). The port is written to `$PENNY_HOME/.dashboard_port` at startup. The `penny` CLI uses this API; you can also call it from scripts.

## Port Discovery

```bash
PORT=$(cat ~/.penny/.dashboard_port 2>/dev/null || echo 7432)
BASE="http://127.0.0.1:$PORT"
```

## Endpoints

### Core endpoints

| Method | Path | Request Body | Response | CLI equivalent |
|--------|------|--------------|----------|----------------|
| `GET` | `/` | — | HTML dashboard | `penny open` |
| `GET` | `/api/state` | — | JSON state snapshot | `penny status` |
| `POST` | `/api/refresh` | `{}` | `{"ok": true}` | `penny refresh` |
| `POST` | `/api/quit` | `{}` | `{"ok": true}` | `penny quit` |

### Plugin-dependent endpoints

These endpoints require an active plugin that provides task management (e.g. the Beads plugin on the `feature/beads-plugin` branch). They return `{"error": "no plugin"}` when no task-management plugin is active.

| Method | Path | Request Body | Response |
|--------|------|--------------|----------|
| `POST` | `/api/run` | `{"task_id": "..."}` | `{"ok": true}` |
| `POST` | `/api/stop-agent` | `{"task_id": "..."}` | `{"ok": true}` |
| `POST` | `/api/dismiss` | `{"task_id": "..."}` | `{"ok": true}` |
| `POST` | `/api/clear-completed` | `{}` | `{"ok": true}` |

## State Snapshot Schema

`GET /api/state` returns:

```json
{
  "generated_at": "2026-03-07T10:00:00",
  "state": {
    "predictions": { "pct_all": 42.1, "pct_sonnet": 31.8 },
    "period_history": [...],
    "session_history": [...],
    "health_alerts": [
      {"project": "Weekly Budget", "cwd": "", "health": "yellow", "reasons": ["Projected to use 92% of weekly budget by reset (1.8d remaining). Currently at 71%."]},
      {"project": "my-project", "cwd": "/path/to/my-project", "health": "red", "reasons": ["High error rate: 37 of 60 tool calls failed (62%)"]}
    ],
    "plugin_state": {}
  },
  "prediction": {
    "pct_all": 42.1,
    "pct_sonnet": 31.8,
    "days_remaining": 1.4,
    "reset_label": "Tomorrow 9 am"
  },
  "session_history": [...],
  "period_history": [...],
  "plugin_cards": [],
  "active_plugins": [],
  "rich_metrics": {},
  "rich_metrics_by_window": {},
  "intraday_samples": []
}
```

| Field | Description |
|-------|-------------|
| `generated_at` | ISO 8601 timestamp when the snapshot was taken |
| `state` | Full runtime state object |
| `prediction.pct_all` | Predicted usage as percentage of total weekly budget |
| `prediction.pct_sonnet` | Predicted Sonnet-model usage percentage |
| `prediction.days_remaining` | Fractional days until billing period resets |
| `prediction.reset_label` | Human-readable reset time (e.g. "Tomorrow 9 am") |
| `session_history` | Per-session token usage records |
| `period_history` | Archived billing period records |
| `plugin_cards` | Dashboard cards contributed by active plugins (empty when none active) |
| `active_plugins` | List of currently active plugin names |
| `state.health_alerts` | Active health alerts: budget projection, sustained anomaly, error rate |
| `rich_metrics` | Detailed model/cache/tool metrics (default window) |
| `rich_metrics_by_window` | Metrics per time window: `session`, `week`, `month`, `all` |
| `intraday_samples` | Periodic usage samples (last 48 hours) |

## Error Responses

All endpoints return `{"error": "description"}` with an appropriate HTTP status code (4xx or 5xx) on failure.

## Examples

```bash
# Get current state
curl -s http://127.0.0.1:7432/api/state | python3 -m json.tool

# Force a refresh cycle
curl -s -X POST http://127.0.0.1:7432/api/refresh \
  -H "Content-Type: application/json" \
  -d '{}'
```
