"""Local HTTP dashboard server.

Starts lazily on first 'View Report' click. Serves a polling live dashboard
at http://127.0.0.1:7432/. Reads app state read-only (GIL-safe).
"""
import dataclasses
import http.server
import json
import socket
import threading
from datetime import datetime
from typing import Any

PREFERRED_PORT = 7432


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
                else:
                    self.send_error(404)

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

    # Deduplicate spawned_this_week by task_id (most recent entry wins)
    seen: set[str] = set()
    completed: list = []
    for agent in reversed(state.get("spawned_this_week", [])):
        tid = agent.get("task_id", "")
        if tid not in seen:
            seen.add(tid)
            completed.append(agent)
    completed.reverse()

    return {
        "generated_at": datetime.now().isoformat(),
        "state": state,
        "prediction": pred_dict,
        "ready_tasks": ready,
        "completed_this_period": completed,
        "session_history": state.get("session_history", []),
    }


_DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>Nae Nae — Live Dashboard</title>
  <style>
    /* Same design tokens as report.py */
    body { font-family: -apple-system,BlinkMacSystemFont,sans-serif; background:#f9fafb; color:#111827; margin:0; padding:16px; }
    h1 { font-size:18px; margin:0 0 4px; }
    .subtitle { color:#6b7280; font-size:13px; margin:0 0 16px; }
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
    .stale { color:#ef4444; font-size:11px; }
    svg text { font-size:10px; fill:#9ca3af; }
  </style>
</head>
<body>
<h1>Nae Nae — Live Dashboard</h1>
<p class="subtitle" id="subtitle">Loading…</p>

<div class="grid">
  <div class="card" id="card-period"><h2>Billing Period Usage</h2><p>…</p></div>
  <div class="card" id="card-session"><h2>Current Sub-Session</h2><p>…</p></div>
</div>
<div class="card" id="card-history" style="margin-bottom:12px"><h2>Session History</h2><p>…</p></div>
<div class="card" id="card-tasks" style="margin-bottom:12px"><h2>Task Queue</h2><p>…</p></div>
<div class="grid">
  <div class="card" id="card-agents"><h2>Agents Running</h2><p>…</p></div>
  <div class="card" id="card-completed"><h2>Completed This Period</h2><p>…</p></div>
</div>

<script>
let lastOk = null;

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
    <div class="stat-row" style="margin-top:8px"><span>Days remaining</span><span>${(pred.days_remaining||0).toFixed(1)}</span></div>
    <div class="stat-row"><span>Projected end-of-period</span><span>${proj}%</span></div>
    <div class="stat-row"><span>Resets</span><span>${pred.reset_label||'–'}</span></div>`;
}

function renderSession(pred) {
  const sp = (pred.session_pct_all||0).toFixed(1);
  return `
    <div class="stat-row"><span>Session Usage</span><span><b>${sp}%</b></span></div>
    ${bar(pred.session_pct_all||0, barColor(pred.session_pct_all||0))}
    <div class="stat-row" style="margin-top:8px"><span>Resets</span><span>${pred.session_reset_label||'–'}</span></div>
    <div class="stat-row"><span>Hours remaining</span><span>${(pred.session_hours_remaining||0).toFixed(1)}</span></div>`;
}

function renderHistory(history) {
  if (!history || !history.length) return '<p style="color:#9ca3af;font-size:12px">No sub-session data yet.</p>';
  const max = Math.max(...history.map(h => h.output_all||0), 1);
  const W = 480, H = 80, n = history.length;
  const bw = Math.max(Math.floor((W - 20) / n) - 2, 4);
  let bars = '';
  history.forEach((h, i) => {
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
  return `<svg viewBox="0 0 ${W} ${H+20}" style="width:100%;height:auto">${bars}</svg>`;
}

function renderTasks(tasks) {
  if (!tasks || !tasks.length) return '<p style="color:#9ca3af;font-size:12px">No ready tasks.</p>';
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

function render(data) {
  const pred = data.prediction || {};
  const state = data.state || {};
  document.getElementById('card-period').innerHTML = '<h2>Billing Period Usage</h2>' + renderPeriod(pred);
  document.getElementById('card-session').innerHTML = '<h2>Current Sub-Session</h2>' + renderSession(pred);
  document.getElementById('card-history').innerHTML = '<h2>Session History</h2>' + renderHistory(data.session_history);
  document.getElementById('card-tasks').innerHTML = '<h2>Task Queue (' + (data.ready_tasks||[]).length + ' ready)</h2>' + renderTasks(data.ready_tasks);
  document.getElementById('card-agents').innerHTML = '<h2>Agents Running (' + (state.agents_running||[]).length + ')</h2>' + renderAgents(state.agents_running);
  const completed = data.completed_this_period || [];
  document.getElementById('card-completed').innerHTML = '<h2>Completed This Period (' + completed.length + ')</h2>' + renderCompleted(completed);
}

async function refresh() {
  try {
    const resp = await fetch('/api/state');
    if (!resp.ok) throw new Error(resp.status);
    const data = await resp.json();
    render(data);
    lastOk = Date.now();
    document.getElementById('subtitle').textContent = 'Updated just now · auto-refreshes every 10s';
    document.getElementById('subtitle').className = 'subtitle';
  } catch (e) {
    const ago = lastOk ? Math.round((Date.now() - lastOk) / 1000) + 's ago' : 'never';
    document.getElementById('subtitle').innerHTML = `<span class="stale">⚠ Reconnecting… last update ${ago}</span>`;
  }
}

refresh();
setInterval(refresh, 10000);

// Keep subtitle "N seconds ago" counter fresh
setInterval(() => {
  if (lastOk) {
    const s = Math.round((Date.now() - lastOk) / 1000);
    if (s > 5) document.getElementById('subtitle').textContent = `Updated ${s}s ago · auto-refreshes every 10s`;
  }
}, 1000);
</script>
</body>
</html>"""
