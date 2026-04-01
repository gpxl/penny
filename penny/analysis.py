"""Usage stats parsing and capacity prediction for Penny.

Billing period: Anthropic resets weekly at Friday 20:00 UTC (global epoch).
This matches the "Resets Mar 6 at 9pm (Europe/Amsterdam)" display in /status
(Amsterdam CET = UTC+1, so 9pm CET = 20:00 UTC).

Usage metrics: output tokens from JSONL conversation files, broken out by
all-models and Sonnet-only — exactly what /status tracks.
"""

from __future__ import annotations

import glob
import json
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from datetime import time as _time
from pathlib import Path
from typing import Any

# Global Anthropic epoch for weekly billing periods.
# Verified by working backwards from /status "Resets Mar 6 20:00 UTC".
_BILLING_EPOCH = datetime(2023, 12, 29, 20, 0, 0, tzinfo=timezone.utc)
_WEEK_SECONDS = 7 * 24 * 3600

# Generic rate-limit regex — captures hour, am/pm, and timezone string.
# Works for any timezone the /status output returns.
_RATE_LIMIT_RE = re.compile(
    r"resets\s+(\d+)(am|pm)\s*\(([^)]+)\)",
    re.IGNORECASE,
)

SONNET_MODELS = {
    "claude-sonnet-4-6",
    "claude-sonnet-4-5",
    "claude-sonnet-4-5-20250929",
    "claude-sonnet-4-20250514",
    "claude-3-5-sonnet-20241022",
    "claude-3-5-sonnet-20240620",
    "claude-3-sonnet-20240229",
}

OPUS_MODELS = {
    "claude-opus-4-6",
    "claude-opus-4-5",
    "claude-opus-4-20250514",
    "claude-3-opus-20240229",
}

HAIKU_MODELS = {
    "claude-haiku-4-5-20251001",
    "claude-haiku-4-5",
    "claude-haiku-3-5-20241022",
    "claude-3-haiku-20240307",
}

_24H_CACHE: tuple[float, bool] | None = None
_24H_CACHE_TTL: float = 60.0


# ---------------------------------------------------------------------------
# Period helpers
# ---------------------------------------------------------------------------

def current_billing_period() -> tuple[datetime, datetime]:
    """Return (period_start, period_end) in UTC for the current billing week."""
    now = datetime.now(timezone.utc)
    elapsed = (now - _BILLING_EPOCH).total_seconds()
    n = int(elapsed // _WEEK_SECONDS)
    start = _BILLING_EPOCH + timedelta(seconds=n * _WEEK_SECONDS)
    end = start + timedelta(seconds=_WEEK_SECONDS)
    return start, end


def past_billing_periods(n: int = 12) -> list[tuple[datetime, datetime]]:
    """Return the last n billing periods as (start, end) pairs, sorted oldest first."""
    start, _ = current_billing_period()
    periods = []
    for i in range(n):
        p_start = start - timedelta(seconds=i * _WEEK_SECONDS)
        p_end = p_start + timedelta(seconds=_WEEK_SECONDS)
        periods.append((p_start, p_end))
    periods.sort()
    return periods


def days_until_reset() -> float:
    """Fractional days until the next billing period reset."""
    _, end = current_billing_period()
    remaining = (end - datetime.now(timezone.utc)).total_seconds()
    return max(0.0, remaining / 86400)


def uses_24h_time() -> bool:
    """Return True if the OS is configured for 24-hour time (macOS).

    Uses NSDateFormatter with the ICU 'j' skeleton, which returns the
    locale-preferred hour format ('H' = 24h, 'h' = 12h).  This correctly
    handles both the explicit AppleICUForce24HourTime override and the
    implicit preference that comes from the system region (e.g. nl_NL).
    Result is cached for 60 seconds since it never changes during app lifetime.
    """
    global _24H_CACHE
    import time as _t
    now = _t.monotonic()
    if _24H_CACHE is not None:
        ts, val = _24H_CACHE
        if now - ts < _24H_CACHE_TTL:
            return val

    try:
        from Foundation import NSDateFormatter, NSLocale  # type: ignore[import]
        fmt = NSDateFormatter.dateFormatFromTemplate_options_locale_(
            "j", 0, NSLocale.currentLocale()
        )
        result = "H" in (fmt or "")
    except Exception:
        # Fallback: check the explicit override key via defaults(1)
        try:
            r = subprocess.run(
                ["defaults", "read", "NSGlobalDomain", "AppleICUForce24HourTime"],
                capture_output=True, text=True, timeout=2,
            )
            result = r.stdout.strip() == "1"
        except Exception:
            result = False

    _24H_CACHE = (_t.monotonic(), result)
    return result


def reset_label() -> str:
    """Human-readable reset date/time in local time, e.g. 'Fri Mar 6 at 21:00'."""
    _, end = current_billing_period()
    local = end.astimezone()
    time_fmt = "%-H:%M" if uses_24h_time() else "%-I:%M %p"
    return local.strftime(f"%a %b %-d at {time_fmt}")


def format_reset_label(label: str) -> str:
    """Convert the time portion of a reset label to 24h if the OS is set that way.

    Handles labels as they come from status_fetcher (always 12h) or from local
    estimation (may be "Today at 5:59 PM" style):
    - "5:59pm"           → "17:59"         (session reset, compact)
    - "Mar 6 at 9pm"     → "Mar 6 at 21:00" (weekly reset with date)
    - "Today at 5:59 PM" → "Today at 17:59" (local estimation style)
    Returns label unchanged when OS is in 12h mode or format is unrecognised.
    """
    if not label or label == "—":
        return label
    if not uses_24h_time():
        return label

    def _to_24h(h: int, mins: str, ampm: str) -> str:
        a = ampm.upper()
        h24 = (0 if h == 12 else h) if a == "AM" else (12 if h == 12 else h + 12)
        return f"{h24}:{mins}" if mins != "00" else str(h24)

    # "at H:MM AM/PM" — long form from local estimation ("Today at 5:59 PM")
    m = re.search(r"at (\d+):(\d+) (AM|PM)", label, re.IGNORECASE)
    if m:
        return label[: m.start()] + "at " + _to_24h(int(m.group(1)), m.group(2), m.group(3))

    # "at Npm" or "at N:MMpm" — from status_fetcher ("Mar 6 at 9pm")
    m = re.search(r"at (\d+)(?::(\d+))?(am|pm)", label, re.IGNORECASE)
    if m:
        mins = m.group(2) or "00"
        return label[: m.start()] + "at " + _to_24h(int(m.group(1)), mins, m.group(3))

    # Pure compact time — "5:59pm", "9pm" (session reset from status_fetcher)
    m = re.match(r"^(\d+)(?::(\d+))?(am|pm)$", label, re.IGNORECASE)
    if m:
        mins = m.group(2) or "00"
        return _to_24h(int(m.group(1)), mins, m.group(3))

    return label


def short_reset_label(label: str) -> str:
    """Return a compact reset label: 'today at <time>' or '<date> at <time>'.

    - "Mar 24 at 10am" → "today at 10am"  (if today is Mar 24)
    - "Mar 28 at 9:59"  → "Mar 28 at 9:59"
    - "Today at 5:59 PM" → "today at 5:59 PM"
    - "5pm" → "today at 5pm"  (bare time from session scraper)
    - "17:59" → "today at 17:59"
    """
    if not label or label == "—":
        return label

    # Already has "Today at" — pass through
    if label.lower().startswith("today at "):
        return "Today at " + label[9:]

    # Dated weekly: "Mar 28 at 9am" — replace today's date with "Today"
    m = re.match(r"^([A-Z][a-z]{2}\s+\d{1,2})\s+at\s+(.*)", label)
    if m:
        date_str = m.group(1)
        time_str = m.group(2)
        today_str = datetime.now().strftime("%b ") + str(datetime.now().day)
        if date_str == today_str:
            return f"Today at {time_str}"
        return label

    # Bare time (session scraper): "5pm", "17:59", "21" — prefix with "Today"
    return f"Today at {label}"


# ---------------------------------------------------------------------------
# Token counting from JSONL files
# ---------------------------------------------------------------------------

@dataclass
class TokenUsage:
    output_all: int = 0      # output tokens, all models
    output_sonnet: int = 0   # output tokens, Sonnet models only
    input_all: int = 0       # non-cache input tokens (for reference)
    cache_create: int = 0    # cache creation tokens
    cache_read: int = 0      # cache reads (not counted toward limits)
    turns: int = 0           # number of assistant turns (conversation depth)


@dataclass
class RichMetrics:
    opus_tokens: int = 0
    sonnet_tokens: int = 0
    haiku_tokens: int = 0
    other_tokens: int = 0
    cache_create_tokens: int = 0
    cache_read_tokens: int = 0
    tool_counts: dict = field(default_factory=dict)       # {tool_name: count}
    hourly_activity: list = field(default_factory=lambda: [0] * 24)  # turns by hour (local)
    subagent_turns: int = 0
    total_turns: int = 0
    pr_count: int = 0
    unique_projects: int = 0
    unique_branches: int = 0
    session_count: int = 0       # unique sessionId values seen
    web_search_count: int = 0    # sum of usage.server_tool_use.web_search_requests
    web_fetch_count: int = 0     # sum of usage.server_tool_use.web_fetch_requests
    thinking_turns: int = 0      # assistant turns containing a "thinking" content block
    agentic_turns: int = 0       # assistant turns with stop_reason == "tool_use"
    tool_error_count: int = 0    # user records where toolUseResult.is_error == True
    files_edited: int = 0        # unique file paths from user records' toolUseResult.filePath
    project_usage: list = field(default_factory=list)    # per-project breakdown (sorted desc)
    session_usage: list = field(default_factory=list)    # flat per-session breakdown (sorted desc)
    health_alerts: list = field(default_factory=list)    # [{project, cwd, health, reasons}]


def count_tokens_since(since: datetime, until: datetime | None = None) -> TokenUsage:
    """
    Sum token usage from ~/.claude/projects/**/*.jsonl since a given UTC datetime.
    Only counts assistant messages that have a usage block.
    If until is provided, only counts messages with timestamps before that datetime.
    """
    since_str = since.strftime("%Y-%m-%dT%H:%M:%S")
    until_str = until.strftime("%Y-%m-%dT%H:%M:%S") if until else None
    projects_dir = Path.home() / ".claude" / "projects"
    usage = TokenUsage()

    if not projects_dir.exists():
        return usage

    since_ts = since.timestamp()
    for filepath in glob.glob(str(projects_dir / "**" / "*.jsonl"), recursive=True):
        try:
            if Path(filepath).stat().st_mtime < since_ts:
                continue  # file not modified since our window — no new entries possible
            with open(filepath, errors="ignore") as fh:
                for raw in fh:
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        obj = json.loads(raw)
                    except json.JSONDecodeError:
                        continue

                    ts = obj.get("timestamp", "")
                    if not ts or ts[:19] < since_str:
                        continue
                    if until_str and ts[:19] >= until_str:
                        continue
                    if obj.get("type") != "assistant":
                        continue

                    msg = obj.get("message", {})
                    u = msg.get("usage")
                    if not u:
                        continue

                    out = u.get("output_tokens", 0)
                    inp = u.get("input_tokens", 0)
                    cc = u.get("cache_creation_input_tokens", 0)
                    cr = u.get("cache_read_input_tokens", 0)
                    model = msg.get("model", "")

                    usage.output_all += out
                    usage.input_all += inp
                    usage.cache_create += cc
                    usage.cache_read += cr
                    usage.turns += 1

                    if model in SONNET_MODELS:
                        usage.output_sonnet += out

        except OSError:
            continue

    return usage


def count_tokens_by_window(
    windows: dict[str, tuple[datetime, datetime]],
) -> dict[str, TokenUsage]:
    """Count tokens for multiple (since, until) time windows in a single JSONL pass.

    *windows* maps label → (since, until) pair.  Returns a dict with the same keys,
    each holding the TokenUsage for that window.  Much faster than calling
    count_tokens_since() once per window.
    """
    if not windows:
        return {}

    projects_dir = Path.home() / ".claude" / "projects"
    if not projects_dir.exists():
        return {k: TokenUsage() for k in windows}

    # Pre-compute string cutoffs
    ranges = {
        k: (s.strftime("%Y-%m-%dT%H:%M:%S"), e.strftime("%Y-%m-%dT%H:%M:%S"))
        for k, (s, e) in windows.items()
    }
    earliest_ts = min(s.timestamp() for s, _ in windows.values())
    usage: dict[str, TokenUsage] = {k: TokenUsage() for k in windows}

    for filepath in glob.glob(str(projects_dir / "**" / "*.jsonl"), recursive=True):
        try:
            if Path(filepath).stat().st_mtime < earliest_ts:
                continue
            with open(filepath, errors="ignore") as fh:
                for raw in fh:
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        obj = json.loads(raw)
                    except json.JSONDecodeError:
                        continue

                    ts = obj.get("timestamp", "")
                    if not ts:
                        continue
                    ts19 = ts[:19]
                    if obj.get("type") != "assistant":
                        continue

                    msg = obj.get("message", {})
                    u = msg.get("usage")
                    if not u:
                        continue

                    out = u.get("output_tokens", 0)
                    inp = u.get("input_tokens", 0)
                    cc = u.get("cache_creation_input_tokens", 0)
                    cr = u.get("cache_read_input_tokens", 0)
                    model = msg.get("model", "")

                    for k, (since_s, until_s) in ranges.items():
                        if ts19 >= since_s and ts19 < until_s:
                            w = usage[k]
                            w.output_all += out
                            w.input_all += inp
                            w.cache_create += cc
                            w.cache_read += cr
                            w.turns += 1
                            if model in SONNET_MODELS:
                                w.output_sonnet += out

        except OSError:
            continue

    return usage


def scan_rich_metrics(since: datetime, until: datetime | None = None) -> RichMetrics:
    """
    Scan JSONL files since a given UTC datetime and return rich behavioral metrics.
    Single JSONL pass using identical mtime guard and timestamp filter as count_tokens_since.
    """
    since_str = since.strftime("%Y-%m-%dT%H:%M:%S")
    until_str = until.strftime("%Y-%m-%dT%H:%M:%S") if until else None
    projects_dir = Path.home() / ".claude" / "projects"
    metrics = RichMetrics()

    if not projects_dir.exists():
        return metrics

    since_ts = since.timestamp()
    cwds: set[str] = set()
    branches: set[str] = set()
    sessions: set[str] = set()
    edited_files: set[str] = set()
    session_titles: dict[str, str] = {}  # sessionId -> customTitle
    # Per-project accumulator: {cwd: {opus, sonnet, haiku, other, turns, sessions: {sid: {...}}}}
    proj_acc: dict[str, dict] = {}

    for filepath in glob.glob(str(projects_dir / "**" / "*.jsonl"), recursive=True):
        try:
            if Path(filepath).stat().st_mtime < since_ts:
                continue
            with open(filepath, errors="ignore") as fh:
                for raw in fh:
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        obj = json.loads(raw)
                    except json.JSONDecodeError:
                        continue

                    ts = obj.get("timestamp", "")
                    if not ts or ts[:19] < since_str:
                        continue
                    if until_str and ts[:19] >= until_str:
                        continue

                    entry_type = obj.get("type")

                    # Count PR link entries
                    if entry_type == "pr-link":
                        metrics.pr_count += 1
                        continue

                    # Capture session title from custom-title entries
                    if entry_type == "custom-title":
                        sid = obj.get("sessionId")
                        title = obj.get("customTitle", "")
                        if sid and title:
                            session_titles[sid] = title
                        continue

                    # Extract tool results from user records
                    if entry_type == "user":
                        result = obj.get("toolUseResult")
                        if isinstance(result, dict):
                            if result.get("is_error"):
                                metrics.tool_error_count += 1
                                # Per-project/session error counting
                                u_cwd = obj.get("cwd")
                                u_sid = obj.get("sessionId")
                                if u_cwd:
                                    pa = proj_acc.setdefault(u_cwd, {
                                        "opus": 0, "sonnet": 0, "haiku": 0,
                                        "other": 0, "turns": 0,
                                        "tool_errors": 0, "sessions": {},
                                    })
                                    pa["tool_errors"] = pa.get("tool_errors", 0) + 1
                                    if u_sid:
                                        sa = pa["sessions"].setdefault(u_sid, {
                                            "opus": 0, "sonnet": 0, "haiku": 0,
                                            "other": 0, "turns": 0,
                                            "tool_errors": 0,
                                            "first_ts": ts[:19], "last_ts": ts[:19],
                                        })
                                        sa["tool_errors"] = sa.get("tool_errors", 0) + 1
                            fp = result.get("filePath")
                            if fp:
                                edited_files.add(fp)
                        continue

                    if entry_type != "assistant":
                        continue

                    metrics.total_turns += 1

                    # Track unique sessions
                    session_id = obj.get("sessionId")
                    if session_id:
                        sessions.add(session_id)

                    # Subagent turns
                    if obj.get("isSidechain"):
                        metrics.subagent_turns += 1

                    # Hourly activity (local time)
                    try:
                        event_utc = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                        hour = event_utc.astimezone().hour
                        metrics.hourly_activity[hour] += 1
                    except (ValueError, IndexError):
                        pass

                    # Track unique projects and branches
                    cwd = obj.get("cwd")
                    if cwd:
                        cwds.add(cwd)
                    branch = obj.get("gitBranch")
                    if branch:
                        branches.add(branch)

                    msg = obj.get("message", {})
                    u = msg.get("usage", {})
                    out = u.get("output_tokens", 0)
                    cc = u.get("cache_creation_input_tokens", 0)
                    cr = u.get("cache_read_input_tokens", 0)
                    model = msg.get("model", "")

                    metrics.cache_create_tokens += cc
                    metrics.cache_read_tokens += cr

                    # Web search / fetch counts from server_tool_use
                    stu = u.get("server_tool_use", {})
                    metrics.web_search_count += stu.get("web_search_requests", 0)
                    metrics.web_fetch_count += stu.get("web_fetch_requests", 0)

                    if model in OPUS_MODELS:
                        metrics.opus_tokens += out
                    elif model in SONNET_MODELS:
                        metrics.sonnet_tokens += out
                    elif model in HAIKU_MODELS:
                        metrics.haiku_tokens += out
                    else:
                        metrics.other_tokens += out

                    # Tool usage counts + thinking detection
                    content = msg.get("content", [])
                    if isinstance(content, list):
                        has_thinking = False
                        for block in content:
                            if isinstance(block, dict):
                                btype = block.get("type")
                                if btype == "tool_use":
                                    tool_name = block.get("name", "unknown")
                                    metrics.tool_counts[tool_name] = metrics.tool_counts.get(tool_name, 0) + 1
                                elif btype == "thinking" and not has_thinking:
                                    has_thinking = True
                        if has_thinking:
                            metrics.thinking_turns += 1

                    # Agentic turns (Claude called a tool)
                    if msg.get("stop_reason") == "tool_use":
                        metrics.agentic_turns += 1

                    # Per-project / per-session accumulation
                    if cwd:
                        model_key = (
                            "opus" if model in OPUS_MODELS
                            else "sonnet" if model in SONNET_MODELS
                            else "haiku" if model in HAIKU_MODELS
                            else "other"
                        )
                        pa = proj_acc.setdefault(cwd, {
                            "opus": 0, "sonnet": 0, "haiku": 0, "other": 0,
                            "turns": 0, "tool_errors": 0, "sessions": {},
                        })
                        pa[model_key] += out
                        pa["turns"] += 1
                        if session_id:
                            sa = pa["sessions"].setdefault(session_id, {
                                "opus": 0, "sonnet": 0, "haiku": 0, "other": 0,
                                "turns": 0, "tool_errors": 0,
                                "first_ts": ts[:19], "last_ts": ts[:19],
                            })
                            sa[model_key] += out
                            sa["turns"] += 1
                            if ts[:19] < sa["first_ts"]:
                                sa["first_ts"] = ts[:19]
                            if ts[:19] > sa["last_ts"]:
                                sa["last_ts"] = ts[:19]

        except OSError:
            continue

    metrics.unique_projects = len(cwds)
    metrics.unique_branches = len(branches)
    metrics.session_count = len(sessions)
    metrics.files_edited = len(edited_files)
    metrics.project_usage, metrics.health_alerts = _assemble_project_usage(
        proj_acc, session_titles=session_titles,
    )
    metrics.session_usage = _assemble_flat_sessions(proj_acc, session_titles=session_titles)
    return metrics


def _compute_session_anomalies(sessions: list[dict]) -> None:
    """Flag anomalous sessions in-place based on duration, error rate, and cost."""
    for s in sessions:
        first = s.get("first_ts", "")
        last = s.get("last_ts", "")
        turns = s.get("total_turns", 0)
        tokens = s.get("total_output_tokens", 0)
        errors = s.get("tool_errors", 0)
        reasons: list[str] = []

        # Compute duration in minutes
        duration_m = 0.0
        if first and last:
            try:
                t0 = datetime.fromisoformat(first)
                t1 = datetime.fromisoformat(last)
                duration_m = max((t1 - t0).total_seconds() / 60, 0)
            except (ValueError, TypeError):
                pass
        s["duration_m"] = round(duration_m, 1)

        # Tokens per turn
        tpt = tokens / turns if turns > 0 else 0.0
        s["tokens_per_turn"] = round(tpt, 1)

        # Anomaly checks
        if duration_m < 1 and tokens > 1000:
            reasons.append(f"Short session ({duration_m:.0f}m) with {tokens} tokens")
        if errors > 0 and turns > 0:
            err_rate = errors / (errors + turns) * 100
            if err_rate > 50:
                reasons.append(f"High error rate ({err_rate:.0f}%)")
        if tpt > 5000:
            reasons.append(f"High cost per turn ({tpt:.0f} tok/turn)")

        s["anomaly"] = len(reasons) > 0
        s["anomaly_reasons"] = reasons


def _compute_project_health(
    projects: list[dict],
    recent_projects: list[dict] | None = None,
) -> list[dict]:
    """Compute health signals for each project and return alerts for unhealthy ones.

    Three detection layers:
    1. Absolute thresholds (works with 1 project)
    2. Relative outlier (2+ projects, median comparison)
    3. Spike detection (recent vs week burn rate)
    """
    alerts: list[dict] = []
    if not projects:
        return alerts

    # Build recent lookup: cwd -> burn_rate
    recent_rates: dict[str, float] = {}
    if recent_projects:
        for rp in recent_projects:
            cwd = rp.get("cwd", "")
            if cwd and rp.get("total_output_tokens", 0) > 0:
                # Recent window is 1 hour, so burn_rate = tokens directly
                recent_rates[cwd] = float(rp["total_output_tokens"])

    # Compute rate metrics for each project
    for p in projects:
        turns = p.get("total_turns", 0)
        tokens = p.get("total_output_tokens", 0)
        errors = p.get("tool_errors", 0)
        sessions = p.get("sessions", [])

        # Compute time span from earliest to latest session activity
        all_first: list[str] = []
        all_last: list[str] = []
        for s in sessions:
            if s.get("first_ts"):
                all_first.append(s["first_ts"])
            if s.get("last_ts"):
                all_last.append(s["last_ts"])

        hours_active = 0.0
        if all_first and all_last:
            try:
                t0 = datetime.fromisoformat(min(all_first))
                t1 = datetime.fromisoformat(max(all_last))
                hours_active = max((t1 - t0).total_seconds() / 3600, 0.01)
            except (ValueError, TypeError):
                hours_active = 0.01

        burn_rate = tokens / hours_active if hours_active > 0 else 0.0
        error_rate = errors / (errors + turns) * 100 if (errors + turns) > 0 else 0.0
        n_sessions = len(sessions)
        session_velocity = n_sessions / hours_active if hours_active > 0 else 0.0

        # Compute session anomalies first — sets duration_m on each session
        _compute_session_anomalies(sessions)

        # Average session duration (uses duration_m set by anomaly detection)
        durations = [s.get("duration_m", 0) for s in sessions if s.get("duration_m", 0) > 0]
        avg_duration = sum(durations) / len(durations) if durations else 0.0

        p["burn_rate"] = round(burn_rate, 1)
        p["error_rate"] = round(error_rate, 1)
        p["session_velocity"] = round(session_velocity, 2)
        p["avg_session_duration_m"] = round(avg_duration, 1)

    # Minimum tokens to avoid noisy alerts from tiny samples
    _MIN_TOKENS_FOR_ALERT = 5_000

    # Layer 1: Absolute thresholds
    for p in projects:
        reasons: list[str] = []
        level = "green"
        tokens = p.get("total_output_tokens", 0)

        er = p["error_rate"]
        if er > 50:
            level = "red"
            reasons.append(f"High error rate ({er:.0f}%)")
        elif er > 20:
            level = max(level, "yellow", key=lambda x: ["green", "yellow", "red"].index(x))
            reasons.append(f"Elevated error rate ({er:.0f}%)")

        br = p["burn_rate"]
        if tokens >= _MIN_TOKENS_FOR_ALERT:
            if br > 50_000:
                level = "red"
                reasons.append(f"Very high burn rate ({br/1000:.0f}k tok/h)")
            elif br > 20_000:
                level = max(level, "yellow", key=lambda x: ["green", "yellow", "red"].index(x))
                reasons.append(f"High burn rate ({br/1000:.0f}k tok/h)")

        sv = p["session_velocity"]
        if tokens >= _MIN_TOKENS_FOR_ALERT:
            if sv > 10:
                level = "red"
                reasons.append(f"Very high session velocity ({sv:.1f}/h)")
            elif sv > 5:
                level = max(level, "yellow", key=lambda x: ["green", "yellow", "red"].index(x))
                reasons.append(f"High session velocity ({sv:.1f}/h)")

        avg_d = p["avg_session_duration_m"]
        n_sess = len(p.get("sessions", []))
        if n_sess > 5 and avg_d > 0:
            if avg_d < 1:
                level = "red"
                reasons.append(f"Very short sessions (avg {avg_d:.1f}m)")
            elif avg_d < 2:
                level = max(level, "yellow", key=lambda x: ["green", "yellow", "red"].index(x))
                reasons.append(f"Short sessions (avg {avg_d:.1f}m)")

        p["health"] = level
        p["health_reasons"] = reasons

    # Layer 2: Relative outlier detection (2+ projects, only with enough data)
    significant = [p for p in projects if p.get("total_output_tokens", 0) >= _MIN_TOKENS_FOR_ALERT]
    if len(significant) >= 2:
        burn_rates = sorted(p["burn_rate"] for p in significant)
        error_rates = sorted(p["error_rate"] for p in significant)
        n = len(burn_rates)
        median_br = (burn_rates[n // 2] + burn_rates[(n - 1) // 2]) / 2
        median_er = (error_rates[n // 2] + error_rates[(n - 1) // 2]) / 2

        for p in significant:
            if median_br > 0 and p["burn_rate"] > 5 * median_br:
                if p["health"] != "red":
                    p["health"] = "red"
                    p["health_reasons"].append(
                        f"Burn rate {p['burn_rate']/median_br:.1f}x other projects"
                    )
            elif median_br > 0 and p["burn_rate"] > 3 * median_br:
                if p["health"] == "green":
                    p["health"] = "yellow"
                    p["health_reasons"].append(
                        f"Burn rate {p['burn_rate']/median_br:.1f}x other projects"
                    )

            if median_er > 0 and p["error_rate"] > 5 * median_er:
                if p["health"] != "red":
                    p["health"] = "red"
                    p["health_reasons"].append(
                        f"Error rate {p['error_rate']/median_er:.1f}x other projects"
                    )
            elif median_er > 0 and p["error_rate"] > 3 * median_er:
                if p["health"] == "green":
                    p["health"] = "yellow"
                    p["health_reasons"].append(
                        f"Error rate {p['error_rate']/median_er:.1f}x other projects"
                    )

    # Layer 3: Spike detection (recent 1h vs longer-term average)
    for p in projects:
        cwd = p.get("cwd", "")
        recent_br = recent_rates.get(cwd, 0)
        avg_br = p["burn_rate"]
        if recent_br > 0 and avg_br > 0:
            spike = recent_br / avg_br
            if spike > 5:
                if p["health"] != "red":
                    p["health"] = "red"
                p["health_reasons"].append(f"Burn rate spike ({spike:.1f}x normal)")
            elif spike > 3:
                if p["health"] == "green":
                    p["health"] = "yellow"
                p["health_reasons"].append(f"Burn rate spike ({spike:.1f}x normal)")

    # Collect alerts for unhealthy projects
    for p in projects:
        if p["health"] != "green":
            alerts.append({
                "project": p.get("name", ""),
                "cwd": p.get("cwd", ""),
                "health": p["health"],
                "reasons": p["health_reasons"],
            })

    return alerts


def _assemble_project_usage(
    proj_acc: dict[str, dict], max_projects: int = 20, max_sessions: int = 20,
    session_titles: dict[str, str] | None = None,
    recent_proj_acc: dict[str, dict] | None = None,
) -> tuple[list[dict], list[dict]]:
    """Convert raw project accumulators into sorted project_usage list.

    Returns (project_usage, health_alerts).
    """
    titles = session_titles or {}
    result = []
    for cwd_val, pa in proj_acc.items():
        total = pa["opus"] + pa["sonnet"] + pa["haiku"] + pa["other"]
        sess_list = []
        for sid, sa in pa["sessions"].items():
            s_total = sa["opus"] + sa["sonnet"] + sa["haiku"] + sa["other"]
            sess_list.append({
                "session_id": sid,
                "title": titles.get(sid, ""),
                "opus_tokens": sa["opus"],
                "sonnet_tokens": sa["sonnet"],
                "haiku_tokens": sa["haiku"],
                "other_tokens": sa["other"],
                "total_output_tokens": s_total,
                "total_turns": sa["turns"],
                "tool_errors": sa.get("tool_errors", 0),
                "first_ts": sa["first_ts"],
                "last_ts": sa["last_ts"],
            })
        sess_list.sort(key=lambda s: s["last_ts"], reverse=True)
        result.append({
            "cwd": cwd_val,
            "name": Path(cwd_val).name,
            "opus_tokens": pa["opus"],
            "sonnet_tokens": pa["sonnet"],
            "haiku_tokens": pa["haiku"],
            "other_tokens": pa["other"],
            "total_output_tokens": total,
            "total_turns": pa["turns"],
            "tool_errors": pa.get("tool_errors", 0),
            "session_count": len(pa["sessions"]),
            "sessions": sess_list[:max_sessions],
        })
    result.sort(key=lambda p: p["total_output_tokens"], reverse=True)
    result = result[:max_projects]

    # Build recent project list for spike detection
    recent_projects: list[dict] | None = None
    if recent_proj_acc is not None:
        recent_projects = []
        for cwd_val, rpa in recent_proj_acc.items():
            r_total = rpa["opus"] + rpa["sonnet"] + rpa["haiku"] + rpa["other"]
            recent_projects.append({
                "cwd": cwd_val,
                "total_output_tokens": r_total,
            })

    alerts = _compute_project_health(result, recent_projects=recent_projects)
    return result, alerts


def _assemble_flat_sessions(
    proj_acc: dict[str, dict], session_titles: dict[str, str] | None = None,
) -> list[dict]:
    """Build a flat list of all sessions across projects, sorted by last active desc."""
    titles = session_titles or {}
    result = []
    for cwd_val, pa in proj_acc.items():
        for sid, sa in pa["sessions"].items():
            s_total = sa["opus"] + sa["sonnet"] + sa["haiku"] + sa["other"]
            result.append({
                "session_id": sid,
                "title": titles.get(sid, ""),
                "cwd": cwd_val,
                "name": Path(cwd_val).name,
                "opus_tokens": sa["opus"],
                "sonnet_tokens": sa["sonnet"],
                "haiku_tokens": sa["haiku"],
                "other_tokens": sa["other"],
                "total_output_tokens": s_total,
                "total_turns": sa["turns"],
                "tool_errors": sa.get("tool_errors", 0),
                "first_ts": sa["first_ts"],
                "last_ts": sa["last_ts"],
            })
    result.sort(key=lambda s: s["last_ts"], reverse=True)
    return result[:30]


def scan_rich_metrics_multi(
    windows: dict[str, datetime],
) -> dict[str, RichMetrics]:
    """Scan JSONL files once and bucket events into multiple time windows.

    *windows* maps label → UTC cutoff datetime.  Every event at or after the
    cutoff is counted toward that window's RichMetrics.  Returns a dict with
    the same keys.
    """
    if not windows:
        return {}

    projects_dir = Path.home() / ".claude" / "projects"
    if not projects_dir.exists():
        return {k: RichMetrics() for k in windows}

    # Pre-compute string cutoffs for fast comparison
    cutoffs = {
        k: v.strftime("%Y-%m-%dT%H:%M:%S") for k, v in windows.items()
    }
    earliest_ts = min(v.timestamp() for v in windows.values())

    # Per-window accumulators
    metrics: dict[str, RichMetrics] = {k: RichMetrics() for k in windows}
    cwds: dict[str, set[str]] = {k: set() for k in windows}
    branches: dict[str, set[str]] = {k: set() for k in windows}
    sessions: dict[str, set[str]] = {k: set() for k in windows}
    edited_files: dict[str, set[str]] = {k: set() for k in windows}
    session_titles: dict[str, str] = {}  # sessionId -> customTitle (shared across windows)
    proj_acc: dict[str, dict[str, dict]] = {k: {} for k in windows}

    for filepath in glob.glob(str(projects_dir / "**" / "*.jsonl"), recursive=True):
        try:
            if Path(filepath).stat().st_mtime < earliest_ts:
                continue
            with open(filepath, errors="ignore") as fh:
                for raw in fh:
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        obj = json.loads(raw)
                    except json.JSONDecodeError:
                        continue

                    ts = obj.get("timestamp", "")
                    if not ts:
                        continue
                    ts19 = ts[:19]

                    # Determine which windows this event falls into
                    matched = [k for k, c in cutoffs.items() if ts19 >= c]
                    if not matched:
                        continue

                    entry_type = obj.get("type")

                    if entry_type == "pr-link":
                        for k in matched:
                            metrics[k].pr_count += 1
                        continue

                    # Capture session title from custom-title entries
                    if entry_type == "custom-title":
                        sid = obj.get("sessionId")
                        title = obj.get("customTitle", "")
                        if sid and title:
                            session_titles[sid] = title
                        continue

                    if entry_type == "user":
                        result = obj.get("toolUseResult")
                        if isinstance(result, dict):
                            is_err = result.get("is_error")
                            if is_err:
                                for k in matched:
                                    metrics[k].tool_error_count += 1
                                # Per-project/session error counting
                                u_cwd = obj.get("cwd")
                                u_sid = obj.get("sessionId")
                                if u_cwd:
                                    for k in matched:
                                        pa = proj_acc[k].setdefault(u_cwd, {
                                            "opus": 0, "sonnet": 0, "haiku": 0,
                                            "other": 0, "turns": 0,
                                            "tool_errors": 0, "sessions": {},
                                        })
                                        pa["tool_errors"] = pa.get("tool_errors", 0) + 1
                                        if u_sid:
                                            sa = pa["sessions"].setdefault(u_sid, {
                                                "opus": 0, "sonnet": 0, "haiku": 0,
                                                "other": 0, "turns": 0,
                                                "tool_errors": 0,
                                                "first_ts": ts19, "last_ts": ts19,
                                            })
                                            sa["tool_errors"] = sa.get("tool_errors", 0) + 1
                            fp = result.get("filePath")
                            if fp:
                                for k in matched:
                                    edited_files[k].add(fp)
                        continue

                    if entry_type != "assistant":
                        continue

                    for k in matched:
                        metrics[k].total_turns += 1

                    session_id = obj.get("sessionId")
                    if session_id:
                        for k in matched:
                            sessions[k].add(session_id)

                    is_sidechain = obj.get("isSidechain")
                    if is_sidechain:
                        for k in matched:
                            metrics[k].subagent_turns += 1

                    try:
                        event_utc = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                        hour = event_utc.astimezone().hour
                        for k in matched:
                            metrics[k].hourly_activity[hour] += 1
                    except (ValueError, IndexError):
                        pass

                    cwd = obj.get("cwd")
                    if cwd:
                        for k in matched:
                            cwds[k].add(cwd)
                    branch_val = obj.get("gitBranch")
                    if branch_val:
                        for k in matched:
                            branches[k].add(branch_val)

                    msg = obj.get("message", {})
                    u = msg.get("usage", {})
                    out = u.get("output_tokens", 0)
                    cc = u.get("cache_creation_input_tokens", 0)
                    cr = u.get("cache_read_input_tokens", 0)
                    model = msg.get("model", "")

                    stu = u.get("server_tool_use", {})
                    ws = stu.get("web_search_requests", 0)
                    wf = stu.get("web_fetch_requests", 0)

                    if model in OPUS_MODELS:
                        tok_attr = "opus_tokens"
                    elif model in SONNET_MODELS:
                        tok_attr = "sonnet_tokens"
                    elif model in HAIKU_MODELS:
                        tok_attr = "haiku_tokens"
                    else:
                        tok_attr = "other_tokens"

                    content = msg.get("content", [])
                    has_thinking = False
                    tool_names: list[str] = []
                    if isinstance(content, list):
                        for block in content:
                            if isinstance(block, dict):
                                btype = block.get("type")
                                if btype == "tool_use":
                                    tool_names.append(block.get("name", "unknown"))
                                elif btype == "thinking" and not has_thinking:
                                    has_thinking = True
                    is_agentic = msg.get("stop_reason") == "tool_use"

                    model_key = tok_attr.split("_")[0]  # "opus", "sonnet", etc.

                    for k in matched:
                        m = metrics[k]
                        m.cache_create_tokens += cc
                        m.cache_read_tokens += cr
                        m.web_search_count += ws
                        m.web_fetch_count += wf
                        setattr(m, tok_attr, getattr(m, tok_attr) + out)
                        for tn in tool_names:
                            m.tool_counts[tn] = m.tool_counts.get(tn, 0) + 1
                        if has_thinking:
                            m.thinking_turns += 1
                        if is_agentic:
                            m.agentic_turns += 1

                        # Per-project / per-session accumulation
                        if cwd:
                            pa = proj_acc[k].setdefault(cwd, {
                                "opus": 0, "sonnet": 0, "haiku": 0, "other": 0,
                                "turns": 0, "tool_errors": 0, "sessions": {},
                            })
                            pa[model_key] += out
                            pa["turns"] += 1
                            if session_id:
                                sa = pa["sessions"].setdefault(session_id, {
                                    "opus": 0, "sonnet": 0, "haiku": 0, "other": 0,
                                    "turns": 0, "tool_errors": 0,
                                    "first_ts": ts19, "last_ts": ts19,
                                })
                                sa[model_key] += out
                                sa["turns"] += 1
                                if ts19 < sa["first_ts"]:
                                    sa["first_ts"] = ts19
                                if ts19 > sa["last_ts"]:
                                    sa["last_ts"] = ts19

        except OSError:
            continue

    # Use "recent" window's proj_acc for spike detection if available
    recent_pa = proj_acc.get("recent") if "recent" in windows else None

    for k in windows:
        metrics[k].unique_projects = len(cwds[k])
        metrics[k].unique_branches = len(branches[k])
        metrics[k].session_count = len(sessions[k])
        metrics[k].files_edited = len(edited_files[k])
        metrics[k].project_usage, metrics[k].health_alerts = _assemble_project_usage(
            proj_acc[k], session_titles=session_titles,
            recent_proj_acc=recent_pa if k != "recent" else None,
        )
        metrics[k].session_usage = _assemble_flat_sessions(
            proj_acc[k], session_titles=session_titles,
        )

    return metrics


# ---------------------------------------------------------------------------
# Budget estimation
# ---------------------------------------------------------------------------

def load_stats_cache(path: str | None = None) -> dict[str, Any]:
    """Load the ~/.claude/stats-cache.json (historical data only, may be stale)."""
    if path is None:
        path = "~/.claude/stats-cache.json"
    resolved = Path(path).expanduser()
    if not resolved.exists():
        return {"dailyActivity": [], "dailyModelTokens": []}
    with resolved.open() as f:
        return json.load(f)



# ---------------------------------------------------------------------------
# Sub-session tracking
# ---------------------------------------------------------------------------

@dataclass
class SessionInfo:
    session_start: datetime        # when current session started (UTC)
    output_all: int                # tokens used this session (all models)
    output_sonnet: int             # tokens used this session (Sonnet only)
    pct_all: float                 # % of session budget used (0 if unknown)
    pct_sonnet: float              # % of session budget used (Sonnet; 0 if unknown)
    hours_remaining: float         # hours until estimated session reset (0 if unknown)
    session_reset_label: str       # e.g. "—" when unavailable, "Today at 5:00 PM" from live


def _parse_rate_limit_reset(text: str) -> datetime | None:
    """
    Parse session reset time from a JSONL rate-limit message.

    Message format: "resets 5pm (Europe/Amsterdam)" or any timezone.
    Uses the timezone string from the message itself — no hardcoded tz.
    Returns UTC datetime of the next session start, or None on failure.
    """
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    m = _RATE_LIMIT_RE.search(text)
    if not m:
        return None

    hour = int(m.group(1))
    meridiem = m.group(2).lower()
    tz_name = m.group(3).strip()

    # Convert to 24-hour
    if meridiem == "am":
        hour_24 = 0 if hour == 12 else hour
    else:
        hour_24 = 12 if hour == 12 else hour + 12

    try:
        msg_tz = ZoneInfo(tz_name.strip())
    except (ZoneInfoNotFoundError, KeyError, ValueError):
        return None

    # We need a context timestamp to determine the reset date — caller must provide it
    return hour_24, msg_tz  # type: ignore[return-value]  # partial — used below


def find_session_boundaries(since: datetime) -> list[datetime]:
    """
    Scan JSONL files for rate-limit messages since `since`.
    Returns sorted list of session reset datetimes in UTC — each is the START of a new session.
    Uses the timezone embedded in the rate-limit message text (not hardcoded).
    """
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    projects_dir = Path.home() / ".claude" / "projects"
    since_str = since.strftime("%Y-%m-%dT%H:%M:%S")
    boundaries: list[datetime] = []

    if not projects_dir.exists():
        return boundaries

    since_ts = since.timestamp()
    for filepath in glob.glob(str(projects_dir / "**" / "*.jsonl"), recursive=True):
        try:
            if Path(filepath).stat().st_mtime < since_ts:
                continue  # file not modified since our window — no new entries possible
            with open(filepath, errors="ignore") as fh:
                for raw in fh:
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        obj = json.loads(raw)
                    except json.JSONDecodeError:
                        continue

                    ts = obj.get("timestamp", "")
                    if not ts or ts[:19] < since_str:
                        continue
                    if obj.get("type") != "assistant":
                        continue

                    content = obj.get("message", {}).get("content", [])
                    if not isinstance(content, list):
                        continue

                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        text = block.get("text", "")
                        if "You've hit your limit" not in text:
                            continue

                        m = _RATE_LIMIT_RE.search(text)
                        if not m:
                            continue

                        hour = int(m.group(1))
                        meridiem = m.group(2).lower()
                        tz_name = m.group(3).strip()

                        # Convert to 24-hour
                        if meridiem == "am":
                            hour_24 = 0 if hour == 12 else hour
                        else:
                            hour_24 = 12 if hour == 12 else hour + 12

                        try:
                            msg_tz = ZoneInfo(tz_name.strip())
                        except (ZoneInfoNotFoundError, KeyError, ValueError):
                            continue

                        try:
                            event_utc = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                        except ValueError:
                            continue

                        event_local = event_utc.astimezone(msg_tz)
                        reset_date = event_local.date()

                        # Build reset datetime in the message's timezone
                        reset_local = datetime.combine(reset_date, _time(hour_24, 0), tzinfo=msg_tz)
                        # Push to next day if the reset time is not in the future from the event
                        if reset_local <= event_local:
                            from datetime import timedelta as _td
                            reset_local = datetime.combine(
                                reset_date + _td(days=1),
                                _time(hour_24, 0),
                                tzinfo=msg_tz,
                            )

                        boundaries.append(reset_local.astimezone(timezone.utc))
        except OSError:
            continue

    return sorted(set(boundaries))


def find_current_session_start(
    period_start: datetime,
    precomputed_boundaries: list[datetime] | None = None,
) -> datetime:
    """Return when the current sub-session started (UTC).

    Prefers explicit rate-limit boundaries from JSONL files. Falls back to
    period_start when no rate-limit messages exist for this period.
    """
    now = datetime.now(timezone.utc)
    boundaries = (
        precomputed_boundaries
        if precomputed_boundaries is not None
        else find_session_boundaries(period_start)
    )
    past = [b for b in boundaries if b <= now]

    if past:
        anchor = past[-1]
        gaps_h = [
            (boundaries[i] - boundaries[i - 1]).total_seconds() / 3600
            for i in range(1, len(boundaries))
            if (boundaries[i] - boundaries[i - 1]).total_seconds() / 3600 <= 12
        ]
        sub_session_h = sorted(gaps_h)[len(gaps_h) // 2] if gaps_h else 5.0
        # Walk forward from anchor until the next window would exceed now.
        current = anchor
        while current + timedelta(hours=sub_session_h) <= now:
            current += timedelta(hours=sub_session_h)
        return current

    # No rate-limit messages this period — start of period is the safest fallback.
    return period_start


def build_session_info(
    state: dict[str, Any],
    precomputed_boundaries: list[datetime] | None = None,
) -> SessionInfo:
    """Build current sub-session information, matching /status 'Current session'.

    pct_all/pct_sonnet are returned as 0.0 when no session budget is known.
    The UI should override these with live /status data when available.
    """
    now = datetime.now(timezone.utc)
    period_start, _ = current_billing_period()

    if precomputed_boundaries is not None:
        period_boundaries = [b for b in precomputed_boundaries if b >= period_start]
    else:
        period_boundaries = find_session_boundaries(period_start)

    session_start = find_current_session_start(period_start, precomputed_boundaries=period_boundaries)
    usage = count_tokens_since(session_start)

    # Estimate next reset time from observed rate-limit gaps
    past_boundaries = [b for b in period_boundaries if b <= now]
    if past_boundaries:
        gaps_h = [
            (period_boundaries[i] - period_boundaries[i - 1]).total_seconds() / 3600
            for i in range(1, len(period_boundaries))
            if (period_boundaries[i] - period_boundaries[i - 1]).total_seconds() / 3600 <= 12
        ]
        sub_session_h = sorted(gaps_h)[len(gaps_h) // 2] if gaps_h else 5.0
        estimated_next_reset = session_start + timedelta(hours=sub_session_h)
        hours_remaining = max(0.0, (estimated_next_reset - now).total_seconds() / 3600)

        local_reset = estimated_next_reset.astimezone()
        time_fmt = "%-H:%M" if uses_24h_time() else "%-I:%M %p"
        if local_reset.date() == datetime.now().date():
            session_reset_label = "Today at " + local_reset.strftime(time_fmt)
        else:
            session_reset_label = local_reset.strftime(f"%a at {time_fmt}")
    else:
        # No observed boundaries — live /status will provide the real label
        hours_remaining = 0.0
        session_reset_label = "—"

    return SessionInfo(
        session_start=session_start,
        output_all=usage.output_all,
        output_sonnet=usage.output_sonnet,
        pct_all=0.0,      # unknown without budget — overridden by live data
        pct_sonnet=0.0,   # unknown without budget — overridden by live data
        hours_remaining=round(hours_remaining, 1),
        session_reset_label=session_reset_label,
    )


# ---------------------------------------------------------------------------
# Prediction
# ---------------------------------------------------------------------------

@dataclass
class Prediction:
    # Current period
    output_all: int = 0
    output_sonnet: int = 0

    # Budget estimates (None = unknown)
    budget_all: int | None = None
    budget_sonnet: int | None = None

    # Derived percentages (matching /status display)
    pct_all: float = 0.0
    pct_sonnet: float = 0.0

    # Time (all-models)
    days_remaining: float = 0.0
    reset_label: str = ""
    period_start: str = ""
    period_end: str = ""

    # Time (Sonnet — independent reset schedule)
    days_remaining_sonnet: float = 0.0
    reset_label_sonnet: str = ""

    # Trigger
    will_trigger: bool = False
    projected_pct_all: float = 0.0

    # Sub-session fields
    session_start: str = ""
    session_pct_all: float = 0.0
    session_pct_sonnet: float = 0.0
    session_hours_remaining: float = 0.0
    session_reset_label: str = ""

    # True when the last /status fetch returned an API error
    outage: bool = False


def _hours_until_dated_reset_label(label: str, tz_name: str) -> float:
    """Parse a dated reset label like 'Mar 24 at 8pm' and return hours until that time.

    Handles formats: 'Mar 24 at 8pm', 'Mar 28 at 9:59am'.
    Falls back to 0.0 if the label doesn't match.
    """
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    if not tz_name or not tz_name.strip():
        return 0.0
    try:
        tz = ZoneInfo(tz_name.strip())
    except (ZoneInfoNotFoundError, KeyError, ValueError):
        return 0.0

    label = label.strip()
    # Match "Mar 24 at 8pm", "Mar 28 at 9:59am"
    m = re.match(
        r"^([A-Z][a-z]{2})\s+(\d{1,2})\s+at\s+(\d{1,2})(?::(\d{2}))?(am|pm)$",
        label,
    )
    if not m:
        return 0.0

    month_str, day, hour = m.group(1), int(m.group(2)), int(m.group(3))
    minute = int(m.group(4)) if m.group(4) else 0
    meridiem = m.group(5)

    months = {
        "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
        "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
    }
    month = months.get(month_str, 0)
    if month == 0:
        return 0.0

    hour_24 = (0 if hour == 12 else hour) if meridiem == "am" else (12 if hour == 12 else hour + 12)

    now_tz = datetime.now(tz)
    # Try current year first, then next year
    for year in [now_tz.year, now_tz.year + 1]:
        try:
            reset = datetime(year, month, day, hour_24, minute, tzinfo=tz)
            if reset > now_tz:
                return max(0.0, (reset - now_tz).total_seconds() / 3600)
        except ValueError:
            continue
    return 0.0


def _hours_until_reset_label(label: str, tz_name: str) -> float:
    """Parse a live reset label like '2pm' or '2:30pm' and return hours until that time.

    Uses the timezone from /status (e.g. 'Europe/Amsterdam') so the result is
    accurate regardless of the local machine's timezone.
    """
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    if not tz_name or not tz_name.strip():
        return 0.0
    try:
        tz = ZoneInfo(tz_name.strip())
    except (ZoneInfoNotFoundError, KeyError, ValueError):
        return 0.0

    now_tz = datetime.now(tz)
    label = label.strip().lower()

    # Match "2pm", "9am", "12pm"
    m = re.match(r"^(\d{1,2})(am|pm)$", label)
    if m:
        hour, meridiem = int(m.group(1)), m.group(2)
        hour_24 = (0 if hour == 12 else hour) if meridiem == "am" else (12 if hour == 12 else hour + 12)
        minute = 0
    else:
        # Match "2:30pm", "12:00am"
        m = re.match(r"^(\d{1,2}):(\d{2})(am|pm)$", label)
        if not m:
            return 0.0
        hour, minute, meridiem = int(m.group(1)), int(m.group(2)), m.group(3)
        hour_24 = (0 if hour == 12 else hour) if meridiem == "am" else (12 if hour == 12 else hour + 12)

    reset = now_tz.replace(hour=hour_24, minute=minute, second=0, microsecond=0)
    if reset <= now_tz:
        reset += timedelta(days=1)
    return max(0.0, (reset - now_tz).total_seconds() / 3600)


def build_prediction(
    state: dict[str, Any],
    force: bool = False,
    precomputed_boundaries: list[datetime] | None = None,
) -> Prediction:
    """Compute the full prediction from current token usage + live /status data.

    Percentages and reset labels come from claude /status (ground-truth).
    Token counts come from JSONL parsing (/status doesn't expose those).
    Budget is back-calculated from live percentage + token count.
    """
    from .status_fetcher import fetch_live_status  # local import to keep startup fast

    start, end = current_billing_period()
    usage = count_tokens_since(start)

    days_rem = days_until_reset()
    elapsed_days = max((_WEEK_SECONDS / 86400) - days_rem, 1 / 24)

    session = build_session_info(state, precomputed_boundaries=precomputed_boundaries)

    live = fetch_live_status(force=force)
    pct_all = live.weekly_pct_all
    pct_sonnet = live.weekly_pct_sonnet
    session_pct_all = live.session_pct
    session_reset_label = live.session_reset_label
    live_weekly_reset = live.weekly_reset_label
    live_sonnet_reset = live.weekly_reset_label_sonnet or live.weekly_reset_label
    session_hours_remaining = _hours_until_reset_label(
        live.session_reset_label, live.session_reset_tz
    )
    sonnet_hours = _hours_until_dated_reset_label(
        live_sonnet_reset, live.weekly_reset_tz_sonnet or live.weekly_reset_tz
    )
    # Fall back to all-models days_remaining if Sonnet date parsing fails
    days_rem_sonnet = sonnet_hours / 24 if sonnet_hours > 0 else days_rem

    # Back-calculate budget from live percentage + observed token count
    # pct = tokens / budget * 100  →  budget = tokens / (pct / 100)
    budget_all: int | None = None
    budget_sonnet: int | None = None
    if live.weekly_pct_all > 0 and usage.output_all > 0:
        budget_all = int(usage.output_all / (live.weekly_pct_all / 100))
    if live.weekly_pct_sonnet > 0 and usage.output_sonnet > 0:
        budget_sonnet = int(usage.output_sonnet / (live.weekly_pct_sonnet / 100))

    # Projection: if current rate continues (only meaningful when budget is known)
    if budget_all:
        daily_rate = usage.output_all / elapsed_days
        projected = usage.output_all + (daily_rate * days_rem)
        projected_pct = (projected / max(budget_all, 1)) * 100
        remaining_pct = max(0.0, 100.0 - projected_pct)
    else:
        projected_pct = pct_all  # best guess: current usage
        remaining_pct = max(0.0, 100.0 - pct_all)

    return Prediction(
        output_all=usage.output_all,
        output_sonnet=usage.output_sonnet,
        budget_all=budget_all,
        budget_sonnet=budget_sonnet,
        pct_all=round(pct_all, 1),
        pct_sonnet=round(pct_sonnet, 1),
        days_remaining=round(days_rem, 2),
        reset_label=live_weekly_reset,
        days_remaining_sonnet=round(days_rem_sonnet, 2),
        reset_label_sonnet=live_sonnet_reset,
        period_start=start.isoformat(),
        period_end=end.isoformat(),
        will_trigger=(remaining_pct >= 1.0),
        projected_pct_all=round(projected_pct, 1),
        session_start=session.session_start.isoformat(),
        session_pct_all=round(session_pct_all, 1),
        session_pct_sonnet=session.pct_sonnet,
        session_hours_remaining=round(session_hours_remaining, 1),
        session_reset_label=session_reset_label,
        outage=live.outage,
    )


def should_trigger(prediction: Prediction, config: dict[str, Any]) -> bool:
    """
    Returns True if Penny should spawn agents.
    Fires when predicted unused capacity >= threshold AND ≤ N days left.
    """
    cfg = config.get("trigger", {})
    min_remaining = cfg.get("min_capacity_percent", 30)
    max_days = cfg.get("max_days_remaining", 2)
    remaining_pct = max(0.0, 100.0 - prediction.projected_pct_all)
    return remaining_pct >= min_remaining and prediction.days_remaining <= max_days


def get_usage_bar(pct_used: float, width: int = 8) -> str:
    """Return a color-coded emoji square progress bar.

    Green < 60 %, yellow 60–80 %, red ≥ 80 %.
    """
    filled = int(min(pct_used, 100) / 100 * width)
    if pct_used < 60:
        block = "🟩"
    elif pct_used < 80:
        block = "🟨"
    else:
        block = "🟥"
    return block * filled + "⬜" * (width - filled)


# ---------------------------------------------------------------------------
# Lightweight health check (1-minute interval)
# ---------------------------------------------------------------------------

def quick_health_scan(
    file_offsets: dict[str, int],
    baselines: dict[str, dict] | None = None,
) -> tuple[list[dict], dict[str, int]]:
    """Fast incremental JSONL scan for health alerts.

    Reads only new lines since last scan (tracked by byte offset).
    Returns (alerts, updated_offsets).
    """
    projects_dir = Path.home() / ".claude" / "projects"
    new_offsets: dict[str, int] = dict(file_offsets)
    baselines = baselines or {}

    # Per-project accumulators for this delta
    delta: dict[str, dict] = {}  # cwd -> {tokens, errors, turns, sessions}

    if not projects_dir.exists():
        return [], new_offsets

    for filepath in glob.glob(str(projects_dir / "**" / "*.jsonl"), recursive=True):
        try:
            size = Path(filepath).stat().st_size
            prev_offset = file_offsets.get(filepath, 0)
            if size <= prev_offset:
                new_offsets[filepath] = size
                continue

            with open(filepath, "rb") as fh:
                fh.seek(prev_offset)
                raw_bytes = fh.read()
                new_offsets[filepath] = prev_offset + len(raw_bytes)

            for raw_line in raw_bytes.decode("utf-8", errors="ignore").splitlines():
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                try:
                    obj = json.loads(raw_line)
                except json.JSONDecodeError:
                    continue

                entry_type = obj.get("type")
                cwd = obj.get("cwd")
                if not cwd:
                    continue

                d = delta.setdefault(cwd, {
                    "tokens": 0, "errors": 0, "turns": 0, "sessions": set(),
                })

                if entry_type == "assistant":
                    msg = obj.get("message", {})
                    out = msg.get("usage", {}).get("output_tokens", 0)
                    d["tokens"] += out
                    d["turns"] += 1
                    sid = obj.get("sessionId")
                    if sid:
                        d["sessions"].add(sid)
                elif entry_type == "user":
                    result = obj.get("toolUseResult")
                    if isinstance(result, dict) and result.get("is_error"):
                        d["errors"] += 1

        except OSError:
            continue

    # Evaluate deltas against baselines
    alerts: list[dict] = []
    for cwd, d in delta.items():
        turns = d["turns"]
        errors = d["errors"]
        tokens = d["tokens"]
        name = Path(cwd).name

        # Error rate in this 1-min window
        total_events = errors + turns
        error_rate = errors / total_events * 100 if total_events > 0 else 0

        # Compare against baseline
        bl = baselines.get(cwd, {})
        avg_rate = bl.get("avg_tokens_per_hour", 0)
        reasons: list[str] = []
        level = "green"

        # Absolute: high error rate in the delta
        if error_rate > 50 and errors >= 3:
            level = "red"
            reasons.append(f"High error rate ({error_rate:.0f}%) in last minute")
        elif error_rate > 20 and errors >= 2:
            level = "yellow"
            reasons.append(f"Elevated error rate ({error_rate:.0f}%) in last minute")

        # Spike: tokens in 1 min extrapolated to hourly rate vs baseline
        if avg_rate > 0 and tokens > 0:
            hourly_rate = tokens * 60  # extrapolate 1 min to 1 hour
            spike = hourly_rate / avg_rate
            if spike > 5:
                level = "red"
                reasons.append(f"Burn rate spike ({spike:.1f}x baseline)")
            elif spike > 3:
                if level == "green":
                    level = "yellow"
                reasons.append(f"Burn rate spike ({spike:.1f}x baseline)")

        if level != "green":
            alerts.append({
                "project": name,
                "cwd": cwd,
                "health": level,
                "reasons": reasons,
            })

    return alerts, new_offsets
