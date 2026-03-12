"""Unit tests for penny/onboarding.py.

All tests avoid requiring AppKit — NSAlert calls are either not reached
(logic-only paths) or patched out.
"""

from __future__ import annotations

import textwrap
from unittest.mock import MagicMock, patch

import yaml

from penny.onboarding import (
    _write_config_with_comments,
    check_full_permissions_consent,
    needs_onboarding,
)

# ── needs_onboarding ─────────────────────────────────────────────────────────


class TestNeedsOnboarding:
    def test_no_projects_returns_true(self):
        assert needs_onboarding({}) is True

    def test_empty_projects_list_returns_true(self):
        assert needs_onboarding({"projects": []}) is True

    def test_placeholder_project_returns_true(self):
        config = {"projects": [{"path": "/some/PLACEHOLDER/path"}]}
        assert needs_onboarding(config) is True

    def test_real_project_returns_false(self):
        config = {"projects": [{"path": "/Users/alice/myproject"}]}
        assert needs_onboarding(config) is False

    def test_multiple_projects_all_real_returns_false(self):
        config = {
            "projects": [
                {"path": "/Users/alice/proj1"},
                {"path": "/Users/alice/proj2"},
            ]
        }
        assert needs_onboarding(config) is False

    def test_mix_of_placeholder_and_real_returns_true(self):
        config = {
            "projects": [
                {"path": "/Users/alice/real"},
                {"path": "PLACEHOLDER"},
            ]
        }
        assert needs_onboarding(config) is True


# ── check_full_permissions_consent ───────────────────────────────────────────


class TestCheckFullPermissionsConsent:
    def test_non_full_mode_returns_true_immediately(self):
        config = {"work": {"agent_permissions": "off"}}
        assert check_full_permissions_consent(config, {}) is True

    def test_non_full_mode_scoped_returns_true(self):
        config = {"work": {"agent_permissions": "scoped"}}
        assert check_full_permissions_consent(config, {}) is True

    def test_full_mode_with_prior_consent_returns_true(self):
        config = {"work": {"agent_permissions": "full"}}
        state = {"agent_permissions_consent": {"mode": "full", "given": True}}
        assert check_full_permissions_consent(config, state) is True

    def test_full_mode_no_appkit_returns_true(self):
        config = {"work": {"agent_permissions": "full"}}
        with patch("penny.onboarding._HAS_APPKIT", False):
            assert check_full_permissions_consent(config, {}) is True

    def test_full_mode_user_accepts_records_consent(self):
        config = {"work": {"agent_permissions": "full"}}
        state = {}
        mock_alert = MagicMock()
        mock_alert.runModal.return_value = 1000

        with patch("penny.onboarding._HAS_APPKIT", True), \
             patch("penny.onboarding._bring_to_front"), \
             patch("penny.onboarding.NSAlertFirstButtonReturn", 1000), \
             patch("penny.onboarding.NSAlert") as mock_cls:
            mock_cls.alloc.return_value.init.return_value = mock_alert
            result = check_full_permissions_consent(config, state)

        assert result is True
        assert state["agent_permissions_consent"]["given"] is True
        assert state["agent_permissions_consent"]["mode"] == "full"

    def test_full_mode_user_declines_returns_false(self):
        config = {"work": {"agent_permissions": "full"}}
        state = {}
        mock_alert = MagicMock()
        mock_alert.runModal.return_value = 1001  # NSAlertSecondButtonReturn

        with patch("penny.onboarding._HAS_APPKIT", True), \
             patch("penny.onboarding._bring_to_front"), \
             patch("penny.onboarding.NSAlert") as mock_cls:
            mock_cls.alloc.return_value.init.return_value = mock_alert
            result = check_full_permissions_consent(config, state)

        assert result is False
        assert "agent_permissions_consent" not in state


# ── _write_config_with_comments ──────────────────────────────────────────────


class TestWriteConfigWithComments:
    def test_writes_projects_to_config(self, tmp_path):
        config_path = tmp_path / "config.yaml"
        projects = [{"path": "/tmp/proj1", "priority": 1}]

        _write_config_with_comments(config_path, projects, {}, "off")

        text = config_path.read_text()
        assert "/tmp/proj1" in text

    def test_falls_back_to_yaml_dump_without_template(self, tmp_path):
        config_path = tmp_path / "config.yaml"
        projects = [{"path": "/tmp/proj", "priority": 1}]

        _write_config_with_comments(config_path, projects, {}, "off")

        # File must be valid YAML
        data = yaml.safe_load(config_path.read_text())
        assert data["projects"][0]["path"] == "/tmp/proj"

    def test_uses_template_when_available(self, tmp_path):
        # _write_config_with_comments searches config_path.parent for the template
        (tmp_path / "config.yaml.template").write_text(textwrap.dedent("""\
            projects:
              - path: PLACEHOLDER
                priority: 1

            work:
              agent_permissions: "off"
        """))
        config_path = tmp_path / "config.yaml"
        projects = [{"path": "/real/path", "priority": 1}]

        _write_config_with_comments(config_path, projects, {}, "off")

        text = config_path.read_text()
        assert "/real/path" in text
        assert "PLACEHOLDER" not in text

    def test_sets_agent_permissions_in_template(self, tmp_path):
        (tmp_path / "config.yaml.template").write_text(textwrap.dedent("""\
            projects:
              - path: PLACEHOLDER

            work:
              agent_permissions: "off"
        """))
        config_path = tmp_path / "config.yaml"

        _write_config_with_comments(
            config_path, [{"path": "/p", "priority": 1}], {}, "full"
        )

        text = config_path.read_text()
        assert 'agent_permissions: "full"' in text

    def test_writes_multiple_projects(self, tmp_path):
        config_path = tmp_path / "config.yaml"
        projects = [
            {"path": "/proj/a", "priority": 1},
            {"path": "/proj/b", "priority": 2},
        ]

        _write_config_with_comments(config_path, projects, {}, "off")

        text = config_path.read_text()
        assert "/proj/a" in text
        assert "/proj/b" in text
