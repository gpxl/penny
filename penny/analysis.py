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
    """Return a compact version of a reset label for inline display.

    - "Mar 28 at 10am" → "Mar 28"  (weekly: strip time, just show date)
    - "5pm" → "5pm"               (session: already short, pass through)
    - "Mar 24 at 20" → "Mar 24"   (24h formatted: strip time)
    """
    if not label or label == "—":
        return label
    # Weekly labels have "at" — strip everything from "at" onward
    m = re.match(r"^([A-Z][a-z]{2}\s+\d{1,2})\s+at\s+", label)
    if m:
        return m.group(1)
    return label


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

                    # Extract tool results from user records
                    if entry_type == "user":
                        result = obj.get("toolUseResult")
                        if isinstance(result, dict):
                            if result.get("is_error"):
                                metrics.tool_error_count += 1
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

        except OSError:
            continue

    metrics.unique_projects = len(cwds)
    metrics.unique_branches = len(branches)
    metrics.session_count = len(sessions)
    metrics.files_edited = len(edited_files)
    return metrics


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

                    if entry_type == "user":
                        result = obj.get("toolUseResult")
                        if isinstance(result, dict):
                            if result.get("is_error"):
                                for k in matched:
                                    metrics[k].tool_error_count += 1
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

        except OSError:
            continue

    for k in windows:
        metrics[k].unique_projects = len(cwds[k])
        metrics[k].unique_branches = len(branches[k])
        metrics[k].session_count = len(sessions[k])
        metrics[k].files_edited = len(edited_files[k])

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
