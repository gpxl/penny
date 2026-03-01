"""Usage stats parsing and capacity prediction for Nae Nae.

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
from dataclasses import dataclass
from datetime import datetime, time as _time, timedelta, timezone
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


def days_until_reset() -> float:
    """Fractional days until the next billing period reset."""
    _, end = current_billing_period()
    remaining = (end - datetime.now(timezone.utc)).total_seconds()
    return max(0.0, remaining / 86400)


def reset_label() -> str:
    """Human-readable reset date/time in local time, e.g. 'Fri Mar 6 at 9:00 PM'."""
    _, end = current_billing_period()
    local = end.astimezone()
    return local.strftime("%a %b %-d at %-I:%M %p")


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

    for filepath in glob.glob(str(projects_dir / "**" / "*.jsonl"), recursive=True):
        try:
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


def estimate_budget_from_history(state: dict[str, Any]) -> dict[str, int | None]:
    """
    Estimate weekly output token budgets from stored period history in state.json.
    Returns {'all': N | None, 'sonnet': N | None}.
    Returns None values when there is insufficient history to estimate.
    """
    history = state.get("period_history", [])
    defaults: dict[str, int | None] = {"all": None, "sonnet": None}

    if len(history) < 2:
        return defaults

    all_totals = [p["output_all"] for p in history if p.get("output_all", 0) > 0]
    sonnet_totals = [p["output_sonnet"] for p in history if p.get("output_sonnet", 0) > 0]

    if not all_totals:
        return defaults

    # 90th percentile (or max if < 4 samples) as the "likely limit-hit week"
    def percentile90(vals: list[int]) -> int:
        s = sorted(vals)
        if len(s) < 4:
            return max(s)
        return s[min(int(len(s) * 0.9), len(s) - 1)]

    return {
        "all": percentile90(all_totals),
        "sonnet": percentile90(sonnet_totals) if sonnet_totals else None,
    }


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
        msg_tz = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, KeyError):
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

    for filepath in glob.glob(str(projects_dir / "**" / "*.jsonl"), recursive=True):
        try:
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
                            msg_tz = ZoneInfo(tz_name)
                        except (ZoneInfoNotFoundError, KeyError):
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


def find_current_session_start(period_start: datetime) -> datetime:
    """Return when the current sub-session started (UTC).

    Prefers explicit rate-limit boundaries from JSONL files. Falls back to
    period_start when no rate-limit messages exist for this period.
    """
    now = datetime.now(timezone.utc)
    boundaries = find_session_boundaries(period_start)
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


def build_session_info(state: dict[str, Any]) -> SessionInfo:
    """Build current sub-session information, matching /status 'Current session'.

    pct_all/pct_sonnet are returned as 0.0 when no session budget is known.
    The UI should override these with live /status data when available.
    """
    now = datetime.now(timezone.utc)
    period_start, _ = current_billing_period()

    session_start = find_current_session_start(period_start)
    usage = count_tokens_since(session_start)

    # Estimate next reset time from observed rate-limit gaps
    boundaries = find_session_boundaries(period_start)
    past_boundaries = [b for b in boundaries if b <= now]
    if past_boundaries:
        gaps_h = [
            (boundaries[i] - boundaries[i - 1]).total_seconds() / 3600
            for i in range(1, len(boundaries))
            if (boundaries[i] - boundaries[i - 1]).total_seconds() / 3600 <= 12
        ]
        sub_session_h = sorted(gaps_h)[len(gaps_h) // 2] if gaps_h else 5.0
        estimated_next_reset = session_start + timedelta(hours=sub_session_h)
        hours_remaining = max(0.0, (estimated_next_reset - now).total_seconds() / 3600)

        local_reset = estimated_next_reset.astimezone()
        if local_reset.date() == datetime.now().date():
            session_reset_label = "Today at " + local_reset.strftime("%-I:%M %p")
        else:
            session_reset_label = local_reset.strftime("%a at %-I:%M %p")
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

    # Time
    days_remaining: float = 0.0
    reset_label: str = ""
    period_start: str = ""
    period_end: str = ""

    # Trigger
    will_trigger: bool = False
    projected_pct_all: float = 0.0

    # Sub-session fields
    session_start: str = ""
    session_pct_all: float = 0.0
    session_pct_sonnet: float = 0.0
    session_hours_remaining: float = 0.0
    session_reset_label: str = ""


def build_prediction(state: dict[str, Any]) -> Prediction:
    """Compute the full prediction from current token usage + budget estimate.

    Percentages and reset labels are overridden with ground-truth values from
    claude /status when available (via status_fetcher). Token counts and budget
    absolutes still come from JSONL parsing — /status doesn't expose those.
    Budget is back-calculated from live percentage + token count when possible.
    """
    from .status_fetcher import fetch_live_status  # local import to keep startup fast

    start, end = current_billing_period()
    usage = count_tokens_since(start)
    hist_budget = estimate_budget_from_history(state)

    days_rem = days_until_reset()
    elapsed_days = max((_WEEK_SECONDS / 86400) - days_rem, 1 / 24)

    session = build_session_info(state)

    # --- Override with live /status data when available ---
    live = fetch_live_status()
    if live is not None:
        pct_all = live.weekly_pct_all
        pct_sonnet = live.weekly_pct_sonnet
        session_pct_all = live.session_pct
        session_reset_label = live.session_reset_label
        live_weekly_reset = live.weekly_reset_label

        # Back-calculate budget from live percentage + observed token count
        # pct = tokens / budget * 100  →  budget = tokens / (pct / 100)
        if live.weekly_pct_all > 0 and usage.output_all > 0:
            budget_all: int | None = int(usage.output_all / (live.weekly_pct_all / 100))
        else:
            budget_all = hist_budget.get("all")

        if live.weekly_pct_sonnet > 0 and usage.output_sonnet > 0:
            budget_sonnet: int | None = int(usage.output_sonnet / (live.weekly_pct_sonnet / 100))
        else:
            budget_sonnet = hist_budget.get("sonnet")
    else:
        # No live data — use JSONL-estimated percentages (less accurate)
        budget_all = hist_budget.get("all")
        budget_sonnet = hist_budget.get("sonnet")
        pct_all = (usage.output_all / max(budget_all, 1)) * 100 if budget_all else 0.0
        pct_sonnet = (usage.output_sonnet / max(budget_sonnet, 1)) * 100 if budget_sonnet else 0.0
        session_pct_all = session.pct_all
        session_reset_label = session.session_reset_label
        live_weekly_reset = reset_label()

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
        period_start=start.isoformat(),
        period_end=end.isoformat(),
        will_trigger=(remaining_pct >= 1.0),
        projected_pct_all=round(projected_pct, 1),
        session_start=session.session_start.isoformat(),
        session_pct_all=round(session_pct_all, 1),
        session_pct_sonnet=session.pct_sonnet,
        session_hours_remaining=session.hours_remaining,
        session_reset_label=session_reset_label,
    )


def should_trigger(prediction: Prediction, config: dict[str, Any]) -> bool:
    """
    Returns True if Nae Nae should spawn agents.
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
