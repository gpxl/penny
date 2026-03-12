"""HTML status report generation for Penny."""

from __future__ import annotations

import html as _html
import subprocess
from datetime import date, datetime
from pathlib import Path
from typing import Any

from .analysis import get_usage_bar
from .log import logger
from .paths import data_dir

REPORT_DIR = data_dir() / "reports"


def _history_svg(history: list[dict], width: int = 500, height: int = 120) -> str:
    """SVG bar chart of period_history output token usage."""
    if not history:
        return "<p style='color:#6b7280'>No historical data yet.</p>"

    max_val = max(p.get("output_all", 0) for p in history) or 1
    bar_width = width // max(len(history), 1) - 4
    bars = []

    for i, period in enumerate(history):
        total = period.get("output_all", 0)
        pct = min(total / max_val, 1.0)
        bar_h = int(pct * (height - 20))
        x = i * (bar_width + 4) + 2
        y = height - 20 - bar_h
        color = "#ef4444" if pct > 0.9 else "#3b82f6" if pct > 0.6 else "#10b981"
        label = period.get("period_start", "")[:10][5:]  # MM-DD
        bars.append(
            f'<rect x="{x}" y="{y}" width="{bar_width}" height="{bar_h}" fill="{color}" rx="2"/>'
            f'<text x="{x + bar_width//2}" y="{height - 4}" text-anchor="middle" '
            f'font-size="9" fill="#6b7280">{label}</text>'
        )

    return (
        f'<svg width="{width}" height="{height}" xmlns="http://www.w3.org/2000/svg">'
        + "".join(bars)
        + "</svg>"
    )


def generate_report(state: dict[str, Any], config: dict[str, Any], plugin_mgr: Any = None) -> Path:
    """Generate a self-contained HTML status report. Returns path to the file."""
    REPORT_DIR.mkdir(exist_ok=True)

    pred = state.get("predictions", {})
    agents_running = state.get("agents_running", [])
    spawned = state.get("recently_completed", [])
    history = state.get("period_history", [])

    pct_all = pred.get("pct_all", 0.0)
    pct_sonnet = pred.get("pct_sonnet", 0.0)
    bar_all = get_usage_bar(pct_all)
    bar_sonnet = get_usage_bar(pct_sonnet)

    sess_pct_all = pred.get("session_pct_all", 0.0)
    sess_pct_sonnet = pred.get("session_pct_sonnet", 0.0)
    bar_sess = get_usage_bar(sess_pct_all)
    sess_reset_label = pred.get("session_reset_label", "—")
    sess_hours = pred.get("session_hours_remaining", 0.0)
    sess_remaining = pred.get("sessions_remaining_week", 0)

    # Task queue — fetch all ready tasks and full descriptions
    projects = config.get("projects", [])
    ready_tasks = plugin_mgr.get_all_tasks(projects) if plugin_mgr else []
    task_rows = ""
    recently_ids = {s["task_id"] for s in state.get("recently_completed", [])}
    running_ids = {a["task_id"] for a in state.get("agents_running", [])}

    for task in ready_tasks:
        desc_raw = plugin_mgr.get_task_description(task) if plugin_mgr else task.title
        desc_escaped = _html.escape(desc_raw)
        status_badge = ""
        if task.task_id in running_ids:
            status_badge = ' <span style="color:#f59e0b;font-size:0.7rem">● running</span>'
        elif task.task_id in recently_ids:
            status_badge = ' <span style="color:#10b981;font-size:0.7rem">✓ done</span>'
        priority_color = {"P1": "#ef4444", "P2": "#f59e0b", "P3": "#6b7280"}.get(task.priority, "#6b7280")
        task_rows += f"""
        <tr>
          <td style="white-space:nowrap"><code>{_html.escape(task.task_id)}</code>{status_badge}</td>
          <td style="white-space:nowrap">{_html.escape(task.project_name)}</td>
          <td style="white-space:nowrap;color:{priority_color};font-weight:600">{task.priority}</td>
          <td>
            <strong>{_html.escape(task.title)}</strong>
            <details style="margin-top:4px">
              <summary style="cursor:pointer;color:#6b7280;font-size:0.75rem">Full description</summary>
              <pre style="margin-top:6px;font-size:0.75rem;white-space:pre-wrap;background:#f3f4f6;padding:8px;border-radius:6px;max-height:300px;overflow-y:auto">{desc_escaped}</pre>
            </details>
          </td>
        </tr>"""

    svg = _history_svg(history)

    running_rows = ""
    for a in agents_running:
        running_rows += (
            f"<tr><td>{a['task_id']}</td><td>{a['project']}</td>"
            f"<td>{a['title']}</td><td>🔄 Running</td>"
            f"<td><a href='file://{a['log']}'>log</a></td></tr>"
        )

    completed_rows = ""
    for s in reversed(spawned):
        icon = "✅" if s.get("status") == "completed" else "❌"
        completed_rows += (
            f"<tr><td>{s['task_id']}</td><td>{s['project']}</td>"
            f"<td>{s['title']}</td><td>{icon} {s.get('status','')}</td>"
            f"<td>{s.get('spawned_at','')[:10]}</td>"
            f"<td><a href='file://{s['log']}'>log</a></td></tr>"
        )

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Penny — Status Report</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
          background: #f9fafb; color: #111827; padding: 24px; }}
  h1 {{ font-size: 1.5rem; font-weight: 700; margin-bottom: 4px; }}
  .subtitle {{ color: #6b7280; font-size: 0.875rem; margin-bottom: 24px; }}
  .card {{ background: #fff; border-radius: 12px; padding: 20px;
           box-shadow: 0 1px 3px rgba(0,0,0,.1); margin-bottom: 16px; }}
  h2 {{ font-size: 1rem; font-weight: 600; margin-bottom: 12px; }}
  .gauge {{ font-family: monospace; font-size: 1.1rem; letter-spacing: 1px; color: #3b82f6; }}
  .gauge-row {{ margin-bottom: 8px; }}
  .gauge-label {{ font-size: 0.75rem; color: #6b7280; margin-bottom: 2px; }}
  .stat {{ display: inline-block; margin-right: 24px; margin-top: 12px; }}
  .stat-val {{ font-size: 1.4rem; font-weight: 700; }}
  .stat-lbl {{ font-size: 0.75rem; color: #6b7280; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 0.875rem; }}
  th {{ text-align: left; padding: 6px 8px; background: #f3f4f6;
        font-weight: 600; font-size: 0.75rem; text-transform: uppercase;
        color: #6b7280; }}
  td {{ padding: 8px; border-bottom: 1px solid #f3f4f6; }}
  tr:last-child td {{ border-bottom: none; }}
  a {{ color: #3b82f6; text-decoration: none; }}
  .note {{ font-size: 0.75rem; color: #9ca3af; margin-top: 8px; }}
</style>
</head>
<body>
<h1>● Penny — Status Report</h1>
<p class="subtitle">Generated {datetime.now().strftime("%Y-%m-%d %H:%M:%S")} ·
Last check: {state.get('last_check', 'never')[:19]}</p>

<div class="card">
  <h2>📊 Current Billing Period Usage</h2>
  <div class="gauge-row">
    <div class="gauge-label">All models</div>
    <div class="gauge">{bar_all} {pct_all:.1f}%</div>
  </div>
  <div class="gauge-row">
    <div class="gauge-label">Sonnet only</div>
    <div class="gauge">{bar_sonnet} {pct_sonnet:.1f}%</div>
  </div>
  <div>
    <span class="stat">
      <span class="stat-val">{pred.get('output_all', 0):,}</span><br>
      <span class="stat-lbl">Output tokens (all)</span>
    </span>
    <span class="stat">
      <span class="stat-val">{pred.get('output_sonnet', 0):,}</span><br>
      <span class="stat-lbl">Output tokens (Sonnet)</span>
    </span>
    <span class="stat">
      <span class="stat-val">{pred.get('days_remaining', 0):.1f}d</span><br>
      <span class="stat-lbl">Days remaining</span>
    </span>
    <span class="stat">
      <span class="stat-val">{pred.get('projected_pct_all', 0):.0f}%</span><br>
      <span class="stat-lbl">Projected end-of-week</span>
    </span>
  </div>
  <p style="margin-top:12px">⏰ Resets: {pred.get('reset_label', '—')}</p>
  <p class="note">Budget estimates are based on historical usage peaks.
  Compare with <code>/status</code> in Claude Code for server-side percentages.</p>
</div>

<div class="card">
  <h2>⏱ Current Sub-Session Usage</h2>
  <div class="gauge-row">
    <div class="gauge-label">All models (vs. estimated session budget)</div>
    <div class="gauge">{bar_sess} {sess_pct_all:.1f}%</div>
  </div>
  <div class="gauge-row">
    <div class="gauge-label">Sonnet only</div>
    <div class="gauge">{get_usage_bar(sess_pct_sonnet)} {sess_pct_sonnet:.1f}%</div>
  </div>
  <div>
    <span class="stat">
      <span class="stat-val">{sess_hours:.1f}h</span><br>
      <span class="stat-lbl">Hours until reset</span>
    </span>
    <span class="stat">
      <span class="stat-val">{sess_remaining}</span><br>
      <span class="stat-lbl">Sessions left this week</span>
    </span>
  </div>
  <p style="margin-top:12px">⏰ Session resets: {sess_reset_label}</p>
  <p class="note">Sub-session limits reset every ~5–6 hours at fixed clock boundaries.
  Session budget estimated from historical rate-limit data.</p>
</div>

<div class="card">
  <h2>📈 Period History (output tokens)</h2>
  {svg}
</div>

<div class="card">
  <h2>📋 Task Queue ({len(ready_tasks)} ready)</h2>
  {'<p style="color:#6b7280">No ready tasks found across configured projects.</p>' if not ready_tasks else
  '<table><tr><th>ID</th><th>Project</th><th>Pri</th><th>Task &amp; Description</th></tr>'
  + task_rows + '</table>'}
</div>

<div class="card">
  <h2>⚙ Agents Running ({len(agents_running)})</h2>
  {'<p style="color:#6b7280">No agents currently running.</p>' if not agents_running else
  '<table><tr><th>ID</th><th>Project</th><th>Task</th><th>Status</th><th>Log</th></tr>'
  + running_rows + '</table>'}
</div>

<div class="card">
  <h2>✅ Completed This Period ({len(spawned)})</h2>
  {'<p style="color:#6b7280">No tasks completed yet this period.</p>' if not spawned else
  '<table><tr><th>ID</th><th>Project</th><th>Task</th><th>Status</th><th>Date</th><th>Log</th></tr>'
  + completed_rows + '</table>'}
</div>

</body>
</html>
"""

    # Plugin-contributed sections (appended after core sections)
    plugin_sections = ""
    if plugin_mgr is not None:
        try:
            for section_html in plugin_mgr.get_report_sections(state, config):
                plugin_sections += f'\n<div class="card">\n{section_html}\n</div>\n'
        except Exception as exc:
            logger.error("report plugin_sections error: %s", exc)

    if plugin_sections:
        html = html.replace("</body>", plugin_sections + "\n</body>")

    today = date.today().isoformat()
    report_path = REPORT_DIR / f"report-{today}.html"
    report_path.write_text(html)

    latest = REPORT_DIR / "latest.html"
    if latest.exists() or latest.is_symlink():
        latest.unlink()
    latest.symlink_to(report_path.name)

    return report_path


def open_report(report_path: Path | None = None) -> None:
    """Open the latest report in the default browser."""
    if report_path is None:
        report_path = REPORT_DIR / "latest.html"
    subprocess.run(["open", str(report_path)], check=False)
