"""Update checker for Penny — checks GitHub Releases for new versions."""

from __future__ import annotations

import json
import re
import urllib.request
from datetime import datetime, timezone
from typing import Any

from . import __version__

GITHUB_API_URL = "https://api.github.com/repos/gpxl/penny/releases/latest"
CHECK_INTERVAL_HOURS = 24


_PRE_RANK = {"a": 0, "b": 1, "rc": 2}


def compare_versions(local: str, remote: str) -> int:
    """Compare two semver / PEP 440 version strings. Returns -1/0/1.

    Handles pre-release tags: a (alpha), b (beta), rc (release candidate).
    Ordering: 0.1.0a1 < 0.1.0b1 < 0.1.0rc1 < 0.1.0 (release).
    Returns -1 if local < remote, 0 if equal, 1 if local > remote.
    """
    def _parse(v: str) -> tuple[tuple[int, ...], tuple[int, int] | None]:
        v = v.strip().lstrip("v")
        m = re.match(r"^(\d+(?:\.\d+)*)(?:(a|b|rc)(\d+))?$", v)
        if not m:
            return (0,), (0, 0)
        nums = tuple(int(x) for x in m.group(1).split("."))
        if m.group(2) is not None:
            pre = (_PRE_RANK.get(m.group(2), 0), int(m.group(3)))
        else:
            pre = None  # release (no pre-release tag)
        return nums, pre

    l_nums, l_pre = _parse(local)
    r_nums, r_pre = _parse(remote)

    if l_nums != r_nums:
        return -1 if l_nums < r_nums else 1

    # Same numeric base — compare pre-release tags.
    # None (release) > any pre-release.
    if l_pre == r_pre:
        return 0
    if l_pre is None:
        return 1   # local is release, remote is pre-release
    if r_pre is None:
        return -1  # local is pre-release, remote is release
    return -1 if l_pre < r_pre else 1


def check_for_update() -> dict[str, Any] | None:
    """Check GitHub API for latest release. Returns info dict or None on error."""
    try:
        req = urllib.request.Request(
            GITHUB_API_URL,
            headers={"Accept": "application/vnd.github.v3+json", "User-Agent": "Penny"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
    except Exception:
        return None

    tag = data.get("tag_name", "").lstrip("v")
    if not tag:
        return None

    current = __version__
    cmp = compare_versions(current, tag)

    return {
        "update_available": cmp < 0,
        "latest_version": tag,
        "current_version": current,
        "release_url": data.get("html_url", ""),
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }


def should_check(state: dict[str, Any]) -> bool:
    """True if the last update check was >24 hours ago or never performed."""
    uc = state.get("update_check", {})
    checked_at = uc.get("checked_at")
    if not checked_at:
        return True
    try:
        last = datetime.fromisoformat(checked_at)
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return True
    elapsed = (datetime.now(timezone.utc) - last).total_seconds() / 3600
    return elapsed >= CHECK_INTERVAL_HOURS


def update_state_with_check(state: dict[str, Any]) -> dict[str, Any]:
    """Run an update check and store the result in state."""
    result = check_for_update()
    if result is not None:
        existing = state.get("update_check", {})
        # Preserve dismissed_version and notified_version across checks
        result["notified_version"] = existing.get("notified_version", "")
        result["dismissed_version"] = existing.get("dismissed_version", "")
        state["update_check"] = result
    return state


def should_notify(state: dict[str, Any]) -> bool:
    """True if update available and user hasn't been notified for this version."""
    uc = state.get("update_check", {})
    if not uc.get("update_available"):
        return False
    return uc.get("notified_version") != uc.get("latest_version")


def mark_notified(state: dict[str, Any], version: str) -> None:
    """Record that the user was notified about this version."""
    uc = state.setdefault("update_check", {})
    uc["notified_version"] = version


def is_dismissed(state: dict[str, Any], version: str) -> bool:
    """True if the user dismissed the banner for this specific version."""
    uc = state.get("update_check", {})
    return uc.get("dismissed_version") == version and version != ""


def dismiss_version(state: dict[str, Any], version: str) -> None:
    """Record that the user dismissed the update banner for this version."""
    uc = state.setdefault("update_check", {})
    uc["dismissed_version"] = version
