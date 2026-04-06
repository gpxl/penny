"""Unit tests for penny/update_checker.py."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch


class TestCompareVersions:
    """Tests for compare_versions()."""

    def test_equal_versions(self):
        from penny.update_checker import compare_versions

        assert compare_versions("0.1.0", "0.1.0") == 0

    def test_equal_alpha_versions(self):
        from penny.update_checker import compare_versions

        assert compare_versions("0.1.0a1", "0.1.0a1") == 0

    def test_local_older(self):
        from penny.update_checker import compare_versions

        assert compare_versions("0.1.0", "0.2.0") == -1

    def test_local_newer(self):
        from penny.update_checker import compare_versions

        assert compare_versions("0.2.0", "0.1.0") == 1

    def test_alpha_before_release(self):
        from penny.update_checker import compare_versions

        assert compare_versions("0.1.0a1", "0.1.0") == -1

    def test_release_after_alpha(self):
        from penny.update_checker import compare_versions

        assert compare_versions("0.1.0", "0.1.0a1") == 1

    def test_alpha_ordering(self):
        from penny.update_checker import compare_versions

        assert compare_versions("0.1.0a1", "0.1.0a2") == -1
        assert compare_versions("0.1.0a3", "0.1.0a2") == 1

    def test_different_minor_with_alpha(self):
        from penny.update_checker import compare_versions

        assert compare_versions("0.1.0a2", "0.2.0a1") == -1

    def test_beta_versions(self):
        from penny.update_checker import compare_versions

        assert compare_versions("0.1.0b1", "0.1.0b1") == 0
        assert compare_versions("0.1.0b1", "0.1.0b2") == -1
        assert compare_versions("0.1.0b3", "0.1.0b2") == 1

    def test_alpha_before_beta(self):
        from penny.update_checker import compare_versions

        assert compare_versions("0.1.0a1", "0.1.0b1") == -1
        assert compare_versions("0.1.0b1", "0.1.0a1") == 1

    def test_beta_before_rc(self):
        from penny.update_checker import compare_versions

        assert compare_versions("0.1.0b1", "0.1.0rc1") == -1
        assert compare_versions("0.1.0rc1", "0.1.0b1") == 1

    def test_rc_before_release(self):
        from penny.update_checker import compare_versions

        assert compare_versions("0.1.0rc1", "0.1.0") == -1
        assert compare_versions("0.1.0", "0.1.0rc1") == 1

    def test_full_prerelease_chain(self):
        from penny.update_checker import compare_versions

        # a < b < rc < release
        assert compare_versions("0.1.0a1", "0.1.0b1") == -1
        assert compare_versions("0.1.0b1", "0.1.0rc1") == -1
        assert compare_versions("0.1.0rc1", "0.1.0") == -1

    def test_v_prefix_stripped(self):
        from penny.update_checker import compare_versions

        assert compare_versions("v0.1.0", "0.1.0") == 0

    def test_malformed_input(self):
        from penny.update_checker import compare_versions

        # Malformed returns (0,), 0 for both → equal
        assert compare_versions("garbage", "nonsense") == 0

    def test_malformed_vs_valid(self):
        from penny.update_checker import compare_versions

        # Malformed → (0,), 0; valid "0.1.0" → (0, 1, 0), None
        # (0,) < (0, 1, 0) → -1
        assert compare_versions("garbage", "0.1.0") == -1


class TestCheckForUpdate:
    """Tests for check_for_update() with mocked HTTP."""

    def _make_github_response(self, tag: str, url: str = "https://github.com/gpxl/penny/releases/tag/v0.2.0"):
        body = json.dumps({"tag_name": tag, "html_url": url}).encode()
        resp = MagicMock()
        resp.read.return_value = body
        resp.__enter__ = MagicMock(return_value=resp)
        resp.__exit__ = MagicMock(return_value=False)
        return resp

    @patch("penny.update_checker.__version__", "0.1.0a1")
    @patch("penny.update_checker.urllib.request.urlopen")
    def test_update_available(self, mock_urlopen):
        from penny.update_checker import check_for_update

        mock_urlopen.return_value = self._make_github_response("v0.2.0")
        result = check_for_update()

        assert result is not None
        assert result["update_available"] is True
        assert result["latest_version"] == "0.2.0"
        assert result["current_version"] == "0.1.0a1"
        assert "checked_at" in result

    @patch("penny.update_checker.__version__", "0.2.0")
    @patch("penny.update_checker.urllib.request.urlopen")
    def test_no_update(self, mock_urlopen):
        from penny.update_checker import check_for_update

        mock_urlopen.return_value = self._make_github_response("v0.2.0")
        result = check_for_update()

        assert result is not None
        assert result["update_available"] is False

    @patch("penny.update_checker.urllib.request.urlopen")
    def test_network_error_returns_none(self, mock_urlopen):
        from penny.update_checker import check_for_update

        mock_urlopen.side_effect = OSError("Network error")
        result = check_for_update()

        assert result is None

    @patch("penny.update_checker.urllib.request.urlopen")
    def test_empty_tag_returns_none(self, mock_urlopen):
        from penny.update_checker import check_for_update

        body = json.dumps({"tag_name": "", "html_url": ""}).encode()
        resp = MagicMock()
        resp.read.return_value = body
        resp.__enter__ = MagicMock(return_value=resp)
        resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = resp

        result = check_for_update()
        assert result is None


class TestShouldCheck:
    """Tests for should_check()."""

    def test_no_previous_check(self):
        from penny.update_checker import should_check

        assert should_check({}) is True

    def test_recent_check(self):
        from penny.update_checker import should_check

        now = datetime.now(timezone.utc).isoformat()
        state = {"update_check": {"checked_at": now}}
        assert should_check(state) is False

    def test_old_check(self):
        from penny.update_checker import should_check

        old = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
        state = {"update_check": {"checked_at": old}}
        assert should_check(state) is True

    def test_invalid_timestamp(self):
        from penny.update_checker import should_check

        state = {"update_check": {"checked_at": "not-a-date"}}
        assert should_check(state) is True

    def test_timestamp_without_timezone(self):
        from penny.update_checker import should_check

        # Timestamp without tzinfo should be treated as UTC
        now_no_tz = datetime.now().isoformat()
        state = {"update_check": {"checked_at": now_no_tz}}
        assert should_check(state) is False


class TestShouldNotify:
    """Tests for should_notify() and mark_notified()."""

    def test_no_update(self):
        from penny.update_checker import should_notify

        state = {"update_check": {"update_available": False}}
        assert should_notify(state) is False

    def test_update_not_yet_notified(self):
        from penny.update_checker import should_notify

        state = {"update_check": {
            "update_available": True,
            "latest_version": "0.2.0",
            "notified_version": "",
        }}
        assert should_notify(state) is True

    def test_already_notified(self):
        from penny.update_checker import should_notify

        state = {"update_check": {
            "update_available": True,
            "latest_version": "0.2.0",
            "notified_version": "0.2.0",
        }}
        assert should_notify(state) is False

    def test_mark_notified(self):
        from penny.update_checker import mark_notified, should_notify

        state = {"update_check": {
            "update_available": True,
            "latest_version": "0.2.0",
            "notified_version": "",
        }}
        assert should_notify(state) is True
        mark_notified(state, "0.2.0")
        assert should_notify(state) is False


class TestDismiss:
    """Tests for is_dismissed() and dismiss_version()."""

    def test_not_dismissed(self):
        from penny.update_checker import is_dismissed

        state = {"update_check": {}}
        assert is_dismissed(state, "0.2.0") is False

    def test_dismissed(self):
        from penny.update_checker import dismiss_version, is_dismissed

        state = {"update_check": {}}
        dismiss_version(state, "0.2.0")
        assert is_dismissed(state, "0.2.0") is True

    def test_different_version_not_dismissed(self):
        from penny.update_checker import dismiss_version, is_dismissed

        state = {"update_check": {}}
        dismiss_version(state, "0.2.0")
        assert is_dismissed(state, "0.3.0") is False

    def test_empty_version_not_dismissed(self):
        from penny.update_checker import is_dismissed

        state = {"update_check": {"dismissed_version": ""}}
        assert is_dismissed(state, "") is False


class TestRevalidateUpdateFlag:
    """Tests for revalidate_update_flag() — clears stale update_available."""

    @patch("penny.update_checker.__version__", "0.6.0b1")
    def test_clears_flag_when_current_matches_latest(self):
        from penny.update_checker import revalidate_update_flag

        state = {"update_check": {
            "update_available": True,
            "latest_version": "0.6.0b1",
        }}
        revalidate_update_flag(state)
        assert state["update_check"]["update_available"] is False

    @patch("penny.update_checker.__version__", "0.7.0")
    def test_clears_flag_when_current_newer_than_latest(self):
        from penny.update_checker import revalidate_update_flag

        state = {"update_check": {
            "update_available": True,
            "latest_version": "0.6.0b1",
        }}
        revalidate_update_flag(state)
        assert state["update_check"]["update_available"] is False

    @patch("penny.update_checker.__version__", "0.5.0")
    def test_preserves_flag_when_update_genuinely_available(self):
        from penny.update_checker import revalidate_update_flag

        state = {"update_check": {
            "update_available": True,
            "latest_version": "0.6.0b1",
        }}
        revalidate_update_flag(state)
        assert state["update_check"]["update_available"] is True

    def test_noop_when_no_update_check(self):
        from penny.update_checker import revalidate_update_flag

        state = {}
        revalidate_update_flag(state)
        assert "update_check" not in state

    def test_noop_when_flag_already_false(self):
        from penny.update_checker import revalidate_update_flag

        state = {"update_check": {
            "update_available": False,
            "latest_version": "0.6.0b1",
        }}
        revalidate_update_flag(state)
        assert state["update_check"]["update_available"] is False


class TestUpdateStateWithCheck:
    """Tests for update_state_with_check()."""

    @patch("penny.update_checker.check_for_update")
    def test_stores_result(self, mock_check):
        from penny.update_checker import update_state_with_check

        mock_check.return_value = {
            "update_available": True,
            "latest_version": "0.2.0",
            "current_version": "0.1.0a1",
            "release_url": "https://example.com",
            "checked_at": "2026-01-01T00:00:00+00:00",
        }
        state = {}
        state = update_state_with_check(state)
        assert state["update_check"]["update_available"] is True
        assert state["update_check"]["latest_version"] == "0.2.0"

    @patch("penny.update_checker.check_for_update")
    def test_preserves_dismissed_version(self, mock_check):
        from penny.update_checker import update_state_with_check

        mock_check.return_value = {
            "update_available": True,
            "latest_version": "0.3.0",
            "current_version": "0.1.0a1",
            "release_url": "",
            "checked_at": "2026-01-01T00:00:00+00:00",
        }
        state = {"update_check": {"dismissed_version": "0.2.0", "notified_version": "0.2.0"}}
        state = update_state_with_check(state)
        assert state["update_check"]["dismissed_version"] == "0.2.0"
        assert state["update_check"]["notified_version"] == "0.2.0"

    @patch("penny.update_checker.check_for_update")
    def test_network_error_preserves_state(self, mock_check):
        from penny.update_checker import update_state_with_check

        mock_check.return_value = None
        state = {"update_check": {"latest_version": "0.1.0"}}
        state = update_state_with_check(state)
        # State unchanged when check fails
        assert state["update_check"]["latest_version"] == "0.1.0"
