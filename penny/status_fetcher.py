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

import json
import os
import re
import shutil
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Cache TTL: match the background refresh interval so opening the popover
# always shows data no older than one refresh cycle.
_CACHE_TTL_SECONDS = 5 * 60
# Shorter TTL when the last fetch hit an outage — retry sooner.
_OUTAGE_RETRY_SECONDS = 2 * 60

_cache: LiveStatus | None = None


def _cache_file() -> Path:
    env = os.environ.get("PENNY_HOME")
    d = Path(env) if env else Path.home() / ".penny"
    return d / "status_cache.json"


def _detect_api_error(screen_txt: str) -> bool:
    """Return True if the /status Usage tab is showing an API error instead of data."""
    return (
        "api_error" in screen_txt
        or "Internal server error" in screen_txt
        or "Failed to load usage data" in screen_txt
        or '"type":"error"' in screen_txt
    )


def _save_cache(status: LiveStatus) -> None:
    if status.outage:
        return  # don't persist outage state to disk — restart should try fresh
    try:
        path = _cache_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "session_pct": status.session_pct,
            "session_reset_label": status.session_reset_label,
            "session_reset_tz": status.session_reset_tz,
            "weekly_pct_all": status.weekly_pct_all,
            "weekly_pct_sonnet": status.weekly_pct_sonnet,
            "weekly_reset_label": status.weekly_reset_label,
            "weekly_reset_tz": status.weekly_reset_tz,
            "fetched_at": status.fetched_at.isoformat(),
        }))
    except Exception:
        pass


def _load_cache() -> LiveStatus | None:
    try:
        path = _cache_file()
        if not path.exists():
            return None
        d = json.loads(path.read_text())
        return LiveStatus(
            session_pct=d["session_pct"],
            session_reset_label=d["session_reset_label"],
            session_reset_tz=d["session_reset_tz"],
            weekly_pct_all=d["weekly_pct_all"],
            weekly_pct_sonnet=d["weekly_pct_sonnet"],
            weekly_reset_label=d["weekly_reset_label"],
            weekly_reset_tz=d["weekly_reset_tz"],
            fetched_at=datetime.fromisoformat(d["fetched_at"]),
        )
    except Exception:
        return None

# Terminal dimensions for the spawned claude process.
# Dialog requires at least 40 rows; 200 cols avoids line-wrapping.
_ROWS = 50
_COLS = 200


@dataclass
class LiveStatus:
    """Parsed data from claude /status Usage tab, all values ground-truth from Anthropic."""
    session_pct: float          # "16% used"
    session_reset_label: str    # "2pm" (extracted from "Resets 2pm (Europe/Amsterdam)")
    session_reset_tz: str       # "Europe/Amsterdam" (timezone shown in /status)
    weekly_pct_all: float       # "30% used"
    weekly_pct_sonnet: float    # "41% used"
    weekly_reset_label: str     # "Mar 6 at 9pm"
    weekly_reset_tz: str        # "Europe/Amsterdam" (timezone shown in /status)
    fetched_at: datetime
    outage: bool = False        # True when /status returned an API error instead of data


def _parse_usage_screen(screen_txt: str) -> LiveStatus | None:
    """
    Parse percentages and reset labels from the pyte-rendered Usage tab text.

    Labels on screen (from /status Usage tab):
      "Current session"              → N% used  →  Resets TIME (timezone)
      "Current week (all models)"    → N% used  →  Resets DATE at TIME (timezone)
      "Current week (Sonnet only)"   → N% used  (same reset as all models)

    With _COLS=200 each row fits on one line, so label and "N% used" appear together.
    We extract percentages by anchoring to label text to avoid misassignment when the
    pyte screen contains residual content from a prior tab or partial render.

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
    section_lines = section.splitlines()

    # Label-anchored extraction: find the line containing the label, then extract
    # "N% used" from that same line.  This is immune to screen bleed-through because
    # we never rely on the positional order of all "N% used" matches on screen.
    _pct_re = re.compile(r"(\d+(?:\.\d+)?)\s*%\s*used")

    def _extract_labeled_pct(label_pattern: str) -> float | None:
        for line in section_lines:
            if re.search(label_pattern, line, re.IGNORECASE):
                m = _pct_re.search(line)
                if m:
                    return float(m.group(1))
        return None

    session_pct = _extract_labeled_pct(r"current session")
    pct_all     = _extract_labeled_pct(r"all models")
    pct_sonnet  = _extract_labeled_pct(r"sonnet")

    # Fallback: positional parsing when label anchoring fails (e.g. narrow terminal
    # where label and value wrap to separate lines).
    if session_pct is None or pct_all is None or pct_sonnet is None:
        pcts = _pct_re.findall(section)
        if len(pcts) < 3:
            return None
        session_pct = float(pcts[0])
        pct_all     = float(pcts[1])
        pct_sonnet  = float(pcts[2])

    # Capture both the time label AND the timezone string — works for any timezone.
    # Resets: session reset first (short: "2pm"), weekly reset last (long: "Mar 6 at 9pm").
    resets = re.findall(r"Resets\s+(.+?)\s*\(([^)]+)\)", section)
    if len(resets) < 1:
        return None

    session_reset_label = resets[0][0].strip()
    session_reset_tz    = resets[0][1].strip()
    weekly_reset_label  = resets[-1][0].strip()
    weekly_reset_tz     = resets[-1][1].strip()

    return LiveStatus(
        session_pct=session_pct,
        session_reset_label=session_reset_label,
        session_reset_tz=session_reset_tz,
        weekly_pct_all=pct_all,
        weekly_pct_sonnet=pct_sonnet,
        weekly_reset_label=weekly_reset_label,
        weekly_reset_tz=weekly_reset_tz,
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
        ttl = _OUTAGE_RETRY_SECONDS if _cache.outage else _CACHE_TTL_SECONDS
        age = (datetime.now(timezone.utc) - _cache.fetched_at).total_seconds()
        if age < ttl:
            return _cache

    # On cold start (no in-memory cache) try the disk cache first.
    # This makes restarts instant — no need to re-scrape claude immediately.
    if _cache is None:
        disk = _load_cache()
        if disk is not None:
            age = (datetime.now(timezone.utc) - disk.fetched_at).total_seconds()
            _cache = disk          # always populate memory cache from disk
            if not force and age < _CACHE_TTL_SECONDS:
                return disk        # still fresh enough — skip live fetch

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

        # Detect API outage before attempting to parse percentages.
        if _detect_api_error(screen_txt):
            prev = _cache  # may be None or a previous good result
            good = prev if (prev is not None and not prev.outage) else None
            result = LiveStatus(
                session_pct=good.session_pct if good else 0.0,
                session_reset_label=good.session_reset_label if good else "—",
                session_reset_tz=good.session_reset_tz if good else "",
                weekly_pct_all=good.weekly_pct_all if good else 0.0,
                weekly_pct_sonnet=good.weekly_pct_sonnet if good else 0.0,
                weekly_reset_label=good.weekly_reset_label if good else "—",
                weekly_reset_tz=good.weekly_reset_tz if good else "",
                fetched_at=datetime.now(timezone.utc),
                outage=True,
            )
            _cache = result  # in-memory only — _save_cache skips outage states
            child.send(b"\x03")
            try:
                child.expect(pexpect.EOF, timeout=5)
            except Exception:
                pass
            child.close(force=True)
            return result

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
        _save_cache(result)   # persist so next process restart loads instantly

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
        "session_reset_tz": status.session_reset_tz,
        "pct_all": status.weekly_pct_all,
        "pct_sonnet": status.weekly_pct_sonnet,
        "reset_label": status.weekly_reset_label,
        "reset_tz": status.weekly_reset_tz,
    }
