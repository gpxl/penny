"""Fetch accurate session/weekly usage by running claude interactively
and parsing the /status Usage tab output.

Uses pexpect to drive a claude subprocess in a pseudo-terminal, sends
/status, navigates to the Usage tab (the dialog has four tabs:
Settings | Status | Config | Usage), captures the terminal output via
the pyte terminal emulator (which tracks screen state cleanly, avoiding
mangled text from interleaved cursor-positioning sequences), and extracts
the three percentage values and reset times with regex.

Results are cached for 30 minutes.

Requirements: pexpect>=4.8, pyte>=0.8  (auto-installed by penny.deps)
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

import pexpect  # type: ignore[import]
import pyte  # type: ignore[import]

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
        or "API Error:" in screen_txt
    )


def _make_outage_status() -> LiveStatus:
    """Build a LiveStatus(outage=True), preserving last-good cached values when available."""
    global _cache
    prev = _cache
    good = prev if (prev is not None and not prev.outage) else None
    result = LiveStatus(
        session_pct=good.session_pct if good else 0.0,
        session_reset_label=good.session_reset_label if good else "\u2014",
        session_reset_tz=good.session_reset_tz if good else "",
        weekly_pct_all=good.weekly_pct_all if good else 0.0,
        weekly_pct_sonnet=good.weekly_pct_sonnet if good else 0.0,
        weekly_reset_label=good.weekly_reset_label if good else "\u2014",
        weekly_reset_tz=good.weekly_reset_tz if good else "",
        weekly_reset_label_sonnet=good.weekly_reset_label_sonnet if good else "\u2014",
        weekly_reset_tz_sonnet=good.weekly_reset_tz_sonnet if good else "",
        fetched_at=datetime.now(timezone.utc),
        outage=True,
    )
    _cache = result  # in-memory only — _save_cache skips outage states
    return result


def _stale_or_default() -> LiveStatus:
    """Return last-good cached data or a zeroed default.

    Used for transient failures (timeouts, parse glitches) that are NOT
    confirmed API outages.  Does NOT update _cache so the next fetch cycle
    retries immediately.
    """
    if _cache is not None and not _cache.outage:
        return _cache
    return LiveStatus(
        session_pct=0.0,
        session_reset_label="\u2014",
        session_reset_tz="",
        weekly_pct_all=0.0,
        weekly_pct_sonnet=0.0,
        weekly_reset_label="\u2014",
        weekly_reset_tz="",
        weekly_reset_label_sonnet="\u2014",
        weekly_reset_tz_sonnet="",
        fetched_at=datetime.now(timezone.utc),
    )


def _screen_text(screen: Any) -> str:
    """Extract clean text from a pyte Screen."""
    return "\n".join(row.rstrip() for row in screen.display)


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
            "weekly_reset_label_sonnet": status.weekly_reset_label_sonnet,
            "weekly_reset_tz_sonnet": status.weekly_reset_tz_sonnet,
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
            weekly_reset_label_sonnet=d.get("weekly_reset_label_sonnet", d["weekly_reset_label"]),
            weekly_reset_tz_sonnet=d.get("weekly_reset_tz_sonnet", d["weekly_reset_tz"]),
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
    session_pct: float              # "16% used"
    session_reset_label: str        # "2pm" (extracted from "Resets 2pm (Europe/Amsterdam)")
    session_reset_tz: str           # "Europe/Amsterdam" (timezone shown in /status)
    weekly_pct_all: float           # "30% used"
    weekly_pct_sonnet: float        # "41% used"
    weekly_reset_label: str         # "Mar 28 at 9:59am" (all-models reset)
    weekly_reset_tz: str            # "Europe/Amsterdam"
    fetched_at: datetime
    outage: bool = False            # True when /status returned an API error instead of data
    weekly_reset_label_sonnet: str = ""   # "Mar 24 at 8pm" (Sonnet-specific reset)
    weekly_reset_tz_sonnet: str = ""      # "Europe/Amsterdam"


def _parse_usage_screen(screen_txt: str) -> LiveStatus | None:
    """
    Parse percentages and reset labels from the pyte-rendered Usage tab text.

    Labels on screen (from /status Usage tab):
      "Current session"              → N% used  →  Resets TIME (timezone)
      "Current week (all models)"    → N% used  →  Resets DATE at TIME (timezone)
      "Current week (Sonnet only)"   → N% used  →  Resets DATE at TIME (timezone) [independent]

    We extract percentages by anchoring to label text to avoid misassignment when the
    pyte screen contains residual content from a prior tab or partial render.
    The label and "N% used" may be on the same line or on adjacent lines (the
    progress bar + percentage often render on the line below the section header).

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
        for i, line in enumerate(section_lines):
            if re.search(label_pattern, line, re.IGNORECASE):
                # Check same line first
                m = _pct_re.search(line)
                if m:
                    return float(m.group(1))
                # Label and percentage may be on separate lines (label on one
                # line, progress bar + "N% used" on the next).  Check the next
                # few lines.
                for j in range(1, 4):
                    if i + j < len(section_lines):
                        m = _pct_re.search(section_lines[i + j])
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

    # Label-anchored reset extraction: walk section lines and assign each
    # "Resets ..." line to its preceding section label.  This handles both
    # the old format (single shared weekly reset) and the new format (separate
    # resets for all-models and Sonnet).
    _reset_re = re.compile(r"Resets\s+(.+?)\s*\(([^)]+)\)")

    def _extract_labeled_reset(label_pattern: str) -> tuple[str, str] | None:
        for i, line in enumerate(section_lines):
            if re.search(label_pattern, line, re.IGNORECASE):
                # Check subsequent lines for a Resets line.
                # Don't stop at "Sonnet only" since in the shared-reset format
                # the Resets line follows both "all models" and "Sonnet only".
                for j in range(1, 6):
                    if i + j < len(section_lines):
                        m = _reset_re.search(section_lines[i + j])
                        if m:
                            return m.group(1).strip(), m.group(2).strip()
                        # Stop at a different major section (session), not at
                        # peer labels (Sonnet/all-models share a section)
                        if re.search(r"current session", section_lines[i + j], re.IGNORECASE):
                            break
        return None

    session_reset = _extract_labeled_reset(r"current session")
    all_models_reset = _extract_labeled_reset(r"all models")
    sonnet_reset = _extract_labeled_reset(r"sonnet")

    # Fallback: positional parsing for old/minimal formats
    if session_reset is None or (all_models_reset is None and sonnet_reset is None):
        resets = _reset_re.findall(section)
        if len(resets) < 1:
            return None
        if session_reset is None:
            session_reset = (resets[0][0].strip(), resets[0][1].strip())
        if all_models_reset is None:
            all_models_reset = (resets[-1][0].strip(), resets[-1][1].strip())

    # If Sonnet has no independent reset, use the all-models reset
    if sonnet_reset is None:
        sonnet_reset = all_models_reset

    return LiveStatus(
        session_pct=session_pct,
        session_reset_label=session_reset[0],
        session_reset_tz=session_reset[1],
        weekly_pct_all=pct_all,
        weekly_pct_sonnet=pct_sonnet,
        weekly_reset_label=all_models_reset[0],
        weekly_reset_tz=all_models_reset[1],
        weekly_reset_label_sonnet=sonnet_reset[0],
        weekly_reset_tz_sonnet=sonnet_reset[1],
        fetched_at=datetime.now(timezone.utc),
    )


def _safe_feed(stream: Any, data: Any) -> None:
    """Feed data to a pyte stream, ignoring non-bytes values (e.g. pexpect.TIMEOUT class)."""
    if isinstance(data, bytes) and data:
        stream.feed(data)


def _feed_child(child: Any, stream: Any, secs: float) -> None:
    """Read from child for up to secs seconds and feed bytes to the pyte stream."""
    deadline = time.time() + secs
    while time.time() < deadline:
        try:
            child.expect(pexpect.TIMEOUT, timeout=0.1)
            if child.before:
                stream.feed(child.before)
        except Exception:
            break


def fetch_live_status(force: bool = False) -> LiveStatus:
    """
    Run claude interactively, navigate /status → Usage tab, parse and return the result.

    Always returns a LiveStatus — either real data or LiveStatus(outage=True)
    when the fetch fails for any reason (API errors, missing claude binary,
    unparseable screens).  Never returns None.

    Caches results for 5 minutes (2 minutes during outage) to avoid spawning
    claude on every refresh.

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
        return _stale_or_default()

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
        _safe_feed(pyte_stream, child.before)
        _safe_feed(pyte_stream, child.after)
        if idx != 0:   # TIMEOUT or EOF — claude didn't start cleanly
            txt = _screen_text(screen)
            child.close(force=True)
            if _detect_api_error(txt):
                return _make_outage_status()
            return _stale_or_default()

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
        # Claude's TUI renders dialog content via ANSI cursor positioning, so
        # pexpect.expect can't reliably match text like "Esc to cancel" in the
        # raw byte stream.  Instead, wait a fixed time and check the pyte screen.
        _feed_child(child, pyte_stream, 4.0)
        screen_check = _screen_text(screen)
        if "Esc to cancel" not in screen_check and "to cycle" not in screen_check:
            # Dialog didn't open — try expect as fallback
            idx2 = child.expect(
                [b"to cycle", b"Esc to cancel", pexpect.TIMEOUT, pexpect.EOF],
                timeout=10,
            )
            _safe_feed(pyte_stream, child.before)
            _safe_feed(pyte_stream, child.after)
            if idx2 >= 2:
                txt = _screen_text(screen)
                child.close(force=True)
                if _detect_api_error(txt):
                    return _make_outage_status()
                return _stale_or_default()

        # Phase 4: navigate to the Usage tab.
        # Tab order: Settings(1) | Status(2) | Config(3) | Usage(4)
        # Claude opens on the Status tab; Right Arrow × 2 reaches Usage.
        child.send(b"\x1b[C")   # Right Arrow → Config tab
        _feed_child(child, pyte_stream, 0.8)
        child.send(b"\x1b[C")   # Right Arrow → Usage tab
        _feed_child(child, pyte_stream, 2.5)

        # Phase 5: extract clean screen text from pyte.
        screen_txt = _screen_text(screen)

        # Detect API outage before attempting to parse percentages.
        if _detect_api_error(screen_txt):
            child.send(b"\x03")
            try:
                child.expect(pexpect.EOF, timeout=5)
            except Exception:
                pass
            child.close(force=True)
            return _make_outage_status()

        result = _parse_usage_screen(screen_txt)

        # Reached the Usage tab but couldn't parse any data — transient glitch
        # (format change, garbled screen, etc.).  Not necessarily an API outage.
        if result is None:
            child.send(b"\x03")
            try:
                child.expect(pexpect.EOF, timeout=5)
            except Exception:
                pass
            child.close(force=True)
            return _stale_or_default()

        # Phase 6: close gracefully.
        child.send(b"\x03")   # Ctrl-C
        try:
            child.expect(pexpect.EOF, timeout=5)
            _safe_feed(pyte_stream, child.before)
        except Exception:
            pass
        child.close(force=True)

    except Exception:
        try:
            txt = _screen_text(screen)
            if _detect_api_error(txt):
                return _make_outage_status()
        except Exception:
            pass
        return _stale_or_default()

    if result:
        _cache = result
        _save_cache(result)   # persist so next process restart loads instantly

    return result


def get_cached_status() -> LiveStatus | None:
    """Return the current in-memory cache of the last successful /status fetch."""
    return _cache


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
        "reset_label_sonnet": status.weekly_reset_label_sonnet,
        "reset_tz_sonnet": status.weekly_reset_tz_sonnet,
    }
