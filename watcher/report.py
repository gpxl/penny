"""HTML status report generation for Watcher."""

from __future__ import annotations

import subprocess
from datetime import date, datetime
from pathlib import Path
from typing import Any

from .analysis import get_usage_bar

REPORT_DIR = Path(__file__).parent.parent / "reports"


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


def generate_report(state: dict[str, Any], config: dict[str, Any]) -> Path:
    """Generate a self-contained HTML status report. Returns path to the file."""
    REPORT_DIR.mkdir(exist_ok=True)

    pred = state.get("predictions", {})
    agents_running = state.get("agents_running", [])
    spawned = state.get("spawned_this_week", [])
    history = state.get("period_history", [])

    pct_all = pred.get("pct_all", 0.0)
    pct_sonnet = pred.get("pct_sonnet", 0.0)
    bar_all = get_usage_bar(pct_all)
    bar_sonnet = get_usage_bar(pct_sonnet)

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
<title>Watcher — Status Report</title>
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
<h1>● Watcher — Status Report</h1>
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
  <h2>📈 Period History (output tokens)</h2>
  {svg}
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
