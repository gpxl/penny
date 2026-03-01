"""Fetch accurate session/weekly usage by running claude interactively
and parsing the /status output.

Uses pexpect to drive a claude subprocess in a pseudo-terminal, sends
/status, captures the terminal output (including ANSI escape codes),
strips them, and extracts the three percentage values and reset times
with regex. Results are cached for 30 minutes.
"""
from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


# Cache TTL: /status data changes at most every few minutes.
# 30 minutes is safe — any drift is temporary.
_CACHE_TTL_SECONDS = 30 * 60

_cache: LiveStatus | None = None


@dataclass
class LiveStatus:
    """Parsed data from claude /status, all values ground-truth from Anthropic."""
    session_pct: float          # "9% used"
    session_reset_label: str    # "2pm (Europe/Amsterdam)" → converted to label
    weekly_pct_all: float       # "29% used"
    weekly_pct_sonnet: float    # "39% used"
    weekly_reset_label: str     # "Mar 6 at 9pm (Europe/Amsterdam)"
    fetched_at: datetime


_ANSI_RE = re.compile(
    r'\x1b(?:'
    r'\[[0-9;?]*[A-Za-z]'   # CSI sequences (colors, cursor movement)
    r'|[=>]'                 # two-char sequences
    r'|\][^\x07\x1b]*(?:\x07|\x1b\\)'  # OSC sequences
    r'|[^[\\]'               # single-char sequences
    r')'
)


def _strip_ansi(text: str) -> str:
    """Remove all ANSI/VT100 escape sequences from terminal output."""
    return _ANSI_RE.sub("", text)


def _parse_status_output(raw: str) -> LiveStatus | None:
    """
    Extract percentages and reset times from stripped /status terminal output.

    /status shows (in order):
      Current session    → N% used  →  Resets TIME (Europe/Amsterdam)
      Current week (all) → N% used  →  Resets DATE at TIME (Europe/Amsterdam)
      Current week (Sonnet) → N% used (same reset)
    """
    text = _strip_ansi(raw)

    pcts = re.findall(r"(\d+(?:\.\d+)?)\s*%\s*used", text)
    resets = re.findall(r"Resets\s+(.+?)\s*\(Europe/Amsterdam\)", text)

    if len(pcts) < 3 or len(resets) < 1:
        return None

    # Session reset is short form: "2pm" or "9am"
    # Weekly reset is long form: "Mar 6 at 9pm"
    # Use the first reset as session, last distinct one as weekly.
    session_reset = resets[0].strip()
    weekly_reset = resets[-1].strip()

    return LiveStatus(
        session_pct=float(pcts[0]),
        session_reset_label=session_reset,
        weekly_pct_all=float(pcts[1]),
        weekly_pct_sonnet=float(pcts[2]),
        weekly_reset_label=weekly_reset,
        fetched_at=datetime.now(timezone.utc),
    )


def fetch_live_status(force: bool = False) -> LiveStatus | None:
    """
    Run claude interactively, send /status, parse and return the result.

    Returns None on any failure so callers can fall back gracefully.
    Caches results for 30 minutes to avoid spawning claude on every refresh.
    """
    global _cache

    if not force and _cache is not None:
        age = (datetime.now(timezone.utc) - _cache.fetched_at).total_seconds()
        if age < _CACHE_TTL_SECONDS:
            return _cache

    if not shutil.which("claude"):
        return None

    try:
        import pexpect  # type: ignore[import]
    except ImportError:
        return None

    env = os.environ.copy()
    env.pop("CLAUDECODE", None)
    env.pop("CLAUDE_CODE", None)

    collected: list[str] = []

    try:
        child = pexpect.spawn(
            "claude",
            args=["--no-session-persistence"],
            env=env,
            timeout=45,
            encoding="utf-8",
            codec_errors="replace",
            dimensions=(40, 120),   # rows × cols — wide enough for dialog
        )

        # Phase 1: wait for the interactive prompt to appear.
        # Claude prints a styled prompt when it's ready for input.
        idx = child.expect(
            [
                r"❯",
                r"\$",
                r">",
                r"Human:",
                r"claude>",
                pexpect.TIMEOUT,
                pexpect.EOF,
            ],
            timeout=30,
        )
        if idx >= 5:  # TIMEOUT or EOF — claude didn't start cleanly
            child.close(force=True)
            return None

        collected.append(child.before or "")
        collected.append(child.after or "")

        # Phase 2: send /status.
        child.sendline("/status")

        # Phase 3: wait for the status dialog content.
        # "Europe/Amsterdam" appears in every reset label — reliable signal.
        idx2 = child.expect(
            [r"Europe/Amsterdam", r"Current session", pexpect.TIMEOUT, pexpect.EOF],
            timeout=20,
        )
        collected.append(child.before or "")
        collected.append(child.after or "")

        if idx2 >= 2:  # TIMEOUT or EOF
            child.close(force=True)
            return None

        # Wait a moment for the full dialog to paint, then drain the buffer.
        try:
            child.expect(pexpect.TIMEOUT, timeout=1.5)
            collected.append(child.before or "")
        except Exception:
            pass

        # Phase 4: close gracefully.
        child.sendcontrol("c")
        try:
            child.expect(pexpect.EOF, timeout=5)
            collected.append(child.before or "")
        except Exception:
            pass
        child.close(force=True)

    except Exception:
        return None

    raw = "".join(collected)
    result = _parse_status_output(raw)
    if result:
        _cache = result

    return result


def status_as_prediction_overrides(status: LiveStatus) -> dict[str, Any]:
    """
    Convert LiveStatus into a dict of fields that override Prediction/SessionInfo.

    Only the values /status knows better than JSONL estimation are returned:
    the percentages and reset labels. Token counts and budget absolutes are
    still read from JSONL (they are not in /status output).
    """
    return {
        "session_pct_all": status.session_pct,
        "session_reset_label": status.session_reset_label,
        "pct_all": status.weekly_pct_all,
        "pct_sonnet": status.weekly_pct_sonnet,
        "reset_label": status.weekly_reset_label,
    }
