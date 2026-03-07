# Dashboard HTTP API

Penny runs a local HTTP server at `127.0.0.1` (default port 7432). The port is written to `$PENNY_HOME/.dashboard_port` at startup. The `penny` CLI uses this API; you can also call it from scripts.

## Port Discovery

```bash
PORT=$(cat ~/.penny/.dashboard_port 2>/dev/null || echo 7432)
BASE="http://127.0.0.1:$PORT"
```

## Endpoints

| Method | Path | Request Body | Response | CLI equivalent |
|--------|------|--------------|----------|----------------|
| `GET` | `/` | — | HTML dashboard | `penny open` |
| `GET` | `/api/state` | — | JSON state snapshot | `penny tasks`, `penny agents` |
| `POST` | `/api/refresh` | `{}` | `{"ok": true}` | `penny refresh` |
| `POST` | `/api/quit` | `{}` | `{"ok": true}` | `penny quit` |
| `POST` | `/api/run` | `{"task_id": "beads-123"}` | `{"ok": true}` | `penny run <id>` |
| `POST` | `/api/stop-agent` | `{"task_id": "beads-123"}` | `{"ok": true}` | `penny stop-agent <id>` |
| `POST` | `/api/dismiss` | `{"task_id": "beads-123"}` | `{"ok": true}` | `penny dismiss <id>` |
| `POST` | `/api/clear-completed` | `{}` | `{"ok": true}` | `penny clear-completed` |

## State Snapshot Schema

`GET /api/state` returns:

```json
{
  "generated_at": "2026-03-07T10:00:00",
  "state": {
    "agents_running": [...],
    "recently_completed": [...],
    "plugin_state": { "beads": { "spawned_task_ids": [...], "seen_closed_ids": [...] } }
  },
  "prediction": {
    "pct_all": 42.1,
    "pct_sonnet": 31.8,
    "days_remaining": 1.4,
    "reset_label": "Tomorrow 9 am"
  },
  "ready_tasks": [
    {
      "task_id": "beads-123",
      "title": "Fix login redirect",
      "priority": "P1",
      "project_path": "/Users/you/projects/myapp",
      "project_name": "myapp"
    }
  ],
  "completed_this_period": [...],
  "session_history": [...]
}
```

| Field | Description |
|-------|-------------|
| `generated_at` | ISO 8601 timestamp when the snapshot was taken |
| `state.agents_running` | Tasks with a currently active agent process |
| `state.recently_completed` | Last 20 completed tasks (agent or external); user-dismissable |
| `state.plugin_state` | Plugin-owned state, namespaced by plugin name; never reset by core |
| `prediction.pct_all` | Predicted usage as percentage of total weekly budget |
| `prediction.pct_sonnet` | Predicted Sonnet-model usage percentage |
| `prediction.days_remaining` | Fractional days until billing period resets |
| `prediction.reset_label` | Human-readable reset time (e.g. "Tomorrow 9 am") |
| `ready_tasks` | Tasks eligible for spawning (from plugins across all projects) |
| `completed_this_period` | Mirror of `state.recently_completed` for dashboard display |
| `session_history` | Per-session token usage records |

## Error Responses

All endpoints return `{"error": "description"}` with an appropriate HTTP status code (4xx or 5xx) on failure.

## Examples

```bash
# List ready tasks
curl -s http://127.0.0.1:7432/api/state | python3 -m json.tool

# Spawn a specific task
curl -s -X POST http://127.0.0.1:7432/api/run \
  -H "Content-Type: application/json" \
  -d '{"task_id": "beads-123"}'

# Force a refresh cycle
curl -s -X POST http://127.0.0.1:7432/api/refresh \
  -H "Content-Type: application/json" \
  -d '{}'
```
