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
                elif self.path == "/api/config":
                    self._json(_config_payload(app))
                elif self.path.startswith("/api/plugin/") and "/install-log" in self.path:
                    # GET /api/plugin/<name>/install-log?offset=N
                    path_part = self.path.split("?", 1)[0]
                    parts = path_part.split("/")
                    plugin_name = parts[3] if len(parts) >= 5 else ""
                    # Parse ?offset=N from query string
                    offset = 0
                    if "?" in self.path:
                        qs = self.path.split("?", 1)[1]
                        for param in qs.split("&"):
                            if param.startswith("offset="):
                                try:
                                    offset = int(param.split("=", 1)[1])
                                except ValueError:
                                    pass
                    self._json(_install_log_payload(app, plugin_name, offset))
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

                elif self.path == "/api/resume-session":
                    session_id = payload.get("session_id", "")
                    cwd = payload.get("cwd", "")
                    if not session_id:
                        self._error(400, "session_id required")
                        return
                    _resume_session(session_id, cwd)
                    self._ok({"ok": True})

                elif self.path == "/api/config":
                    error = _validate_config_patch(payload)
                    if error:
                        self._error(400, error)
                        return
                    _apply_config_patch(app, payload)
                    self._json({"ok": True, "config": getattr(app, "config", {})})

                elif self.path.startswith("/api/plugin/") and self.path.endswith("/install"):
                    # POST /api/plugin/<name>/install
                    parts = self.path.split("/")
                    plugin_name = parts[3] if len(parts) >= 5 else ""
                    started = app.run_plugin_install(plugin_name)
                    if started:
                        self._ok({"ok": True, "installing": True})
                    else:
                        self._error(400, f"Cannot install plugin '{plugin_name}'")

                else:
                    # Route /api/plugin/<name>/... to plugin handler
                    result = _try_plugin_route(app, "POST", self.path, payload)
                    if result is not None:
                        self._json(result)
                    else:
                        self.send_error(404)

            def _ok(self, data: dict) -> None:
                self._json(data)

            def _error(self, code: int, message: str) -> None:
                body = json.dumps({"error": message}).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

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


def _resume_session(session_id: str, cwd: str) -> None:
    """Open a Terminal.app window resuming a Claude session."""
    import shlex

    from .spawner import _open_in_terminal

    parts = []
    if cwd:
        parts.append(f"cd {shlex.quote(cwd)}")
    parts.append(f"claude --resume {shlex.quote(session_id)}")
    _open_in_terminal(" && ".join(parts))


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


def _config_payload(app) -> dict[str, Any]:
    """Build config + plugin metadata for GET /api/config."""
    config = getattr(app, "config", {}) or {}
    plugins_cfg = config.get("plugins", {})
    plugin_mgr = getattr(app, "_plugin_mgr", None)
    plugins_info: list[dict[str, Any]] = []
    if plugin_mgr is not None:
        for name, plugin in plugin_mgr.all_plugins.items():
            if plugin.hidden:
                continue
            pcfg = plugins_cfg.get(name, {})
            if isinstance(pcfg, (bool, str)):
                enabled = pcfg
            else:
                enabled = pcfg.get("enabled", "auto")
            install_cmd = None
            try:
                install_cmd = plugin.install_command()
            except Exception:
                pass
            plugins_info.append({
                "name": name,
                "description": plugin.description,
                "available": plugin.is_available(),
                "enabled": enabled,
                "install_command": install_cmd,
                "config_schema": plugin.config_schema(),
            })
    return {"config": config, "plugins": plugins_info}


def _install_log_payload(app, plugin_name: str, offset: int = 0) -> dict[str, Any]:
    """Build install log response for GET /api/plugin/<name>/install-log."""
    logs = getattr(app, "_install_logs", {})
    entry = logs.get(plugin_name)
    if entry is None:
        return {"status": "idle", "lines": [], "offset": 0}
    lines = entry.get("lines", [])
    return {
        "status": entry.get("status", "idle"),
        "lines": lines[offset:],
        "offset": len(lines),
    }


_VALID_CONFIG_KEYS = {
    "stats_cache_path", "service", "notifications", "plugins",
    "projects", "trigger", "work", "menubar",
}


def _validate_config_patch(patch: dict[str, Any]) -> str | None:
    """Return an error message if the patch is invalid, or None if valid."""
    unknown = set(patch.keys()) - _VALID_CONFIG_KEYS
    if unknown:
        return f"Unknown config keys: {', '.join(sorted(unknown))}"

    svc = patch.get("service")
    if svc is not None:
        if not isinstance(svc, dict):
            return "service must be a dict"
        for key in ("keep_alive", "launch_at_login"):
            if key in svc and not isinstance(svc[key], bool):
                return f"service.{key} must be a boolean"

    notif = patch.get("notifications")
    if notif is not None:
        if not isinstance(notif, dict):
            return "notifications must be a dict"
        for key in ("weekly_summary", "spawn", "completion"):
            if key in notif and not isinstance(notif[key], bool):
                return f"notifications.{key} must be a boolean"

    trigger = patch.get("trigger")
    if trigger is not None:
        if not isinstance(trigger, dict):
            return "trigger must be a dict"
        if "min_capacity_percent" in trigger:
            v = trigger["min_capacity_percent"]
            if not isinstance(v, (int, float)) or v < 0 or v > 100:
                return "trigger.min_capacity_percent must be a number 0-100"
        if "max_days_remaining" in trigger:
            v = trigger["max_days_remaining"]
            if not isinstance(v, (int, float)) or v <= 0:
                return "trigger.max_days_remaining must be a positive number"

    work = patch.get("work")
    if work is not None:
        if not isinstance(work, dict):
            return "work must be a dict"
        if "max_agents_per_run" in work:
            v = work["max_agents_per_run"]
            if not isinstance(v, int) or v < 1:
                return "work.max_agents_per_run must be an integer >= 1"
        if "agent_permissions" in work:
            if work["agent_permissions"] not in ("off", "scoped", "full"):
                return "work.agent_permissions must be 'off', 'scoped', or 'full'"
        if "allowed_tools" in work and not isinstance(work["allowed_tools"], list):
            return "work.allowed_tools must be a list"

    projects = patch.get("projects")
    if projects is not None:
        if not isinstance(projects, list):
            return "projects must be a list"
        for i, p in enumerate(projects):
            if not isinstance(p, dict) or "path" not in p:
                return f"projects[{i}] must have a 'path' key"

    return None



def _apply_config_patch(app, payload: dict[str, Any]) -> None:
    """Apply a config patch and trigger an immediate menubar refresh.

    Called from the HTTP handler thread.  Updates config in memory, persists
    to disk, then calls _force_menubar_refresh directly.  Although AppKit UI
    calls ideally run on the main thread, NSStatusBarButton.setImage_ is
    thread-safe in practice and this avoids the ObjC selector dispatch issues
    that prevented performSelectorOnMainThread from reaching our dynamically
    defined Python methods.
    """
    from .app import _config_mtime, _deep_merge

    app.config = _deep_merge(app.config, payload)
    try:
        app._write_config()
    except Exception:
        pass
    app._config_mtime = _config_mtime()

    # Trigger a config reload on the main thread via the existing config
    # watcher selector.  _checkConfig: detects the mtime change from
    # _write_config above and calls _hot_reload_config → _force_menubar_refresh
    # on the main thread.  This is immediate — no waiting for the 5s timer.
    app.performSelectorOnMainThread_withObject_waitUntilDone_(
        "_checkConfig:", None, False
    )


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
    /* Navigation tabs */
    .nav-tabs { display:flex; gap:0; margin-bottom:16px; border-bottom:1px solid #e5e7eb; }
    .nav-tab { background:none; border:none; border-bottom:2px solid transparent; color:#6b7280; cursor:pointer; font-size:14px; font-weight:600; padding:8px 16px; transition:all .2s; }
    .nav-tab:hover { color:#111827; }
    .nav-tab.active { color:#3b82f6; border-bottom-color:#3b82f6; }
    /* Settings page */
    .setting-row { display:flex; align-items:center; justify-content:space-between; padding:8px 0; border-bottom:1px solid #f3f4f6; }
    .setting-row:last-child { border-bottom:none; }
    .setting-label { font-size:13px; font-weight:500; }
    .setting-hint { font-size:11px; color:#9ca3af; margin-top:2px; }
    .setting-label-wrap { display:flex; flex-direction:column; }
    .toggle { position:relative; width:36px; height:20px; flex-shrink:0; }
    .toggle input { opacity:0; width:0; height:0; }
    .toggle .slider { position:absolute; inset:0; background:#d1d5db; border-radius:10px; cursor:pointer; transition:.2s; }
    .toggle .slider:before { content:""; position:absolute; height:16px; width:16px; left:2px; bottom:2px; background:#fff; border-radius:50%; transition:.2s; }
    .toggle input:checked + .slider { background:#3b82f6; }
    .toggle input:checked + .slider:before { transform:translateX(16px); }
    .setting-select { font-size:12px; padding:4px 8px; border:1px solid #e5e7eb; border-radius:4px; background:#fff; }
    .setting-input { font-size:12px; padding:4px 8px; border:1px solid #e5e7eb; border-radius:4px; width:80px; }
    .project-row { display:flex; gap:8px; align-items:center; padding:6px 0; border-bottom:1px solid #f3f4f6; }
    .project-row input[type=text] { flex:1; font-size:12px; padding:4px 8px; border:1px solid #e5e7eb; border-radius:4px; font-family:monospace; }
    .project-row input[type=number] { width:50px; font-size:12px; padding:4px 8px; border:1px solid #e5e7eb; border-radius:4px; }
    .btn-sm { font-size:11px; padding:3px 10px; border:1px solid #e5e7eb; border-radius:4px; background:#fff; cursor:pointer; color:#6b7280; }
    .btn-sm:hover { background:#f3f4f6; }
    .btn-sm.primary { background:#3b82f6; border-color:#3b82f6; color:#fff; }
    .btn-sm.primary:hover { background:#2563eb; }
    .btn-sm.danger { color:#ef4444; }
    .btn-sm.danger:hover { background:#fef2f2; }
    .install-log { background:#1f2937; color:#d1d5db; font-family:monospace; font-size:11px; padding:8px 10px; border-radius:6px; max-height:200px; overflow-y:auto; margin-top:6px; white-space:pre-wrap; word-break:break-all; }
    .plugin-row { display:flex; align-items:center; justify-content:space-between; padding:8px 0; border-bottom:1px solid #f3f4f6; }
    .plugin-row:last-child { border-bottom:none; }
    .plugin-info { display:flex; flex-direction:column; }
    .plugin-name { font-size:13px; font-weight:500; }
    .plugin-desc { font-size:11px; color:#9ca3af; }
    .save-status { font-size:11px; color:#10b981; margin-left:8px; opacity:0; transition:opacity .3s; }
    .save-status.visible { opacity:1; }
    .inline-error { font-size:11px; color:#ef4444; margin-top:4px; }
    /* Project accordion */
    .proj-row { border-bottom:1px solid #f3f4f6; padding:6px 0; }
    .proj-row:last-child { border-bottom:none; }
    .proj-row summary { display:flex; align-items:center; gap:12px; cursor:pointer; list-style:none; font-size:13px; }
    .proj-row summary::-webkit-details-marker { display:none; }
    .proj-row summary::before { content:'▸'; color:#9ca3af; font-size:11px; transition:transform .15s; }
    .proj-row[open] summary::before { transform:rotate(90deg); }
    .proj-name { font-weight:600; min-width:100px; }
    .proj-stat { color:#6b7280; font-size:12px; }
    .tbl-sm { margin:6px 0 2px 16px; font-size:11px; }
    .tbl-sm th { font-size:10px; padding:2px 6px; cursor:pointer; user-select:none; white-space:nowrap; }
    .tbl-sm th:last-child { cursor:default; }
    .tbl-sm th:hover:not(:last-child) { color:#3b82f6; }
    .tbl-sm th .sort-arrow { font-size:8px; margin-left:2px; opacity:0.4; }
    .tbl-sm th.sorted .sort-arrow { opacity:1; color:#3b82f6; }
    .tbl-sm td { padding:2px 6px; }
    .btn-resume { font-size:10px; padding:2px 8px; border:1px solid #e5e7eb; border-radius:3px; background:#fff; cursor:pointer; color:#3b82f6; }
    .btn-resume:hover { background:#eff6ff; }
    /* Health indicators */
    .health-dot { width:8px; height:8px; border-radius:50%; display:inline-block; margin-right:6px; flex-shrink:0; }
    .health-yellow { background:#eab308; }
    .health-red { background:#ef4444; animation:healthPulse 1.5s infinite; }
    @keyframes healthPulse { 0%,100% { opacity:1; } 50% { opacity:0.4; } }
    .health-banner { padding:10px 14px; border-radius:8px; margin-bottom:12px; font-size:12px; display:flex; align-items:center; gap:8px; }
    .health-banner.banner-red { background:#fef2f2; border:1px solid #fecaca; color:#991b1b; }
    .health-banner.banner-yellow { background:#fefce8; border:1px solid #fef08a; color:#854d0e; }
    .health-banner .dismiss { cursor:pointer; margin-left:auto; opacity:0.5; font-size:16px; line-height:1; }
    .health-banner .dismiss:hover { opacity:1; }
    .anomaly-row { background:#fef2f2; border-left:3px solid #ef4444; }
    .anomaly-row-yellow { background:#fefce8; border-left:3px solid #eab308; }
    .proj-rate { color:#9ca3af; font-size:11px; }
  </style>
</head>
<body>
<h1>Penny</h1>
<div class="nav-tabs">
  <button class="nav-tab active" onclick="showPage('dashboard')">Dashboard</button>
  <button class="nav-tab" onclick="showPage('settings')">Settings</button>
</div>

<div id="health-banner"></div>
<div id="page-dashboard">
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
<div class="card" id="card-projects" style="margin-bottom:12px"><h2>Projects</h2><p>…</p></div>
<div id="plugin-cards-container"></div>
</div><!-- /page-dashboard -->

<div id="page-settings" style="display:none">
<div class="grid">
  <div class="card" id="settings-service">
    <h2>Service</h2>
    <div class="setting-row">
      <div class="setting-label-wrap"><span class="setting-label">Keep Alive</span><span class="setting-hint">Restart Penny automatically if it crashes</span></div>
      <label class="toggle"><input type="checkbox" id="cfg-keep-alive" onchange="saveSetting('service',{keep_alive:this.checked},this)"><span class="slider"></span></label>
    </div>
    <div class="setting-row">
      <div class="setting-label-wrap"><span class="setting-label">Launch at Login</span><span class="setting-hint">Start Penny when you log in</span></div>
      <label class="toggle"><input type="checkbox" id="cfg-launch-at-login" onchange="saveSetting('service',{launch_at_login:this.checked},this)"><span class="slider"></span></label>
    </div>
  </div>
</div>
<div class="grid">
  <div class="card" id="settings-plugins">
    <h2>Plugins</h2>
    <div id="plugin-settings-list"><p style="color:#9ca3af;font-size:12px">Loading...</p></div>
  </div>
  <div class="card" id="settings-notifications">
    <h2>Notifications</h2>
    <div class="setting-row">
      <div class="setting-label-wrap"><span class="setting-label">Agent Spawn</span><span class="setting-hint">Notify when agents are spawned</span></div>
      <label class="toggle"><input type="checkbox" id="cfg-notif-spawn" onchange="saveSetting('notifications',{spawn:this.checked},this)"><span class="slider"></span></label>
    </div>
    <div class="setting-row">
      <div class="setting-label-wrap"><span class="setting-label">Completion</span><span class="setting-hint">Notify when tasks complete</span></div>
      <label class="toggle"><input type="checkbox" id="cfg-notif-completion" onchange="saveSetting('notifications',{completion:this.checked},this)"><span class="slider"></span></label>
    </div>
  </div>
</div>
<div class="grid">
  <div class="card" id="settings-trigger">
    <h2>Trigger Conditions</h2>
    <p style="font-size:11px;color:#9ca3af;margin:0 0 8px">When to auto-spawn agents</p>
    <div class="setting-row">
      <div class="setting-label-wrap"><span class="setting-label">Min Capacity %</span><span class="setting-hint">Spawn if this much capacity remains</span></div>
      <input type="number" class="setting-input" id="cfg-trigger-capacity" min="0" max="100" onchange="saveSetting('trigger',{min_capacity_percent:+this.value},this)">
    </div>
    <div class="setting-row">
      <div class="setting-label-wrap"><span class="setting-label">Max Days Remaining</span><span class="setting-hint">...and this many days left in the week</span></div>
      <input type="number" class="setting-input" id="cfg-trigger-days" min="0" step="0.5" onchange="saveSetting('trigger',{max_days_remaining:+this.value},this)">
    </div>
  </div>
  <div class="card" id="settings-work">
    <h2>Work</h2>
    <div class="setting-row">
      <div class="setting-label-wrap"><span class="setting-label">Agent Permissions</span><span class="setting-hint">off = no spawning, scoped = restricted, full = unrestricted</span></div>
      <select class="setting-select" id="cfg-work-perms" onchange="saveSetting('work',{agent_permissions:this.value},this)"><option value="off">Off</option><option value="scoped">Scoped</option><option value="full">Full</option></select>
    </div>
  </div>
</div>
<div class="card" id="settings-projects" style="margin-bottom:12px">
  <h2>Projects <span class="save-status" id="projects-save-status">Saved</span></h2>
  <div id="project-list"></div>
  <div style="margin-top:8px;display:flex;gap:8px">
    <button class="btn-sm" onclick="addProject()">+ Add Project</button>
    <button class="btn-sm primary" onclick="saveProjects()">Save Projects</button>
  </div>
</div>
</div><!-- /page-settings -->

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

const projSortState = {};
function sortSessions(sessions, key, asc) {
  const cmp = (a, b) => {
    const va = a[key] || '', vb = b[key] || '';
    if (typeof va === 'number') return asc ? va - vb : vb - va;
    return asc ? (va < vb ? -1 : va > vb ? 1 : 0) : (va > vb ? -1 : va < vb ? 1 : 0);
  };
  return [...sessions].sort(cmp);
}
function onSortClick(projIdx, key) {
  const st = projSortState[projIdx] || {key: 'last_ts', asc: false};
  if (st.key === key) { st.asc = !st.asc; } else { st.key = key; st.asc = key === 'title'; }
  projSortState[projIdx] = st;
  renderMetricsCards(lastData);
}
function renderProjectsCard(rm) {
  const projects = (rm && rm.project_usage) || [];
  if (!projects.length) return '<p style="color:#9ca3af;font-size:12px">No project data yet.</p>';
  const grandTotal = projects.reduce((s, p) => s + p.total_output_tokens, 0) || 1;
  const cols = [
    {key:'title', label:'Session'},
    {key:'total_output_tokens', label:'Tokens'},
    {key:'_pct', label:'Share'},
    {key:'total_turns', label:'Turns'},
    {key:'duration_m', label:'Duration'},
    {key:'tool_errors', label:'Errors'},
    {key:'tokens_per_turn', label:'Tok/Turn'},
    {key:'last_ts', label:'Last Active'},
  ];
  return projects.map((p, pi) => {
    const pct = (p.total_output_tokens / grandTotal * 100).toFixed(1);
    const kTokens = Math.round(p.total_output_tokens / 1000);
    const sessions = (p.sessions || []);
    const st = projSortState[pi] || {key: 'last_ts', asc: false};
    const sortKey = st.key === '_pct' ? 'total_output_tokens' : st.key;
    const sorted = sortSessions(sessions, sortKey, st.asc);
    const headers = cols.map(c => {
      const active = st.key === c.key;
      const arrow = active ? (st.asc ? '&#9650;' : '&#9660;') : '&#9660;';
      const cls = active ? ' class="sorted"' : '';
      return `<th${cls} onclick="onSortClick(${pi},'${c.key}')">${c.label}<span class="sort-arrow">${arrow}</span></th>`;
    }).join('') + '<th></th>';
    const sessRows = sorted.map(s => {
      const sK = Math.round(s.total_output_tokens / 1000);
      const sPct = (s.total_output_tokens / p.total_output_tokens * 100).toFixed(0);
      const lastActive = s.last_ts ? s.last_ts.slice(5, 16).replace('T', ' ') : '';
      const sid = s.session_id || '';
      const shortId = sid.length > 12 ? sid.slice(0, 8) + '\u2026' : sid;
      const label = s.title || shortId;
      const titleAttr = s.title ? `${s.title} (${sid})` : sid;
      const durM = s.duration_m != null ? (s.duration_m < 60 ? s.duration_m.toFixed(0) + 'm' : (s.duration_m / 60).toFixed(1) + 'h') : '';
      const errs = s.tool_errors || 0;
      const tpt = s.tokens_per_turn != null ? Math.round(s.tokens_per_turn) : '';
      const isAnomaly = s.anomaly === true;
      const anomalyReasons = (s.anomaly_reasons || []).join('; ');
      const rowCls = isAnomaly ? ' class="anomaly-row"' : '';
      const rowTitle = isAnomaly ? ` title="${anomalyReasons}"` : '';
      return `<tr${rowCls}${rowTitle}>
        <td title="${titleAttr}" style="font-size:11px;max-width:180px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${label}</td>
        <td>${sK}k</td><td>${sPct}%</td><td>${s.total_turns}</td>
        <td>${durM}</td>
        <td${errs > 0 ? ' style="color:#ef4444;font-weight:600"' : ''}>${errs || ''}</td>
        <td${isAnomaly ? ' style="color:#ef4444"' : ''}>${tpt}</td>
        <td>${lastActive}</td>
        <td><button class="btn-resume" onclick="resumeSession('${sid}','${(p.cwd||'').replace(/'/g,"\\'")}')">Resume</button></td>
      </tr>`;
    }).join('');
    const sessTable = sessions.length ? `<table class="tbl-sm">
      <tr>${headers}</tr>
      ${sessRows}</table>` : '<p style="color:#9ca3af;font-size:11px;margin:4px 0">No session data</p>';
    // Health dot
    const healthDot = p.health && p.health !== 'green'
      ? `<span class="health-dot health-${p.health}" title="${(p.health_reasons||[]).join('; ')}"></span>`
      : '';
    // Rate stats
    const burnRate = p.burn_rate > 0 ? Math.round(p.burn_rate / 1000) + 'k/h' : '';
    const errRate = p.error_rate > 0 ? p.error_rate.toFixed(0) + '% err' : '';
    return `<details class="proj-row"${p.health === 'red' ? ' open' : ''}>
      <summary>
        ${healthDot}
        <span class="proj-name" title="${p.cwd||''}">${p.name}</span>
        <span class="proj-stat">${kTokens}k tokens</span>
        <span class="proj-stat">${pct}%</span>
        <span class="proj-stat">${p.total_turns} turns</span>
        <span class="proj-stat">${p.session_count} sessions</span>
        ${burnRate ? `<span class="proj-rate">${burnRate}</span>` : ''}
        ${errRate ? `<span class="proj-rate" style="color:#ef4444">${errRate}</span>` : ''}
      </summary>
      ${sessTable}
    </details>`;
  }).join('');
}

async function resumeSession(sessionId, cwd) {
  try {
    await fetch('/api/resume-session', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({session_id: sessionId, cwd: cwd}),
    });
  } catch (e) { /* ignore */ }
}

function renderPluginCards(cards) {
  const container = document.getElementById('plugin-cards-container');
  if (!container) return;
  container.innerHTML = (cards || []).map(c =>
    `<div class="card" style="margin-bottom:12px">${c.html}</div>`
  ).join('');
}

const dismissedAlerts = new Set();
function renderHealthBanner(data) {
  const el = document.getElementById('health-banner');
  const state = data.state || {};
  const rm = getMetricsForWindow(data);
  // Merge alerts from state (quick scan) and rich_metrics (full scan)
  const stateAlerts = state.health_alerts || [];
  const rmAlerts = (rm && rm.health_alerts) || [];
  // Deduplicate by cwd, prefer most severe
  const byProject = {};
  [...stateAlerts, ...rmAlerts].forEach(a => {
    const existing = byProject[a.cwd];
    if (!existing || (a.health === 'red' && existing.health !== 'red')) {
      byProject[a.cwd] = a;
    }
  });
  const alerts = Object.values(byProject)
    .filter(a => !dismissedAlerts.has(a.cwd))
    .sort((a, b) => (a.health === 'red' ? 0 : 1) - (b.health === 'red' ? 0 : 1));
  if (!alerts.length) { el.innerHTML = ''; return; }
  el.innerHTML = alerts.map(a => {
    const icon = a.health === 'red' ? '&#9888;' : '&#9888;';
    const reasons = (a.reasons || []).join(', ');
    return `<div class="health-banner banner-${a.health}">
      <span>${icon}</span>
      <span><strong>${a.project}</strong> &mdash; ${reasons}</span>
      <span class="dismiss" onclick="dismissedAlerts.add('${a.cwd.replace(/'/g,"\\'")}');renderHealthBanner(lastData)">&times;</span>
    </div>`;
  }).join('');
}

function render(data) {
  lastData = data;
  const pred = data.prediction || {};
  const state = data.state || {};
  const samples = data.intraday_samples || [];
  renderHealthBanner(data);
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
  const projectsEl = document.getElementById('card-projects');
  const rm = getMetricsForWindow(data);
  const hasProjects = rm && (rm.project_usage || []).length > 0;
  projectsEl.style.display = hasProjects ? '' : 'none';
  if (hasProjects) projectsEl.innerHTML = `<h2>Projects${tip("Token usage breakdown by project (working directory). Click a project to see its individual sessions.")}</h2>` + renderProjectsCard(rm);
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

// ── Settings page ───────────────────────────────────────────────────────

let settingsData = null;
let installPollers = {};

function showPage(page) {
  document.getElementById('page-dashboard').style.display = page === 'dashboard' ? '' : 'none';
  document.getElementById('page-settings').style.display = page === 'settings' ? '' : 'none';
  document.querySelectorAll('.nav-tab').forEach(btn => {
    btn.classList.toggle('active', btn.textContent.trim().toLowerCase() === page);
  });
  if (page === 'settings') loadSettings();
}

async function loadSettings() {
  try {
    const resp = await fetch('/api/config');
    if (!resp.ok) throw new Error(resp.status);
    settingsData = await resp.json();
    populateSettings(settingsData.config, settingsData.plugins);
  } catch (e) {
    console.error('Failed to load settings:', e);
  }
}

function populateSettings(cfg, plugins) {
  // Service
  const svc = cfg.service || {};
  document.getElementById('cfg-keep-alive').checked = svc.keep_alive !== false;
  document.getElementById('cfg-launch-at-login').checked = svc.launch_at_login !== false;
  // Notifications
  const notif = cfg.notifications || {};
  document.getElementById('cfg-notif-spawn').checked = notif.spawn !== false;
  document.getElementById('cfg-notif-completion').checked = notif.completion !== false;
  // Trigger
  const trig = cfg.trigger || {};
  document.getElementById('cfg-trigger-capacity').value = trig.min_capacity_percent ?? 30;
  document.getElementById('cfg-trigger-days').value = trig.max_days_remaining ?? 2;
  // Work
  const work = cfg.work || {};
  document.getElementById('cfg-work-perms').value = work.agent_permissions || 'off';
  // Plugins
  renderPluginSettings(plugins, cfg.plugins || {});
  // Projects
  renderProjectList(cfg.projects || []);
}

function renderPluginSettings(plugins, pluginsCfg) {
  const container = document.getElementById('plugin-settings-list');
  if (!plugins || !plugins.length) {
    container.innerHTML = '<p style="color:#9ca3af;font-size:12px">No plugins discovered.</p>';
    return;
  }
  container.innerHTML = plugins.map(p => {
    const pcfg = pluginsCfg[p.name] || {};
    const isEnabled = typeof pcfg === 'boolean' ? pcfg : (typeof pcfg.enabled === 'boolean' ? pcfg.enabled : (pcfg.enabled === 'auto' ? p.available : p.available));
    const installLog = (settingsData && settingsData._installLogs && settingsData._installLogs[p.name]) || null;
    let action = '';
    if (p.available) {
      action = `<label class="toggle"><input type="checkbox" ${isEnabled?'checked':''} onchange="togglePlugin('${p.name}',this.checked,this)"><span class="slider"></span></label>`;
    } else if (p.install_command) {
      action = `<button class="btn-sm primary" id="install-btn-${p.name}" onclick="installPlugin('${p.name}')">Install</button>`;
    } else {
      action = '<span style="font-size:11px;color:#9ca3af">Not available</span>';
    }
    return `<div class="plugin-row" id="plugin-row-${p.name}">
      <div class="plugin-info"><span class="plugin-name">${p.name}</span><span class="plugin-desc">${p.description}</span></div>
      <div>${action}</div>
    </div>
    <div id="install-log-${p.name}" style="display:none"></div>`;
  }).join('');
}

async function saveSetting(section, values, el) {
  // Optimistic: UI already updated by onchange. Stash previous value for revert.
  const prev = el ? (el.type === 'checkbox' ? el.checked : el.value) : null;
  const patch = {};
  patch[section] = values;
  try {
    const resp = await fetch('/api/config', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(patch)
    });
    if (!resp.ok) {
      const data = await resp.json().catch(() => ({}));
      throw new Error(data.error || 'Save failed');
    }
    showSavedFlash(el);
  } catch (e) {
    // Revert optimistic update
    if (el) {
      if (el.type === 'checkbox') el.checked = !prev;
      else el.value = prev;
    }
    showInlineError(el, e.message);
  }
}

function showSavedFlash(el) {
  if (!el) return;
  const row = el.closest('.setting-row') || el.parentElement;
  let flash = row.querySelector('.save-flash');
  if (!flash) {
    flash = document.createElement('span');
    flash.className = 'save-flash';
    flash.style.cssText = 'font-size:10px;color:#10b981;margin-left:6px;opacity:0;transition:opacity .2s';
    row.appendChild(flash);
  }
  flash.textContent = 'Saved';
  flash.style.opacity = '1';
  setTimeout(() => { flash.style.opacity = '0'; }, 1500);
}

function showInlineError(el, msg) {
  if (!el) return;
  const row = el.closest('.setting-row') || el.parentElement;
  let err = row.querySelector('.inline-error');
  if (!err) {
    err = document.createElement('div');
    err.className = 'inline-error';
    row.appendChild(err);
  }
  err.textContent = msg;
  setTimeout(() => err.remove(), 5000);
}

async function togglePlugin(name, enabled, el) {
  const pcfg = {};
  pcfg[name] = {enabled: enabled};
  try {
    const resp = await fetch('/api/config', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({plugins: pcfg})
    });
    if (!resp.ok) throw new Error('Failed');
  } catch (e) {
    if (el) el.checked = !el.checked;
  }
}

async function installPlugin(name) {
  const btn = document.getElementById('install-btn-' + name);
  if (btn) { btn.textContent = 'Installing...'; btn.disabled = true; }
  const logEl = document.getElementById('install-log-' + name);
  if (logEl) { logEl.style.display = 'block'; logEl.innerHTML = '<pre class="install-log"></pre>'; }
  try {
    const resp = await fetch('/api/plugin/' + name + '/install', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: '{}'
    });
    if (!resp.ok) throw new Error('Install request failed');
    pollInstallLog(name, 0);
  } catch (e) {
    if (btn) { btn.textContent = 'Retry'; btn.disabled = false; }
    if (logEl) logEl.innerHTML = '<div class="inline-error">' + e.message + '</div>';
  }
}

function pollInstallLog(name, offset) {
  if (installPollers[name]) clearTimeout(installPollers[name]);
  installPollers[name] = setTimeout(async () => {
    try {
      const resp = await fetch('/api/plugin/' + name + '/install-log?offset=' + offset);
      const data = await resp.json();
      const logEl = document.getElementById('install-log-' + name);
      if (logEl) {
        const pre = logEl.querySelector('pre');
        if (pre && data.lines.length) {
          pre.textContent += data.lines.join('\\n') + '\\n';
          pre.scrollTop = pre.scrollHeight;
        }
      }
      if (data.status === 'installing') {
        pollInstallLog(name, data.offset);
      } else if (data.status === 'success') {
        const row = document.getElementById('plugin-row-' + name);
        if (row) {
          const actionDiv = row.querySelector('div:last-child');
          actionDiv.innerHTML = '<span style="color:#10b981;font-size:12px;font-weight:600">Installed ✓</span>';
          setTimeout(() => loadSettings(), 2000);
        }
      } else if (data.status === 'failed') {
        const btn = document.getElementById('install-btn-' + name);
        if (btn) { btn.textContent = 'Retry'; btn.disabled = false; }
        if (logEl) {
          const errBanner = document.createElement('div');
          errBanner.className = 'inline-error';
          errBanner.textContent = 'Installation failed. Check the log above.';
          logEl.insertBefore(errBanner, logEl.firstChild);
        }
      }
    } catch (e) {
      pollInstallLog(name, offset);
    }
  }, 1000);
}

// ── Projects ────────────────────────────────────────────────────────────

let currentProjects = [];

function renderProjectList(projects) {
  currentProjects = projects.map(p => ({...p}));
  const container = document.getElementById('project-list');
  if (!projects.length) {
    container.innerHTML = '<p style="color:#9ca3af;font-size:12px">No projects configured. Add one below.</p>';
    return;
  }
  container.innerHTML = currentProjects.map((p, i) => `<div class="project-row">
    <input type="text" value="${(p.path||'').replace(/"/g,'&quot;')}" onchange="currentProjects[${i}].path=this.value" placeholder="~/path/to/project">
    <input type="number" value="${p.priority||1}" min="1" max="5" onchange="currentProjects[${i}].priority=+this.value" title="Priority">
    <button class="btn-sm danger" onclick="removeProject(${i})">×</button>
  </div>`).join('');
}

function addProject() {
  currentProjects.push({path: '', priority: 1});
  renderProjectList(currentProjects);
}

function removeProject(index) {
  currentProjects.splice(index, 1);
  renderProjectList(currentProjects);
}

async function saveProjects() {
  const valid = currentProjects.filter(p => p.path && p.path.trim());
  const statusEl = document.getElementById('projects-save-status');
  statusEl.textContent = 'Saving...';
  statusEl.classList.add('visible');
  try {
    const resp = await fetch('/api/config', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({projects: valid})
    });
    if (!resp.ok) throw new Error('Save failed');
    statusEl.textContent = 'Saved ✓';
    setTimeout(() => statusEl.classList.remove('visible'), 2000);
  } catch (e) {
    statusEl.textContent = 'Error!';
    statusEl.style.color = '#ef4444';
    setTimeout(() => { statusEl.classList.remove('visible'); statusEl.style.color = '#10b981'; }, 3000);
  }
}
</script>
</body>
</html>"""
