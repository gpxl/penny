"""Local HTTP dashboard server.

Auto-starts at app launch. Serves a live dashboard at http://127.0.0.1:7432/
and exposes a JSON API for the penny CLI. Reads/writes app state via
main-thread dispatch (same pattern as bg_worker.py).
"""
import dataclasses
import http.server
import json
import os
import socket
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from .analysis import format_reset_label

PREFERRED_PORT = 7432

# POST endpoints dispatch ObjC selectors on the main thread. A burst of rapid
# calls can queue up work faster than the thread can process it. Rate-limit to
# 10 requests/s steady-state (burst of 20) to protect against accidental loops.
_POST_RATE_CAPACITY = 20     # max burst
_POST_RATE_PER_SECOND = 10   # steady-state refill rate


class _TokenBucket:
    """Thread-safe token bucket for rate limiting."""

    def __init__(self, capacity: float, refill_rate: float) -> None:
        self._capacity = capacity
        self._tokens = float(capacity)
        self._refill_rate = refill_rate  # tokens per second
        self._last_refill = time.monotonic()
        self._lock = threading.Lock()

    def consume(self) -> bool:
        """Return True and consume one token, or return False if rate-limited."""
        with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_refill
            self._tokens = min(self._capacity, self._tokens + elapsed * self._refill_rate)
            self._last_refill = now
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return True
            return False


def _port_file() -> Path:
    env = os.environ.get("PENNY_HOME")
    d = Path(env) if env else Path.home() / ".penny"
    return d / ".dashboard_port"


class DashboardServer:
    def __init__(self, app_ref):
        self._app = app_ref
        self._port: int | None = None
        self._started = False
        self._lock = threading.Lock()

    def ensure_started(self) -> int:
        """Start server if not running. Returns port."""
        with self._lock:
            if self._started:
                return self._port
            port = self._pick_port()
            handler = self._make_handler()
            server = http.server.HTTPServer(("127.0.0.1", port), handler)
            t = threading.Thread(target=server.serve_forever, daemon=True)
            t.start()
            self._port = port
            self._started = True
            try:
                _port_file().write_text(str(port))
            except Exception:
                pass
            return port

    def _pick_port(self) -> int:
        try:
            s = socket.socket()
            s.bind(("127.0.0.1", PREFERRED_PORT))
            s.close()
            return PREFERRED_PORT
        except OSError:
            s = socket.socket()
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
            s.close()
            return port

    def _make_handler(self):
        app = self._app
        rate_limiter = _TokenBucket(_POST_RATE_CAPACITY, _POST_RATE_PER_SECOND)

        def _dispatch(selector: str, obj: Any = None, wait: bool = True) -> None:
            """Dispatch an ObjC selector to the main thread."""
            app.performSelectorOnMainThread_withObject_waitUntilDone_(
                selector, obj, wait
            )

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path in ("/", "/index.html"):
                    body = _DASHBOARD_HTML.encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                elif self.path == "/api/state":
                    payload = _snapshot(app)
                    body = json.dumps(payload, default=str).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                elif self.path == "/api/meta":
                    meta = _meta(app)
                    body = json.dumps(meta, default=str).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    # Route /api/plugin/<name>/... to plugin handler
                    result = _try_plugin_route(app, "GET", self.path, {})
                    if result is not None:
                        self._json(result)
                    else:
                        self.send_error(404)

            def do_POST(self):
                if not rate_limiter.consume():
                    self.send_error(429, "Too Many Requests")
                    return

                length = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(length) if length else b"{}"
                try:
                    payload = json.loads(raw) if raw else {}
                except Exception:
                    payload = {}

                if self.path == "/api/refresh":
                    _dispatch("refreshNow:", None, True)
                    self._ok({"ok": True})

                elif self.path == "/api/quit":
                    self._ok({"ok": True})
                    _dispatch("quitApp:", None, False)

                elif self.path == "/api/run":
                    task_id = payload.get("task_id", "")
                    if not task_id:
                        self.send_error(400, "task_id required")
                        return
                    _dispatch("spawnTaskById:", task_id, True)
                    self._ok({"ok": True})

                elif self.path == "/api/stop-agent":
                    task_id = payload.get("task_id", "")
                    if not task_id:
                        self.send_error(400, "task_id required")
                        return
                    _dispatch("stopAgentByTaskId:", task_id, True)
                    self._ok({"ok": True})

                elif self.path == "/api/dismiss":
                    task_id = payload.get("task_id", "")
                    if not task_id:
                        self.send_error(400, "task_id required")
                        return
                    _dispatch("dismissCompleted:", task_id, True)
                    self._ok({"ok": True})

                elif self.path == "/api/clear-completed":
                    _dispatch("clearAllCompleted:", None, True)
                    self._ok({"ok": True})

                else:
                    # Route /api/plugin/<name>/... to plugin handler
                    result = _try_plugin_route(app, "POST", self.path, payload)
                    if result is not None:
                        self._json(result)
                    else:
                        self.send_error(404)

            def _ok(self, data: dict) -> None:
                self._json(data)

            def _json(self, data: dict) -> None:
                body = json.dumps(data, default=str).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass  # Suppress request logs

        return Handler


def _snapshot(app) -> dict[str, Any]:
    """JSON-serializable snapshot of current app state."""
    state = app.state or {}
    pred = app._prediction
    pred_dict = dataclasses.asdict(pred) if pred is not None else {}

    # Task is a dataclass — asdict() serializes declared fields only
    # (_project_priority is a dynamic attr, not a field — excluded automatically)
    ready = [dataclasses.asdict(t) for t in (app._all_ready_tasks or [])]

    # recently_completed is already deduped and capped at 20 by core
    completed = list(state.get("recently_completed", []))

    # Apply OS 12/24h preference to reset labels (same as popover_vc)
    if pred_dict.get("reset_label"):
        pred_dict["reset_label"] = format_reset_label(pred_dict["reset_label"])
    if pred_dict.get("session_reset_label"):
        pred_dict["session_reset_label"] = format_reset_label(pred_dict["session_reset_label"])

    # Plugin-contributed cards ({name, html} per active plugin)
    plugin_cards: list[dict[str, Any]] = []
    active_plugin_names: list[str] = []
    plugin_mgr = getattr(app, "_plugin_mgr", None)
    if plugin_mgr is not None:
        try:
            plugin_cards = plugin_mgr.get_dashboard_cards(state, app.config or {})
            active_plugin_names = [p.name for p in plugin_mgr.active_plugins]
        except Exception:
            pass

    return {
        "generated_at": datetime.now().isoformat(),
        "state": state,
        "prediction": pred_dict,
        "ready_tasks": ready,
        "completed_this_period": completed,
        "session_history": state.get("session_history", []),
        "plugin_cards": plugin_cards,
        "active_plugins": active_plugin_names,
    }


def _meta(app) -> dict[str, Any]:
    """Return metadata about the running app: active plugins and CLI commands."""
    plugin_mgr = getattr(app, "_plugin_mgr", None)
    active: list[str] = []
    cli_commands: list[dict[str, Any]] = []
    if plugin_mgr is not None:
        try:
            active = [p.name for p in plugin_mgr.active_plugins]
            cli_commands = plugin_mgr.get_all_cli_commands()
        except Exception:
            pass
    return {
        "active_plugins": active,
        "cli_commands": cli_commands,
    }


def _try_plugin_route(app, method: str, path: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    """Route /api/plugin/<name>/<suffix> to the named plugin.

    Returns plugin response dict on success, None if path doesn't match or plugin not found.
    """
    prefix = "/api/plugin/"
    if not path.startswith(prefix):
        return None
    rest = path[len(prefix):]
    parts = rest.split("/", 1)
    plugin_name = parts[0]
    path_suffix = parts[1] if len(parts) > 1 else ""

    plugin_mgr = getattr(app, "_plugin_mgr", None)
    if plugin_mgr is None:
        return None
    return plugin_mgr.handle_dashboard_route(plugin_name, method, path_suffix, payload)


_DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>Penny Dashboard</title>
  <style>
    /* Same design tokens as report.py */
    body { font-family: -apple-system,BlinkMacSystemFont,sans-serif; background:#f9fafb; color:#111827; margin:0; padding:16px; }
    h1 { font-size:18px; margin:0 0 16px; }
    .grid { display:grid; grid-template-columns:1fr 1fr; gap:12px; margin-bottom:12px; }
    .card { background:#fff; border:1px solid #e5e7eb; border-radius:8px; padding:14px; }
    .card h2 { font-size:13px; color:#6b7280; text-transform:uppercase; letter-spacing:.05em; margin:0 0 10px; }
    .bar-track { background:#e5e7eb; border-radius:4px; height:8px; margin:6px 0 2px; }
    .bar-fill { height:8px; border-radius:4px; transition:width .4s; }
    .bar-green { background:#10b981; }
    .bar-blue  { background:#3b82f6; }
    .bar-red   { background:#ef4444; }
    .stat-row { display:flex; justify-content:space-between; font-size:12px; color:#6b7280; }
    .badge { display:inline-block; padding:1px 6px; border-radius:4px; font-size:11px; font-weight:600; }
    .p1 { background:#fef2f2; color:#b91c1c; }
    .p2 { background:#fffbeb; color:#b45309; }
    .p3 { background:#f0fdf4; color:#15803d; }
    .running { background:#fef3c7; color:#b45309; }
    .done { background:#dcfce7; color:#166534; }
    table { width:100%; border-collapse:collapse; font-size:12px; }
    th { text-align:left; color:#6b7280; font-weight:600; padding:4px 8px; border-bottom:1px solid #e5e7eb; }
    td { padding:4px 8px; border-bottom:1px solid #f3f4f6; vertical-align:top; }
    svg text { font-size:10px; fill:#9ca3af; }
    .filter-btns { display:flex; gap:4px; margin-bottom:10px; }
    .filter-btn { background:#f3f4f6; border:1px solid #e5e7eb; border-radius:4px; color:#6b7280; cursor:pointer; font-size:11px; font-weight:600; padding:3px 10px; }
    .filter-btn.active { background:#3b82f6; border-color:#3b82f6; color:#fff; }
  </style>
</head>
<body>
<h1>Penny Dashboard</h1>

<div class="grid">
  <div class="card" id="card-period"><h2>Current Week</h2><p>…</p></div>
  <div class="card" id="card-session"><h2>Current Session</h2><p>…</p></div>
</div>
<div class="card" id="card-history" style="margin-bottom:12px"><h2>Session History</h2><p>…</p></div>
<div class="card" id="card-tasks" style="margin-bottom:12px"><h2>Task Queue</h2><p>…</p></div>
<div class="grid">
  <div class="card" id="card-agents"><h2>Agents Running</h2><p>…</p></div>
  <div class="card" id="card-completed"><h2>Completed This Period</h2><p>…</p></div>
</div>
<div id="plugin-cards-container"></div>

<script>
let lastOk = null;
let lastData = null;
let historyFilter = 'week';

function barColor(pct) {
  if (pct < 70) return 'bar-green';
  if (pct < 90) return 'bar-blue';
  return 'bar-red';
}

function bar(pct, cls) {
  return `<div class="bar-track"><div class="bar-fill ${cls}" style="width:${Math.min(pct,100).toFixed(1)}%"></div></div>`;
}

function renderPeriod(pred) {
  const pa = (pred.pct_all||0).toFixed(1);
  const ps = (pred.pct_sonnet||0).toFixed(1);
  const proj = (pred.projected_pct_all||0).toFixed(1);
  return `
    <div class="stat-row"><span>All Models</span><span><b>${pa}%</b></span></div>
    ${bar(pred.pct_all||0, barColor(pred.pct_all||0))}
    <div class="stat-row"><span>Sonnet Only</span><span><b>${ps}%</b></span></div>
    ${bar(pred.pct_sonnet||0, barColor(pred.pct_sonnet||0))}
    <div class="stat-row" style="margin-top:8px"><span>${(pred.days_remaining||0).toFixed(1)} days remaining</span></div>
    <div class="stat-row"><span>Projected token use</span><span>${proj}%</span></div>
    <div class="stat-row"><span>Resets at</span><span>${pred.reset_label||'–'}</span></div>`;
}

function renderSession(pred) {
  const sp = (pred.session_pct_all||0).toFixed(1);
  return `
    <div class="stat-row"><span>Session Usage</span><span><b>${sp}%</b></span></div>
    ${bar(pred.session_pct_all||0, barColor(pred.session_pct_all||0))}
    <div class="stat-row" style="margin-top:8px"><span>Resets at</span><span>${pred.session_reset_label||'–'}</span></div>
    <div class="stat-row"><span>${(pred.session_hours_remaining||0).toFixed(1)} hours remaining</span></div>`;
}

function filterHistory(history, periodStart) {
  if (historyFilter === 'week') {
    return history.filter(h => (h.start || '') >= (periodStart || ''));
  }
  if (historyFilter === '4w') {
    const cutoff = new Date(Date.now() - 28 * 86400000).toISOString();
    return history.filter(h => (h.start || '') >= cutoff);
  }
  return history; // 'all'
}

function setFilter(f) {
  historyFilter = f;
  if (lastData) {
    const pred = lastData.prediction || {};
    document.getElementById('card-history').innerHTML = '<h2>Session History</h2>' + renderHistory_card(lastData.session_history, pred.period_start);
  }
}

function renderHistory_card(history, periodStart) {
  const filtered = filterHistory(history || [], periodStart);
  const btns = ['week','4w','all'].map(f => {
    const label = {week:'This week', '4w':'4 weeks', all:'All time'}[f];
    return `<button class="filter-btn ${historyFilter===f?'active':''}" onclick="setFilter('${f}')">${label}</button>`;
  }).join('');
  const btnRow = `<div class="filter-btns">${btns}</div>`;
  if (!filtered.length) {
    return btnRow + '<p style="color:#9ca3af;font-size:12px">No sub-session data for this range.</p>';
  }
  const max = Math.max(...filtered.map(h => h.output_all||0), 1);
  const W = 480, H = 80, n = filtered.length;
  const bw = Math.max(Math.floor((W - 20) / n) - 2, 4);
  let bars = '';
  filtered.forEach((h, i) => {
    const val = h.output_all || 0;
    const bh = Math.max(Math.round((val / max) * H), 2);
    const x = 10 + i * (bw + 2);
    const y = H - bh;
    const pct = val / max * 100;
    const fill = pct < 70 ? '#10b981' : pct < 90 ? '#3b82f6' : '#ef4444';
    const startLabel = (h.start||'').slice(5, 10); // MM-DD
    const kTokens = Math.round(val / 1000);
    bars += `<rect x="${x}" y="${y}" width="${bw}" height="${bh}" fill="${fill}" rx="2"><title>${startLabel}: ${kTokens}k tokens</title></rect>`;
    if (n <= 8 || i % Math.ceil(n/6) === 0) bars += `<text x="${x + bw/2}" y="${H+14}" text-anchor="middle">${startLabel}</text>`;
  });
  return btnRow + `<svg viewBox="0 0 ${W} ${H+20}" style="width:100%;height:auto">${bars}</svg>`;
}

function renderTasks(tasks, activePlugins) {
  if (!tasks || !tasks.length) {
    if (!activePlugins || !activePlugins.includes('beads')) {
      return '<p style="color:#9ca3af;font-size:12px">Task management not active. ' +
             'Install the <a href="https://github.com/steveyegge/beads" target="_blank">beads CLI</a> ' +
             '(<code>brew install beads</code>) to enable automatic task spawning.</p>';
    }
    return '<p style="color:#9ca3af;font-size:12px">No ready tasks.</p>';
  }
  const rows = tasks.map(t => {
    const cls = {P1:'p1',P2:'p2',P3:'p3'}[t.priority]||'p3';
    return `<tr><td>${t.task_id}</td><td>${t.project_name}</td><td><span class="badge ${cls}">${t.priority}</span></td><td>${t.title}</td></tr>`;
  }).join('');
  return `<table><thead><tr><th>ID</th><th>Project</th><th>Pri</th><th>Title</th></tr></thead><tbody>${rows}</tbody></table>`;
}

function renderAgents(agents) {
  if (!agents || !agents.length) return '<p style="color:#9ca3af;font-size:12px">None running.</p>';
  const rows = agents.map(a =>
    `<tr><td>${a.task_id}</td><td>${a.project_name||''}</td><td>${a.title||''}</td><td><span class="badge running">● running</span></td></tr>`
  ).join('');
  return `<table><thead><tr><th>ID</th><th>Project</th><th>Task</th><th>Status</th></tr></thead><tbody>${rows}</tbody></table>`;
}

function renderCompleted(items) {
  if (!items || !items.length) return '<p style="color:#9ca3af;font-size:12px">None this period.</p>';
  const rows = items.map(a =>
    `<tr><td>${a.task_id}</td><td>${a.project_name||''}</td><td>${a.title||''}</td><td><span class="badge done">✓ done</span></td></tr>`
  ).join('');
  return `<table><thead><tr><th>ID</th><th>Project</th><th>Task</th><th>Status</th></tr></thead><tbody>${rows}</tbody></table>`;
}

function renderPluginCards(cards) {
  const container = document.getElementById('plugin-cards-container');
  if (!container) return;
  container.innerHTML = (cards || []).map(c =>
    `<div class="card" style="margin-bottom:12px">${c.html}</div>`
  ).join('');
}

function render(data) {
  lastData = data;
  const pred = data.prediction || {};
  const state = data.state || {};
  document.getElementById('card-period').innerHTML = '<h2>Current Week</h2>' + renderPeriod(pred);
  document.getElementById('card-session').innerHTML = '<h2>Current Session</h2>' + renderSession(pred);
  document.getElementById('card-history').innerHTML = '<h2>Session History</h2>' + renderHistory_card(data.session_history, pred.period_start);
  document.getElementById('card-tasks').innerHTML = '<h2>Task Queue (' + (data.ready_tasks||[]).length + ' ready)</h2>' + renderTasks(data.ready_tasks, data.active_plugins);
  document.getElementById('card-agents').innerHTML = '<h2>Agents Running (' + (state.agents_running||[]).length + ')</h2>' + renderAgents(state.agents_running);
  const completed = data.completed_this_period || [];
  document.getElementById('card-completed').innerHTML = '<h2>Completed This Period (' + completed.length + ')</h2>' + renderCompleted(completed);
  renderPluginCards(data.plugin_cards);
}

async function refresh() {
  try {
    const resp = await fetch('/api/state');
    if (!resp.ok) throw new Error(resp.status);
    const data = await resp.json();
    render(data);
    lastOk = Date.now();
  } catch (e) {
    // reconnecting silently
  }
}

refresh();
setInterval(refresh, 10000);
</script>
</body>
</html>"""
