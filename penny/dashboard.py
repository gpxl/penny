"""Local HTTP dashboard server.

Auto-starts at app launch. Serves a live dashboard at http://127.0.0.1:7432/
and exposes a JSON API for the penny CLI. Reads/writes app state via
main-thread dispatch (same pattern as bg_worker.py).
"""
from __future__ import annotations

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

_state_generation: int = 0


def bump_state_generation() -> None:
    """Increment the ETag generation. Called after each successful data fetch."""
    global _state_generation
    _state_generation += 1


def _state_etag() -> str:
    return f'"{_state_generation}"'


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
                    etag = _state_etag()
                    if self.headers.get("If-None-Match") == etag:
                        self.send_response(304)
                        self.send_header("ETag", etag)
                        self.end_headers()
                    else:
                        payload = _snapshot(app)
                        body = json.dumps(payload, default=str).encode()
                        self.send_response(200)
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Cache-Control", "no-cache")
                        self.send_header("ETag", etag)
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
    # Computed for plugin use only — not returned in the response
    ready_tasks = [dataclasses.asdict(t) for t in (app._all_ready_tasks or [])]

    # Apply OS 12/24h preference to reset labels (same as popover_vc)
    if pred_dict.get("reset_label"):
        pred_dict["reset_label"] = format_reset_label(pred_dict["reset_label"])
    if pred_dict.get("session_reset_label"):
        pred_dict["session_reset_label"] = format_reset_label(pred_dict["session_reset_label"])
    if pred_dict.get("reset_label_sonnet"):
        pred_dict["reset_label_sonnet"] = format_reset_label(pred_dict["reset_label_sonnet"])

    # Plugin-contributed cards ({name, html} per active plugin)
    plugin_cards: list[dict[str, Any]] = []
    active_plugin_names: list[str] = []
    plugin_mgr = getattr(app, "_plugin_mgr", None)
    if plugin_mgr is not None:
        try:
            augmented_state = {**state, "ready_tasks": ready_tasks}
            plugin_cards = plugin_mgr.get_dashboard_cards(augmented_state, app.config or {})
            active_plugin_names = [p.name for p in plugin_mgr.active_plugins]
        except Exception:
            pass

    return {
        "generated_at": datetime.now().isoformat(),
        "state": state,
        "prediction": pred_dict,
        "session_history": state.get("session_history", []),
        "period_history": state.get("period_history", []),
        "plugin_cards": plugin_cards,
        "active_plugins": active_plugin_names,
        "rich_metrics": state.get("rich_metrics", {}),
        "rich_metrics_by_window": state.get("rich_metrics_by_window", {}),
        "intraday_samples": state.get("intraday_samples", []),
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
    .bar-yellow { background:#eab308; }
    .bar-red   { background:#ef4444; }
    .stat-row { display:flex; justify-content:space-between; font-size:12px; color:#6b7280; }
    table { width:100%; border-collapse:collapse; font-size:12px; }
    th { text-align:left; color:#6b7280; font-weight:600; padding:4px 8px; border-bottom:1px solid #e5e7eb; }
    td { padding:4px 8px; border-bottom:1px solid #f3f4f6; vertical-align:top; }
    svg text { font-size:10px; fill:#9ca3af; }
    .filter-btns { display:flex; gap:4px; margin-bottom:10px; }
    .filter-btn { background:#f3f4f6; border:1px solid #e5e7eb; border-radius:4px; color:#6b7280; cursor:pointer; font-size:11px; font-weight:600; padding:3px 10px; }
    .filter-btn.active { background:#3b82f6; border-color:#3b82f6; color:#fff; }
    .bar-g .tip { display:none; pointer-events:none; }
    .bar-g:hover .tip { display:block; }
    .info-tip { display:inline-block; position:relative; color:#9ca3af; cursor:help; margin-left:4px; font-size:10px; vertical-align:middle; }
    .info-tip .tip-text { display:none; position:absolute; bottom:130%; left:50%; transform:translateX(-50%); background:#1f2937; color:#f9fafb; padding:6px 10px; border-radius:6px; font-size:11px; line-height:1.4; white-space:normal; text-align:left; width:220px; z-index:20; pointer-events:none; box-shadow:0 2px 8px rgba(0,0,0,0.3); }
    .info-tip:hover .tip-text { display:block; }
    svg { overflow:visible; }
    .seg-bar { display:flex; height:14px; border-radius:6px; overflow:hidden; margin:8px 0 4px; }
    .seg-bar span { display:block; transition:width .4s; }
    .sparkline-wrap { margin-top:8px; }
    @keyframes barGrowUp { from { transform: scaleY(0); } to { transform: scaleY(1); } }
  </style>
</head>
<body>
<h1>Penny Dashboard</h1>

<div class="grid">
  <div class="card" id="card-period"><h2>Weekly Budget</h2><p>…</p></div>
  <div class="card" id="card-session"><h2>Session Budget</h2><p>…</p></div>
</div>
<div id="metrics-filter-bar" class="filter-btns"></div>
<div class="grid">
  <div class="card" id="card-model-cache"><h2>Model &amp; Cache Efficiency</h2><p>…</p></div>
  <div class="card" id="card-top-tools"><h2>Top Tools</h2><p>…</p></div>
</div>
<div class="grid">
  <div class="card" id="card-history"><h2>Session History</h2><p>…</p></div>
  <div class="card" id="card-activity-hour"><h2>Activity by Hour</h2><p>…</p></div>
</div>
<div class="card" id="card-weekly-history" style="margin-bottom:12px"><h2>Weekly Budget History</h2><p>…</p></div>
<div id="plugin-cards-container"></div>

<script>
let lastOk = null;
let lastData = null;
let historyFilter = 'week';
let metricsFilter = 'month';
let historyFilterAutoSet = false;

function autoSelectHistoryFilter(history) {
  if (historyFilterAutoSet) return;
  historyFilterAutoSet = true;
  const now = Date.now();
  const weekCutoff = new Date(now - 7 * 86400000).toISOString();
  const fourWeekCutoff = new Date(now - 28 * 86400000).toISOString();
  const weekData = (history || []).filter(h => (h.start || '') >= weekCutoff);
  if (weekData.length > 0) { historyFilter = 'week'; return; }
  const fourWeekData = (history || []).filter(h => (h.start || '') >= fourWeekCutoff);
  if (fourWeekData.length > 0) { historyFilter = '4w'; return; }
  if ((history || []).length > 0) { historyFilter = 'all'; return; }
  historyFilter = 'week';
}

function tip(text) {
  return `<span class="info-tip">ⓘ<span class="tip-text">${text}</span></span>`;
}

function barColor(pct) {
  if (pct < 60) return 'bar-green';
  if (pct < 80) return 'bar-yellow';
  return 'bar-red';
}

function bar(pct, cls) {
  return `<div class="bar-track"><div class="bar-fill ${cls}" style="width:${Math.min(pct,100).toFixed(1)}%"></div></div>`;
}

function renderSparkline(samples) {
  const cutoff = Date.now() - 24 * 3600 * 1000;
  const recent = (samples || []).filter(s => new Date(s.ts).getTime() >= cutoff);
  if (recent.length < 2) return '';
  const W = 300, H = 28;
  const vals = recent.map(s => s.pct_all || 0);
  const minV = Math.min(...vals), maxV = Math.max(...vals);
  const range = maxV - minV || 1;
  const pts = recent.map((s, i) => {
    const x = (i / (recent.length - 1)) * W;
    const y = H - ((s.pct_all - minV) / range) * (H - 4) - 2;
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  }).join(' ');
  return `<div class="sparkline-wrap"><svg viewBox="0 0 ${W} ${H}" style="width:100%;height:${H}px;display:block" preserveAspectRatio="none"><polyline points="${pts}" fill="none" stroke="#3b82f6" stroke-width="1.5" stroke-linejoin="round"/></svg><div class="stat-row" style="margin-top:2px"><span style="font-size:11px;color:#9ca3af">Burn rate (last 24h)${tip("How your weekly % usage changed over the past 24 hours. A steep rise means a heavy session.")}</span><span style="font-size:11px;color:#9ca3af">${minV.toFixed(1)}%–${maxV.toFixed(1)}%</span></div></div>`;
}

function renderPeriod(pred, samples) {
  const pa = (pred.pct_all||0).toFixed(1);
  const ps = (pred.pct_sonnet||0).toFixed(1);
  const proj = (pred.projected_pct_all||0).toFixed(1);
  return `
    <div class="stat-row"><span>All models${tip("Total output tokens across Opus, Sonnet, and Haiku. This is the primary limit Anthropic tracks.")}</span><span><b>${pa}%</b></span></div>
    ${bar(pred.pct_all||0, barColor(pred.pct_all||0))}
    <div class="stat-row" style="font-size:0.85em;color:#888"><span>Resets at</span><span>${pred.reset_label||'–'}</span></div>
    <div class="stat-row"><span>Sonnet only${tip("Output tokens from Sonnet models only. Anthropic applies a separate sublimit for Sonnet with its own reset schedule.")}</span><span><b>${ps}%</b></span></div>
    ${bar(pred.pct_sonnet||0, barColor(pred.pct_sonnet||0))}
    <div class="stat-row" style="font-size:0.85em;color:#888"><span>Resets at</span><span>${pred.reset_label_sonnet||pred.reset_label||'–'}</span></div>
    <div class="stat-row" style="margin-top:8px"><span>${(pred.days_remaining||0).toFixed(1)} days remaining</span></div>
    <div class="stat-row"><span>Projected token use${tip("Extrapolates your current daily burn rate to end-of-week. Red means you'll likely hit the limit before reset.")}</span><span>${proj}%</span></div>
    ${renderSparkline(samples)}`;
}

function renderModelCache(rm, wl) {
  wl = wl || 'last 28 days';
  const opus = rm.opus_tokens || 0;
  const sonnet = rm.sonnet_tokens || 0;
  const haiku = rm.haiku_tokens || 0;
  const other = rm.other_tokens || 0;
  const total = opus + sonnet + haiku + other || 1;
  const cc = rm.cache_create_tokens || 0;
  const cr = rm.cache_read_tokens || 0;
  const cacheTotal = cc + cr || 1;
  const hitRate = (cr / cacheTotal * 100).toFixed(1);
  const freeK = Math.round(cr / 1000);

  const pct = v => (v / total * 100).toFixed(1);
  const seg = (v, color) => v > 0 ? `<span style="width:${pct(v)}%;background:${color}"></span>` : '';

  const segBar = `<div class="seg-bar">${seg(opus,'#7c3aed')}${seg(sonnet,'#3b82f6')}${seg(haiku,'#10b981')}${seg(other,'#9ca3af')}</div>`;
  const legend = `<div class="stat-row" style="flex-wrap:wrap;gap:4px 12px">
    ${opus>0?`<span style="color:#7c3aed">■ Opus ${pct(opus)}%</span>`:''}
    ${sonnet>0?`<span style="color:#3b82f6">■ Sonnet ${pct(sonnet)}%</span>`:''}
    ${haiku>0?`<span style="color:#10b981">■ Haiku ${pct(haiku)}%</span>`:''}
    ${other>0?`<span style="color:#9ca3af">■ Other ${pct(other)}%</span>`:''}
  </div>`;

  const totalTurns = rm.total_turns || 0;
  const subTurns = rm.subagent_turns || 0;
  const subPct = totalTurns > 0 ? (subTurns / totalTurns * 100).toFixed(0) : 0;

  const sessionCount = rm.session_count || 0;
  const avgTurns = sessionCount > 0 ? (totalTurns / sessionCount).toFixed(1) : '—';
  const agenticPct = totalTurns > 0 ? ((rm.agentic_turns || 0) / totalTurns * 100).toFixed(0) + '%' : '—';
  const thinkingPct = totalTurns > 0 ? ((rm.thinking_turns || 0) / totalTurns * 100).toFixed(0) + '%' : '—';

  return segBar + legend + `
    <div class="stat-row" style="margin-top:8px"><span>Cache hit rate${tip("Fraction of input tokens served from Anthropic's prompt cache. Higher = cheaper & faster. Cache tokens are free beyond the creation cost.")}</span><span data-stat="cache-hit-rate" data-value="${parseFloat(hitRate)}">${hitRate}% (${freeK}k free tokens)</span></div>
    <div class="stat-row"><span>Total turns${tip("Total assistant responses across all projects (" + wl + ").")}</span><span data-stat="total-turns" data-value="${totalTurns}">${totalTurns}</span></div>
    <div class="stat-row"><span>Subagent turns${tip("Turns that ran inside a sub-agent (Claude Code Agent tool). These are spawned by your main sessions and run in parallel.")}</span><span data-stat="subagent-turns" data-value="${subTurns}">${subTurns} (${subPct}%)</span></div>
    <div class="stat-row"><span>PRs created${tip("Pull requests created via the gh CLI (detected from JSONL pr-link records).")}</span><span data-stat="pr-count" data-value="${rm.pr_count || 0}">${rm.pr_count || 0}</span></div>
    <div class="stat-row"><span>Projects / branches${tip("Unique working directories and git branches seen across all conversations (" + wl + ").")}</span><span data-stat="projects-branches">${rm.unique_projects || 0} / ${rm.unique_branches || 0}</span></div>
    <div class="stat-row"><span>Sessions${tip("Distinct Claude Code sessions (" + wl + "). One session = one JSONL file.")}</span><span data-stat="sessions" data-value="${sessionCount}">${sessionCount}</span></div>
    <div class="stat-row"><span>Avg turns / session${tip("Mean number of assistant responses per session. High values indicate long, iterative coding tasks.")}</span><span data-stat="avg-turns" data-value="${sessionCount > 0 ? (totalTurns / sessionCount) : 0}">${avgTurns}</span></div>
    <div class="stat-row"><span>Agentic ratio${tip("% of turns where Claude called a tool (stop_reason=tool_use). Higher = more autonomous, tool-driven work.")}</span><span data-stat="agentic-pct">${agenticPct}</span></div>
    <div class="stat-row"><span>Extended thinking${tip("% of turns where Claude used extended thinking (visible reasoning). Enabled automatically for complex problems.")}</span><span data-stat="thinking-pct">${thinkingPct}</span></div>
    <div class="stat-row"><span>Web searches${tip("Total WebSearch tool calls (" + wl + "). Claude uses these to look up docs, APIs, or current info.")}</span><span data-stat="web-searches" data-value="${rm.web_search_count || 0}">${rm.web_search_count || 0}</span></div>
    <div class="stat-row"><span>Web fetches${tip("Total WebFetch calls (direct URL reads). Counted separately from searches.")}</span><span data-stat="web-fetches" data-value="${rm.web_fetch_count || 0}">${rm.web_fetch_count || 0}</span></div>
    <div class="stat-row"><span>Files edited${tip("Unique file paths modified via Edit or Write tools (" + wl + ").")}</span><span data-stat="files-edited" data-value="${rm.files_edited || 0}">${rm.files_edited || 0}</span></div>
    <div class="stat-row"><span>Tool errors${tip("Times a tool returned an error result (e.g. file not found, command failed). Low numbers are expected.")}</span><span data-stat="tool-errors" data-value="${rm.tool_error_count || 0}">${rm.tool_error_count || 0}</span></div>`;
}

function renderActivityHour(rm, wl) {
  wl = wl || 'last 28 days';
  const activity = rm.hourly_activity || Array(24).fill(0);
  const maxV = Math.max(...activity, 1);
  const W = 480, H = 60, PAD_B = 20, PAD_L = 0;
  const colW = W / 24;
  let cols = '';
  activity.forEach((v, h) => {
    const bh = Math.max((v / maxV) * H, v > 0 ? 2 : 0);
    const x = PAD_L + h * colW;
    const y = H - bh;
    const intensity = v / maxV;
    const blue = Math.round(59 + intensity * (37 - 59));
    const green = Math.round(130 + intensity * (99 - 130));
    const redC = Math.round(246 + intensity * (29 - 246));
    const fill = v > 0 ? `rgb(${redC},${green},${blue})` : '#e5e7eb';
    const tipW = 70, tipH = 28;
    const tipX = x + colW/2 - tipW/2;
    const tipY = Math.max(y - tipH - 4, 0);
    cols += `<g class="bar-g">`;
    cols += `<rect x="${x}" y="0" width="${colW}" height="${H}" fill="transparent"/>`;
    cols += `<rect data-hour="${h}" x="${x+1}" y="${y}" width="${colW-2}" height="${bh}" fill="${fill}" rx="2"/>`;
    cols += `<g class="tip"><rect x="${tipX}" y="${tipY}" width="${tipW}" height="${tipH}" rx="3" fill="#1f2937" opacity="0.9"/>`;
    cols += `<text x="${tipX+tipW/2}" y="${tipY+11}" text-anchor="middle" fill="#f9fafb" font-size="9">${h}:00</text>`;
    cols += `<text x="${tipX+tipW/2}" y="${tipY+22}" text-anchor="middle" fill="#f9fafb" font-size="9" data-hour-tip="${h}">${v} turns</text></g>`;
    cols += `</g>`;
  });
  const labels = [0,6,12,18].map(h => {
    const x = PAD_L + h * colW + colW/2;
    const label = h === 0 ? 'midnight' : h === 12 ? 'noon' : `${h < 12 ? h+'a' : (h-12)+'p'}m`;
    return `<text x="${x}" y="${H + PAD_B - 4}" text-anchor="middle">${label}</text>`;
  }).join('');
  return `<p style="color:#6b7280;font-size:12px;margin:0 0 8px">Assistant turns by time of day (${wl}) ${tip("Each column is one hour of your local day. Bars show how many assistant responses (turns) occurred in that hour. Hover a bar for the exact count.")}</p>
    <svg viewBox="0 0 ${W} ${H + PAD_B}" style="width:100%;height:auto;overflow:visible">${cols}${labels}</svg>`;
}

function renderTopTools(rm, wl) {
  wl = wl || 'last 28 days';
  const counts = rm.tool_counts || {};
  const entries = Object.entries(counts).sort((a,b) => b[1]-a[1]).slice(0,8);
  if (!entries.length) return '<p style="color:#9ca3af;font-size:12px">No tool usage data yet.</p>';
  const maxV = entries[0][1] || 1;
  const W = 280, H = 14, GAP = 4;
  const colors = ['#10b981','#10b981','#3b82f6','#3b82f6','#6366f1','#6366f1','#9ca3af','#9ca3af'];
  const totalH = entries.length * (H + GAP);
  const PAD_L = 100;
  let allBars = '';
  entries.forEach(([name, cnt], i) => {
    const bw = Math.max((cnt / maxV) * (W), 2);
    const y = i * (H + GAP);
    const label = name.length > 14 ? name.slice(0,13)+'…' : name;
    const tipW = 80, tipH = 28;
    const tipY = Math.max(y - tipH - 2, 0);
    allBars += `<g class="bar-g" data-tool="${name}">`;
    allBars += `<rect x="${PAD_L}" y="${y}" width="${W}" height="${H}" fill="transparent"/>`;
    allBars += `<rect x="${PAD_L}" y="${y}" width="${bw}" height="${H}" fill="${colors[i]}" rx="2" data-tool-bar="${name}"/>`;
    allBars += `<text x="${PAD_L+bw+4}" y="${y+H-2}" fill="#374151" font-size="10" data-tool-cnt="${name}">${cnt}</text>`;
    allBars += `<text x="${PAD_L-4}" y="${y+H-2}" text-anchor="end" fill="#6b7280" font-size="10">${label}</text>`;
    allBars += `<g class="tip"><rect x="${PAD_L}" y="${tipY}" width="${tipW}" height="${tipH}" rx="3" fill="#1f2937" opacity="0.9"/>`;
    allBars += `<text x="${PAD_L+tipW/2}" y="${tipY+11}" text-anchor="middle" fill="#f9fafb" font-size="9">${name}</text>`;
    allBars += `<text x="${PAD_L+tipW/2}" y="${tipY+22}" text-anchor="middle" fill="#f9fafb" font-size="9">${cnt} calls</text></g>`;
    allBars += `</g>`;
  });
  const svgW = PAD_L + W + 40;
  return `<p style="color:#6b7280;font-size:12px;margin:0 0 8px">Tool calls by frequency (${wl}) ${tip("Shows the 8 most-used Claude Code built-in tools. Each bar is the total invocation count. Hover for exact numbers.")}</p><svg viewBox="0 0 ${svgW} ${totalH}" style="width:100%;height:auto;overflow:visible">${allBars}</svg>`;
}

function updateModelCache(rm, wl) {
  const card = document.getElementById('card-model-cache');
  if (!card) return;
  const opus = rm.opus_tokens || 0;
  const sonnet = rm.sonnet_tokens || 0;
  const haiku = rm.haiku_tokens || 0;
  const other = rm.other_tokens || 0;
  const total = opus + sonnet + haiku + other || 1;
  const pct = v => (v / total * 100).toFixed(1);
  // Update segmented bar spans — CSS transition handles the animation
  const spans = card.querySelectorAll('.seg-bar span');
  const vals = [opus, sonnet, haiku, other];
  spans.forEach((sp, i) => { if (vals[i] !== undefined) sp.style.width = pct(vals[i]) + '%'; });
  // Update stat numbers
  const cc = rm.cache_create_tokens || 0;
  const cr = rm.cache_read_tokens || 0;
  const cacheTotal = cc + cr || 1;
  const hitRate = cr / cacheTotal * 100;
  const freeK = Math.round(cr / 1000);
  const totalTurns = rm.total_turns || 0;
  const subTurns = rm.subagent_turns || 0;
  const subPct = totalTurns > 0 ? (subTurns / totalTurns * 100).toFixed(0) : 0;
  const sessionCount = rm.session_count || 0;
  const avgTurns = sessionCount > 0 ? (totalTurns / sessionCount) : 0;
  const statMap = {
    'cache-hit-rate': {text: hitRate.toFixed(1) + '% (' + freeK + 'k free tokens)', value: hitRate},
    'total-turns': {value: totalTurns},
    'subagent-turns': {text: subTurns + ' (' + subPct + '%)', value: subTurns},
    'pr-count': {value: rm.pr_count || 0},
    'projects-branches': {text: (rm.unique_projects || 0) + ' / ' + (rm.unique_branches || 0)},
    'sessions': {value: sessionCount},
    'avg-turns': {value: avgTurns, decimals: 1},
    'agentic-pct': {text: totalTurns > 0 ? ((rm.agentic_turns || 0) / totalTurns * 100).toFixed(0) + '%' : '—'},
    'thinking-pct': {text: totalTurns > 0 ? ((rm.thinking_turns || 0) / totalTurns * 100).toFixed(0) + '%' : '—'},
    'web-searches': {value: rm.web_search_count || 0},
    'web-fetches': {value: rm.web_fetch_count || 0},
    'files-edited': {value: rm.files_edited || 0},
    'tool-errors': {value: rm.tool_error_count || 0}
  };
  for (const [key, info] of Object.entries(statMap)) {
    const el = card.querySelector('[data-stat="' + key + '"]');
    if (!el) continue;
    if (info.text !== undefined) { el.textContent = info.text; if (info.value !== undefined) el.setAttribute('data-value', info.value); }
    else if (info.value !== undefined) animateNumber(el, info.value, {decimals: info.decimals || 0});
  }
}

function updateActivityHour(rm, wl) {
  const card = document.getElementById('card-activity-hour');
  if (!card) return;
  const activity = rm.hourly_activity || Array(24).fill(0);
  const maxV = Math.max(...activity, 1);
  const H = 60;
  const W = 480;
  const colW = W / 24;
  for (let h = 0; h < 24; h++) {
    const el = card.querySelector('[data-hour="' + h + '"]');
    if (!el) continue;
    const v = activity[h];
    const bh = Math.max((v / maxV) * H, v > 0 ? 2 : 0);
    const y = H - bh;
    const intensity = v / maxV;
    const blue = Math.round(59 + intensity * (37 - 59));
    const green = Math.round(130 + intensity * (99 - 130));
    const redC = Math.round(246 + intensity * (29 - 246));
    const fill = v > 0 ? 'rgb(' + redC + ',' + green + ',' + blue + ')' : '#e5e7eb';
    animateAttr(el, 'y', y);
    animateAttr(el, 'height', bh);
    el.setAttribute('fill', fill);
    const tipText = card.querySelector('[data-hour-tip="' + h + '"]');
    if (tipText) tipText.textContent = v + ' turns';
  }
  // Update the description paragraph
  const p = card.querySelector('p');
  if (p) p.innerHTML = 'Assistant turns by time of day (' + (wl || 'last 28 days') + ') ' + tip("Each column is one hour of your local day. Bars show how many assistant responses (turns) occurred in that hour. Hover a bar for the exact count.");
}

function updateTopTools(rm, wl) {
  const card = document.getElementById('card-top-tools');
  if (!card) return;
  const counts = rm.tool_counts || {};
  const entries = Object.entries(counts).sort((a,b) => b[1]-a[1]).slice(0,8);
  if (!entries.length) return;
  const maxV = entries[0][1] || 1;
  const W = 280;
  // Check if the tool set changed significantly
  const oldTools = card.querySelectorAll('[data-tool]');
  const oldNames = new Set();
  oldTools.forEach(g => oldNames.add(g.getAttribute('data-tool')));
  const newNames = new Set(entries.map(e => e[0]));
  let changed = oldNames.size !== newNames.size;
  if (!changed) oldNames.forEach(n => { if (!newNames.has(n)) changed = true; });
  if (changed) {
    // Tool set changed — full re-render with description update
    card.innerHTML = '<h2>Top Tools' + tip("Which Claude Code tools were invoked most often (" + (wl || 'last 28 days') + "). Counts individual tool calls, not conversations.") + '</h2>' + renderTopTools(rm, wl);
    return;
  }
  // Same tools — animate in place
  const PAD_L = 100;
  entries.forEach(([name, cnt]) => {
    const barEl = card.querySelector('[data-tool-bar="' + name + '"]');
    const cntEl = card.querySelector('[data-tool-cnt="' + name + '"]');
    const bw = Math.max((cnt / maxV) * W, 2);
    if (barEl) animateAttr(barEl, 'width', bw);
    if (cntEl) {
      animateAttr(cntEl, 'x', PAD_L + bw + 4);
      animateNumber(cntEl, cnt);
    }
  });
  const p = card.querySelector('p');
  if (p) p.innerHTML = 'Tool calls by frequency (' + (wl || 'last 28 days') + ') ' + tip("Shows the 8 most-used Claude Code built-in tools. Each bar is the total invocation count. Hover for exact numbers.");
}

function renderSession(pred) {
  const sp = (pred.session_pct_all||0).toFixed(1);
  return `
    <div class="stat-row"><span>Session Usage${tip("Output tokens since the current sub-session started (after the last rate-limit reset). Shown as % of estimated session budget.")}</span><span><b>${sp}%</b></span></div>
    ${bar(pred.session_pct_all||0, barColor(pred.session_pct_all||0))}
    <div class="stat-row" style="margin-top:8px"><span>Resets at${tip("Estimated time your session budget refills. Claude enforces ~5h rolling windows; this is inferred from past rate-limit messages.")}</span><span>${pred.session_reset_label||'–'}</span></div>
    <div class="stat-row"><span>${(pred.session_hours_remaining||0).toFixed(1)} hours remaining</span></div>`;
}

function filterHistory(history) {
  if (historyFilter === 'week') {
    const cutoff = new Date(Date.now() - 7 * 86400000).toISOString();
    return history.filter(h => (h.start || '') >= cutoff);
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
    document.getElementById('card-history').innerHTML = '<h2>Session History</h2>' + renderHistory_card(lastData.session_history);
  }
}

function setMetricsFilter(f) {
  metricsFilter = f;
  renderMetricsFilterBar();
  if (lastData) renderMetricsCards(lastData);
}

function renderMetricsFilterBar() {
  const labels = {session:'Session', week:'This week', month:'This month', all:'All time'};
  document.getElementById('metrics-filter-bar').innerHTML = Object.entries(labels).map(([k, v]) =>
    `<button class="filter-btn ${metricsFilter===k?'active':''}" onclick="setMetricsFilter('${k}')">${v}</button>`
  ).join('');
}

function getMetricsForWindow(data) {
  const byWindow = data.rich_metrics_by_window || {};
  return byWindow[metricsFilter] || data.rich_metrics || {};
}

function metricsWindowLabel() {
  return {session:'current session', week:'last 7 days', month:'last 28 days', all:'all time'}[metricsFilter] || '';
}

function easeOutCubic(t) { return 1 - Math.pow(1 - t, 3); }

const _animTokens = new WeakMap();
function _getTokens(el) { let m = _animTokens.get(el); if (!m) { m = {}; _animTokens.set(el, m); } return m; }

function animateAttr(el, attr, target, duration) {
  duration = duration || 300;
  const from = parseFloat(el.getAttribute(attr)) || 0;
  if (Math.abs(from - target) < 0.5) { el.setAttribute(attr, target); return; }
  const tokens = _getTokens(el);
  const token = {};
  tokens[attr] = token;
  const start = performance.now();
  function tick(now) {
    if (tokens[attr] !== token) return;
    const t = Math.min((now - start) / duration, 1);
    const v = from + (target - from) * easeOutCubic(t);
    el.setAttribute(attr, v);
    if (t < 1) requestAnimationFrame(tick);
  }
  requestAnimationFrame(tick);
}

function animateNumber(el, target, opts) {
  opts = opts || {};
  const duration = opts.duration || 400;
  const suffix = opts.suffix || '';
  const decimals = opts.decimals || 0;
  const from = parseFloat(el.getAttribute('data-value')) || 0;
  if (from === target) return;
  el.setAttribute('data-value', target);
  if (typeof target !== 'number' || isNaN(target)) { el.textContent = target + suffix; return; }
  const tokens = _getTokens(el);
  const token = {};
  tokens['_num'] = token;
  const start = performance.now();
  function tick(now) {
    if (tokens['_num'] !== token) return;
    const t = Math.min((now - start) / duration, 1);
    const v = from + (target - from) * easeOutCubic(t);
    el.textContent = decimals > 0 ? v.toFixed(decimals) + suffix : Math.round(v) + suffix;
    if (t < 1) requestAnimationFrame(tick);
  }
  requestAnimationFrame(tick);
}

let _metricsInitialized = false;

function renderMetricsCards(data) {
  const rm = getMetricsForWindow(data);
  const wl = metricsWindowLabel();
  if (_metricsInitialized) {
    updateModelCache(rm, wl);
    updateActivityHour(rm, wl);
    updateTopTools(rm, wl);
    return;
  }
  document.getElementById('card-model-cache').innerHTML = `<h2>Model &amp; Cache Efficiency${tip("Token and behavioral statistics across all Claude Code conversations (" + wl + ").")}</h2>` + renderModelCache(rm, wl);
  document.getElementById('card-activity-hour').innerHTML = `<h2>Activity by Hour${tip("Number of assistant turns (responses) per hour of your local day (" + wl + "). Shows when you tend to work with Claude.")}</h2>` + renderActivityHour(rm, wl);
  document.getElementById('card-top-tools').innerHTML = `<h2>Top Tools${tip("Which Claude Code tools were invoked most often (" + wl + "). Counts individual tool calls, not conversations.")}</h2>` + renderTopTools(rm, wl);
  _metricsInitialized = true;
}

function renderBarChart(data, opts) {
  const dateKey = opts.dateKey || 'start';
  const showTime = opts.showTime || false;
  const PAD_L = 40, PAD_T = 5, PAD_B = 20;
  const max = Math.max(...data.map(d => d.output_all||0), 1);
  const W = 480, H = 80, n = data.length;
  const chartW = W - PAD_L;
  const bw = Math.max(Math.floor((chartW - 10) / n) - 2, 4);
  const maxK = Math.round(max / 1000);
  let yAxis = '';
  yAxis += `<line x1="${PAD_L}" y1="${PAD_T}" x2="${PAD_L}" y2="${H + PAD_T}" stroke="#e5e7eb" stroke-width="1"/>`;
  [[0, H], [0.5, H/2], [1, 0]].forEach(([frac, yOff]) => {
    const y = PAD_T + yOff;
    const kVal = Math.round(frac * maxK);
    yAxis += `<line x1="${PAD_L}" y1="${y}" x2="${W}" y2="${y}" stroke="#f3f4f6" stroke-width="1"/>`;
    yAxis += `<text x="${PAD_L - 4}" y="${y + 3}" text-anchor="end">${kVal}k</text>`;
  });
  const tipW = showTime ? 90 : 80;
  const tipH = 28;
  let bars = '';
  data.forEach((d, i) => {
    const val = d.output_all || 0;
    const bh = Math.max(Math.round((val / max) * H), 2);
    const x = PAD_L + 5 + i * (bw + 2);
    const y = PAD_T + H - bh;
    const pct = val / max * 100;
    const fill = pct < 60 ? '#10b981' : pct < 80 ? '#eab308' : '#ef4444';
    const raw = (d[dateKey]||'');
    const tipLabel = showTime ? raw.slice(5, 16).replace('T', ' ') : raw.slice(5, 10);
    const dateLabel = raw.slice(5, 10);
    const kTokens = Math.round(val / 1000);
    const tipX = x + bw/2;
    const tipXAdj = tipX + tipW/2 > W ? tipX - tipW - 4 : tipX - tipW/2;
    const tipY = Math.max(y - tipH - 4, PAD_T);
    bars += `<g class="bar-g">`;
    bars += `<rect x="${x}" y="${PAD_T}" width="${bw}" height="${H}" fill="transparent"/>`;
    bars += `<rect x="${x}" y="${y}" width="${bw}" height="${bh}" fill="${fill}" rx="2" style="transform-origin:${x + bw/2}px ${PAD_T + H}px; animation: barGrowUp 0.3s ease-out ${(i * 0.02).toFixed(2)}s both"/>`;
    if (n <= 8 || i % Math.ceil(n/6) === 0) bars += `<text x="${x + bw/2}" y="${PAD_T + H + 14}" text-anchor="middle">${dateLabel}</text>`;
    bars += `<g class="tip">`;
    bars += `<rect x="${tipXAdj}" y="${tipY}" width="${tipW}" height="${tipH}" rx="3" fill="#1f2937" opacity="0.9"/>`;
    bars += `<text x="${tipXAdj + tipW/2}" y="${tipY + 11}" text-anchor="middle" fill="#f9fafb" font-size="9">${tipLabel}</text>`;
    bars += `<text x="${tipXAdj + tipW/2}" y="${tipY + 22}" text-anchor="middle" fill="#f9fafb" font-size="9">${kTokens}k tokens</text>`;
    bars += `</g></g>`;
  });
  return `<svg viewBox="0 0 ${W} ${H + PAD_T + PAD_B}" style="width:100%;height:auto;overflow:visible">${yAxis}${bars}</svg>`;
}

function renderHistory_card(history) {
  const filtered = filterHistory(history || []);
  const btns = ['week','4w','all'].map(f => {
    const label = {week:'This week', '4w':'4 weeks', all:'All time'}[f];
    return `<button class="filter-btn ${historyFilter===f?'active':''}" onclick="setFilter('${f}')">${label}</button>`;
  }).join('');
  const btnRow = `<div class="filter-btns">${btns}</div>`;
  if (!filtered.length) {
    return btnRow + '<p style="color:#9ca3af;font-size:12px">No completed sessions yet. Sub-session history appears after your first rate-limit boundary.</p>';
  }
  return btnRow + renderBarChart(filtered, {dateKey:'start', showTime:true});
}

function renderWeeklyHistory_card(periodHistory) {
  const history = (periodHistory || []).slice(-12);
  if (!history.length) {
    return '<p style="color:#9ca3af;font-size:12px">No billing period data yet.</p>';
  }
  return renderBarChart(history, {dateKey:'period_start', showTime:false});
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
  const samples = data.intraday_samples || [];
  document.getElementById('card-period').innerHTML = `<h2>Weekly Budget${tip("Your cumulative output token usage this billing week. Resets every 7 days (Fri 20:00 UTC). Tracked against Anthropic Claude Pro or Max plan limits.")}</h2>` + renderPeriod(pred, samples);
  document.getElementById('card-session').innerHTML = `<h2>Session Budget${tip("Output token usage since your last rate-limit boundary (~5h windows). Resets independently of the weekly budget.")}</h2>` + renderSession(pred);
  renderMetricsFilterBar();
  renderMetricsCards(data);
  const weeklyHistoryEl = document.getElementById('card-weekly-history');
  const hasWeeklyHistory = (data.period_history || []).length > 0;
  weeklyHistoryEl.style.display = hasWeeklyHistory ? '' : 'none';
  if (hasWeeklyHistory) weeklyHistoryEl.innerHTML = `<h2>Weekly Budget History${tip("Output token totals for each past billing week. Color: green < 60%, yellow 60–80%, red ≥ 80% of the highest observed week.")}</h2>` + renderWeeklyHistory_card(data.period_history);
  autoSelectHistoryFilter(data.session_history);
  document.getElementById('card-history').innerHTML = `<h2>Session History${tip("Output tokens per sub-session (each ~5h block between rate-limit resets). Useful for spotting heavy coding sessions.")}</h2>` + renderHistory_card(data.session_history);
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
setInterval(refresh, 30000);
</script>
</body>
</html>"""
