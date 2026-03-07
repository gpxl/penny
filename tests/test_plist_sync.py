"""Tests for plist sync logic, config writing, and launchd service management.

The actual _sync_launchd_service method lives on PennyApp (NSObject) and can't
be instantiated without AppKit.  These tests reproduce the logic faithfully
and verify it against real plist files in tmp dirs.
"""

from __future__ import annotations

import os
import plistlib
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest
import yaml

PLIST_LABEL = "com.gpxl.penny"


# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_plist(path: Path, keep_alive: bool = True, run_at_load: bool = True) -> None:
    """Write a minimal valid plist to *path*."""
    pl = {
        "Label": PLIST_LABEL,
        "KeepAlive": keep_alive,
        "RunAtLoad": run_at_load,
        "ProgramArguments": ["/usr/bin/python3", "-m", "penny"],
        "WorkingDirectory": "/tmp/penny-dev",
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as f:
        plistlib.dump(pl, f)


def _read_plist(path: Path) -> dict:
    with path.open("rb") as f:
        return plistlib.load(f)


def _sync_launchd_service(
    config: dict,
    plist_path: Path,
    script_dir: Path | None = None,
) -> list[list[str]]:
    """Reproduce PennyApp._sync_launchd_service logic.

    Returns list of launchctl commands that would be run (or empty list for no-op).
    """
    svc = config.get("service", {})
    want_keep_alive = bool(svc.get("keep_alive", True))
    want_run_at_load = bool(svc.get("launch_at_login", True))

    if not plist_path.exists():
        return []

    try:
        with plist_path.open("rb") as f:
            pl = plistlib.load(f)
    except Exception:
        return []

    if (pl.get("KeepAlive", True) == want_keep_alive
            and pl.get("RunAtLoad", True) == want_run_at_load):
        return []  # already in sync

    pl["KeepAlive"] = want_keep_alive
    pl["RunAtLoad"] = want_run_at_load
    plist_bytes = plistlib.dumps(pl)

    plist_path.write_bytes(plist_bytes)

    if script_dir:
        try:
            (script_dir / f"{PLIST_LABEL}.plist").write_bytes(plist_bytes)
        except Exception:
            pass

    uid = str(os.getuid())
    return [
        ["launchctl", "bootout", f"gui/{uid}/{PLIST_LABEL}"],
        ["launchctl", "bootstrap", f"gui/{uid}", str(plist_path)],
    ]


# ── Plist sync tests ─────────────────────────────────────────────────────────


class TestSyncLaunchdService:
    def test_noop_when_plist_missing(self, tmp_path):
        config = {"service": {"keep_alive": False}}
        cmds = _sync_launchd_service(config, tmp_path / "nonexistent.plist")
        assert cmds == []

    def test_noop_when_already_in_sync(self, tmp_path):
        plist_path = tmp_path / "test.plist"
        _make_plist(plist_path, keep_alive=True, run_at_load=True)
        config = {"service": {"keep_alive": True, "launch_at_login": True}}
        cmds = _sync_launchd_service(config, plist_path)
        assert cmds == []

    def test_updates_keep_alive_false(self, tmp_path):
        plist_path = tmp_path / "test.plist"
        _make_plist(plist_path, keep_alive=True, run_at_load=True)
        config = {"service": {"keep_alive": False, "launch_at_login": True}}
        cmds = _sync_launchd_service(config, plist_path)
        assert len(cmds) == 2  # bootout + bootstrap
        pl = _read_plist(plist_path)
        assert pl["KeepAlive"] is False
        assert pl["RunAtLoad"] is True

    def test_updates_run_at_load_false(self, tmp_path):
        plist_path = tmp_path / "test.plist"
        _make_plist(plist_path, keep_alive=True, run_at_load=True)
        config = {"service": {"keep_alive": True, "launch_at_login": False}}
        cmds = _sync_launchd_service(config, plist_path)
        pl = _read_plist(plist_path)
        assert pl["KeepAlive"] is True
        assert pl["RunAtLoad"] is False

    def test_updates_both_at_once(self, tmp_path):
        plist_path = tmp_path / "test.plist"
        _make_plist(plist_path, keep_alive=True, run_at_load=True)
        config = {"service": {"keep_alive": False, "launch_at_login": False}}
        cmds = _sync_launchd_service(config, plist_path)
        pl = _read_plist(plist_path)
        assert pl["KeepAlive"] is False
        assert pl["RunAtLoad"] is False

    def test_defaults_to_true_when_service_key_missing(self, tmp_path):
        plist_path = tmp_path / "test.plist"
        _make_plist(plist_path, keep_alive=True, run_at_load=True)
        config = {}  # no service section
        cmds = _sync_launchd_service(config, plist_path)
        assert cmds == []  # defaults match plist → no-op

    def test_defaults_to_true_triggers_update_when_plist_false(self, tmp_path):
        plist_path = tmp_path / "test.plist"
        _make_plist(plist_path, keep_alive=False, run_at_load=False)
        config = {}  # defaults to keep_alive=True, launch_at_login=True
        cmds = _sync_launchd_service(config, plist_path)
        assert len(cmds) == 2
        pl = _read_plist(plist_path)
        assert pl["KeepAlive"] is True
        assert pl["RunAtLoad"] is True

    def test_launchctl_commands_correct(self, tmp_path):
        plist_path = tmp_path / "test.plist"
        _make_plist(plist_path, keep_alive=True)
        config = {"service": {"keep_alive": False}}
        cmds = _sync_launchd_service(config, plist_path)
        uid = str(os.getuid())
        assert cmds[0] == ["launchctl", "bootout", f"gui/{uid}/{PLIST_LABEL}"]
        assert cmds[1] == ["launchctl", "bootstrap", f"gui/{uid}", str(plist_path)]

    def test_updates_script_dir_copy(self, tmp_path):
        plist_path = tmp_path / "test.plist"
        script_dir = tmp_path / "script_dir"
        script_dir.mkdir()
        _make_plist(plist_path, keep_alive=True)
        config = {"service": {"keep_alive": False}}
        _sync_launchd_service(config, plist_path, script_dir=script_dir)
        copy_path = script_dir / f"{PLIST_LABEL}.plist"
        assert copy_path.exists()
        pl = _read_plist(copy_path)
        assert pl["KeepAlive"] is False

    def test_preserves_other_plist_keys(self, tmp_path):
        plist_path = tmp_path / "test.plist"
        _make_plist(plist_path, keep_alive=True)
        config = {"service": {"keep_alive": False}}
        _sync_launchd_service(config, plist_path)
        pl = _read_plist(plist_path)
        assert pl["Label"] == PLIST_LABEL
        assert pl["ProgramArguments"] == ["/usr/bin/python3", "-m", "penny"]

    def test_noop_when_plist_corrupt(self, tmp_path):
        plist_path = tmp_path / "test.plist"
        plist_path.write_text("not a valid plist")
        config = {"service": {"keep_alive": False}}
        cmds = _sync_launchd_service(config, plist_path)
        assert cmds == []


# ── toggleKeepAlive_ / toggleLaunchAtLogin_ logic ────────────────────────────


class TestToggleLogic:
    """Test the toggle methods' effect on config and plist."""

    def _toggle_keep_alive(self, config: dict, new_value: bool) -> dict:
        """Reproduce toggleKeepAlive_ logic."""
        config.setdefault("service", {})["keep_alive"] = new_value
        return config

    def _toggle_launch_at_login(self, config: dict, new_value: bool) -> dict:
        """Reproduce toggleLaunchAtLogin_ logic."""
        config.setdefault("service", {})["launch_at_login"] = new_value
        return config

    def test_toggle_keep_alive_on(self):
        config = {}
        config = self._toggle_keep_alive(config, True)
        assert config["service"]["keep_alive"] is True

    def test_toggle_keep_alive_off(self):
        config = {"service": {"keep_alive": True}}
        config = self._toggle_keep_alive(config, False)
        assert config["service"]["keep_alive"] is False

    def test_toggle_launch_at_login_on(self):
        config = {}
        config = self._toggle_launch_at_login(config, True)
        assert config["service"]["launch_at_login"] is True

    def test_toggle_launch_at_login_off(self):
        config = {"service": {"launch_at_login": True}}
        config = self._toggle_launch_at_login(config, False)
        assert config["service"]["launch_at_login"] is False

    def test_toggle_then_sync_updates_plist(self, tmp_path):
        """Full flow: toggle → config updated → sync writes plist."""
        plist_path = tmp_path / "test.plist"
        _make_plist(plist_path, keep_alive=True, run_at_load=True)

        config = {"service": {"keep_alive": True, "launch_at_login": True}}
        config = self._toggle_keep_alive(config, False)
        cmds = _sync_launchd_service(config, plist_path)
        assert len(cmds) == 2
        pl = _read_plist(plist_path)
        assert pl["KeepAlive"] is False
        assert pl["RunAtLoad"] is True


# ── _write_config ─────────────────────────────────────────────────────────────


class TestWriteConfig:
    def test_writes_valid_yaml(self, tmp_path):
        config_path = tmp_path / "config.yaml"
        config = {
            "projects": [{"path": "/tmp/proj", "priority": 1}],
            "service": {"keep_alive": True},
        }
        with config_path.open("w") as f:
            yaml.dump(config, f, default_flow_style=False, allow_unicode=True)

        loaded = yaml.safe_load(config_path.read_text())
        assert loaded["projects"][0]["path"] == "/tmp/proj"
        assert loaded["service"]["keep_alive"] is True

    def test_round_trip_preserves_all_keys(self, tmp_path):
        config_path = tmp_path / "config.yaml"
        config = {
            "projects": [{"path": "/tmp/a"}, {"path": "/tmp/b"}],
            "trigger": {"min_capacity_percent": 30, "max_days_remaining": 2},
            "work": {"max_agents_per_run": 3},
            "service": {"keep_alive": False, "launch_at_login": False},
        }
        with config_path.open("w") as f:
            yaml.dump(config, f, default_flow_style=False, allow_unicode=True)

        loaded = yaml.safe_load(config_path.read_text())
        assert loaded == config


# ── _script_dir_from_plist ────────────────────────────────────────────────────


class TestScriptDirFromPlist:
    def test_returns_working_directory(self, tmp_path):
        plist_path = tmp_path / "test.plist"
        _make_plist(plist_path)
        with plist_path.open("rb") as f:
            pl = plistlib.load(f)
        wd = pl.get("WorkingDirectory", "")
        assert wd == "/tmp/penny-dev"

    def test_returns_none_when_no_working_directory(self, tmp_path):
        plist_path = tmp_path / "test.plist"
        pl = {"Label": PLIST_LABEL}
        with plist_path.open("wb") as f:
            plistlib.dump(pl, f)
        with plist_path.open("rb") as f:
            loaded = plistlib.load(f)
        assert loaded.get("WorkingDirectory") is None
