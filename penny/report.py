"""HTML status report generation for Penny."""

from __future__ import annotations

import html as _html
import subprocess
from datetime import date, datetime
from pathlib import Path
from typing import Any

from .analysis import format_reset_label, get_usage_bar
from .paths import data_dir


def _pct_color(pct: float) -> str:
    """Traffic-light CSS color for a percentage value."""
    if pct < 60:
        return "#10b981"   # green
    if pct < 80:
        return "#eab308"   # yellow
    return "#ef4444"       # red

REPORT_DIR = data_dir() / "reports"


def _history_svg(
    history: list[dict],
    width: int = 500,
    height: int = 120,
    label_key: str = "period_start",
) -> str:
    """SVG bar chart of period history output token usage with Y-axis and tooltips."""
    if not history:
        return "<p style='color:#6b7280'>No historical data yet.</p>"

    pad_l, pad_t, pad_b = 40, 5, 20
    chart_h = height - pad_t - pad_b
    chart_w = width - pad_l
    max_val = max(p.get("output_all", 0) for p in history) or 1
    max_k = round(max_val / 1000)
    bar_width = max(chart_w // max(len(history), 1) - 4, 4)

    elements: list[str] = []

    # Y-axis line
    elements.append(
        f'<line x1="{pad_l}" y1="{pad_t}" x2="{pad_l}" y2="{pad_t + chart_h}" '
        f'stroke="#d1d5db" stroke-width="1"/>'
    )

    # Grid lines and Y-axis labels at 0%, 50%, 100%
    for frac, k_val in [(0, 0), (0.5, max_k // 2), (1.0, max_k)]:
        y = pad_t + chart_h - int(frac * chart_h)
        elements.append(
            f'<line x1="{pad_l}" y1="{y}" x2="{width}" y2="{y}" '
            f'stroke="#f3f4f6" stroke-width="1"/>'
        )
        elements.append(
            f'<text x="{pad_l - 3}" y="{y + 3}" text-anchor="end" '
            f'font-size="8" fill="#9ca3af">{k_val}k</text>'
        )

    for i, period in enumerate(history):
        total = period.get("output_all", 0)
        pct = min(total / max_val, 1.0)
        bar_h = max(int(pct * chart_h), 2)
        x = pad_l + 2 + i * (bar_width + 4)
        y = pad_t + chart_h - bar_h
        color = "#ef4444" if pct >= 0.8 else "#eab308" if pct >= 0.6 else "#10b981"
        label = period.get(label_key, "")[:10][5:]  # MM-DD
        k_tokens = round(total / 1000)
        elements.append(
            f'<rect x="{x}" y="{y}" width="{bar_width}" height="{bar_h}" '
            f'fill="{color}" rx="2">'
            f'<title>{label}: {k_tokens}k tokens</title>'
            f'</rect>'
            f'<text x="{x + bar_width // 2}" y="{pad_t + chart_h + pad_b - 4}" '
            f'text-anchor="middle" font-size="8" fill="#9ca3af">{label}</text>'
        )

    return (
        f'<svg width="{width}" height="{height}" xmlns="http://www.w3.org/2000/svg" '
        f'overflow="visible">'
        + "".join(elements)
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
    sess_reset_label = format_reset_label(pred.get("session_reset_label", "—"))
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
    session_history = state.get("session_history", [])[-30:]
    session_svg = _history_svg(session_history, label_key="start") if session_history else ""

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
  .gauge {{ font-family: monospace; font-size: 1.1rem; letter-spacing: 1px; color: #111827; }}
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
  <h2>📊 Weekly Budget</h2>
  <div class="gauge-row">
    <div class="gauge-label">All models</div>
    <div class="gauge">{bar_all} <span style="color:{_pct_color(pct_all)};font-weight:600">{pct_all:.1f}%</span></div>
  </div>
  <div class="gauge-row">
    <div class="gauge-label">Sonnet only</div>
    <div class="gauge">{bar_sonnet} <span style="color:{_pct_color(pct_sonnet)};font-weight:600">{pct_sonnet:.1f}%</span></div>
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
  <p style="margin-top:12px">⏰ Resets: {format_reset_label(pred.get('reset_label', '—'))}</p>
  <p class="note">Budget estimates are based on historical usage peaks.
  Compare with <code>/status</code> in Claude Code for server-side percentages.</p>
</div>

<div class="card">
  <h2>⏱ Session Budget</h2>
  <div class="gauge-row">
    <div class="gauge-label">All models</div>
    <div class="gauge">{bar_sess} <span style="color:{_pct_color(sess_pct_all)};font-weight:600">{sess_pct_all:.1f}%</span></div>
  </div>
  <div class="gauge-row">
    <div class="gauge-label">Sonnet only</div>
    <div class="gauge">{get_usage_bar(sess_pct_sonnet)} <span style="color:{_pct_color(sess_pct_sonnet)};font-weight:600">{sess_pct_sonnet:.1f}%</span></div>
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

{'<div class="card"><h2>📊 Session History (output tokens)</h2>' + session_svg + '</div>' if session_svg else ''}

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
            print(f"[penny] report plugin_sections error: {exc}", flush=True)

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
