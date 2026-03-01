"""Fetch accurate session/weekly usage by running claude interactively
and parsing the /status Usage tab output.

Uses pexpect to drive a claude subprocess in a pseudo-terminal, sends
/status, navigates to the Usage tab (the dialog has four tabs:
Settings | Status | Config | Usage), captures the terminal output via
the pyte terminal emulator (which tracks screen state cleanly, avoiding
mangled text from interleaved cursor-positioning sequences), and extracts
the three percentage values and reset times with regex.

Results are cached for 30 minutes.

Requirements: pexpect>=4.8, pyte>=0.8
"""
from __future__ import annotations

import os
import re
import shutil
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


# Cache TTL: /status data changes at most every few minutes.
# 30 minutes is safe — any drift is temporary.
_CACHE_TTL_SECONDS = 30 * 60

_cache: LiveStatus | None = None

# Terminal dimensions for the spawned claude process.
# Dialog requires at least 40 rows; 200 cols avoids line-wrapping.
_ROWS = 50
_COLS = 200


@dataclass
class LiveStatus:
    """Parsed data from claude /status Usage tab, all values ground-truth from Anthropic."""
    session_pct: float          # "16% used"
    session_reset_label: str    # "2pm" (extracted from "Resets 2pm (Europe/Amsterdam)")
    weekly_pct_all: float       # "30% used"
    weekly_pct_sonnet: float    # "41% used"
    weekly_reset_label: str     # "Mar 6 at 9pm"
    fetched_at: datetime


def _parse_usage_screen(screen_txt: str) -> LiveStatus | None:
    """
    Parse percentages and reset labels from the pyte-rendered Usage tab text.

    The Usage tab shows (in order):
      Current session    → N% used  →  Resets TIME (Europe/Amsterdam)
      Current week (all) → N% used  →  Resets DATE at TIME (Europe/Amsterdam)
      Current week (Sonnet) → N% used (same reset line)

    The pyte screen may contain remnants of the previous tab above the tab bar.
    We locate the tab bar line ("Usage" with "Config" also visible) and parse
    only the content below it.
    """
    lines = screen_txt.splitlines()

    # Find the tab bar: the line that contains both "Config" and "Usage"
    # (when Usage tab is active, the tab bar renders something like:
    #  ")            Status   Config   Usage")
    tab_bar_idx = -1
    for i, line in enumerate(lines):
        if "Usage" in line and ("Config" in line or "Status" in line):
            tab_bar_idx = i
            break

    section = "\n".join(lines[tab_bar_idx:]) if tab_bar_idx != -1 else screen_txt

    pcts = re.findall(r"(\d+(?:\.\d+)?)\s*%\s*used", section)
    resets = re.findall(r"Resets\s+(.+?)\s*\(Europe/Amsterdam\)", section)

    if len(pcts) < 3 or len(resets) < 1:
        return None

    # Values appear in order: session, all-models week, sonnet week.
    # Resets appear: session reset (short: "2pm"), weekly reset (long: "Mar 6 at 9pm").
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


def _feed_child(child: Any, stream: Any, secs: float) -> None:
    """Read from child for up to secs seconds and feed bytes to the pyte stream."""
    try:
        import pexpect  # type: ignore[import]
    except ImportError:
        return
    deadline = time.time() + secs
    while time.time() < deadline:
        try:
            child.expect(pexpect.TIMEOUT, timeout=0.1)
            if child.before:
                stream.feed(child.before)
        except Exception:
            break


def fetch_live_status(force: bool = False) -> LiveStatus | None:
    """
    Run claude interactively, navigate /status → Usage tab, parse and return the result.

    Returns None on any failure so callers can fall back gracefully.
    Caches results for 30 minutes to avoid spawning claude on every refresh.

    Interaction flow:
      1. Spawn claude, wait for ❯ prompt
      2. Send /status + second Enter (first Enter selects autocomplete, second executes)
      3. Wait for dialog navigation hint ("to cycle")
      4. Press Right Arrow × 2 to reach Usage tab (Settings | Status | Config | Usage)
      5. Read clean screen text via pyte, parse
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

    try:
        import pyte  # type: ignore[import]
    except ImportError:
        return None

    env = os.environ.copy()
    env.pop("CLAUDECODE", None)
    env.pop("CLAUDE_CODE", None)

    screen = pyte.Screen(_COLS, _ROWS)
    pyte_stream = pyte.ByteStream(screen)

    result: LiveStatus | None = None

    try:
        child = pexpect.spawn(
            "claude",
            # No extra flags — interactive mode only.
            # --no-session-persistence only works with --print and errors otherwise.
            env=env,
            timeout=45,
            encoding=None,             # bytes mode required for pyte
            dimensions=(_ROWS, _COLS),
        )

        # Phase 1: wait for the interactive prompt (❯ = U+276F, UTF-8: e2 9d af).
        idx = child.expect(
            [b"\xe2\x9d\xaf", pexpect.TIMEOUT, pexpect.EOF],
            timeout=30,
        )
        pyte_stream.feed(child.before or b"")
        pyte_stream.feed(child.after or b"")
        if idx != 0:   # TIMEOUT or EOF — claude didn't start cleanly
            child.close(force=True)
            return None

        # Let the TUI fully stabilize (hint text, etc.)
        _feed_child(child, pyte_stream, 1.5)

        # Phase 2: send /status.
        # In the claude TUI, the first Enter (\n from sendline) selects the
        # highlighted autocomplete item but does NOT execute the command.
        # A second Enter (\r) executes it.
        child.send(b"/status\n")
        time.sleep(0.3)
        child.send(b"\r")

        # Phase 3: wait for the /status dialog to open.
        # Every tab of the dialog shows navigation hints.
        idx2 = child.expect(
            [b"to cycle", b"Esc to cancel", pexpect.TIMEOUT, pexpect.EOF],
            timeout=15,
        )
        pyte_stream.feed(child.before or b"")
        pyte_stream.feed(child.after or b"")
        if idx2 >= 2:   # dialog didn't open
            child.close(force=True)
            return None

        _feed_child(child, pyte_stream, 1.5)

        # Phase 4: navigate to the Usage tab.
        # Tab order: Settings(1) | Status(2) | Config(3) | Usage(4)
        # Claude opens on the Status tab; Right Arrow × 2 reaches Usage.
        child.send(b"\x1b[C")   # Right Arrow → Config tab
        _feed_child(child, pyte_stream, 0.8)
        child.send(b"\x1b[C")   # Right Arrow → Usage tab
        _feed_child(child, pyte_stream, 2.5)

        # Phase 5: extract clean screen text from pyte.
        screen_txt = "\n".join(row.rstrip() for row in screen.display)
        result = _parse_usage_screen(screen_txt)

        # Phase 6: close gracefully.
        child.send(b"\x03")   # Ctrl-C
        try:
            child.expect(pexpect.EOF, timeout=5)
            pyte_stream.feed(child.before or b"")
        except Exception:
            pass
        child.close(force=True)

    except Exception:
        return None

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
