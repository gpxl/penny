"""Unit tests for penny/plugins/loadout_plugin.py — Loadout plugin."""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from penny.plugins.loadout_plugin import (
    Plugin,
    _format_scan_date,
    _needs_scan,
    _query_loadout_status,
)
from penny.tasks import Task

# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_app(
    projects: list[dict[str, Any]] | None = None,
    config_overrides: dict[str, Any] | None = None,
) -> MagicMock:
    """Create a mock PennyApp with configurable projects and state."""
    app = MagicMock()
    config: dict[str, Any] = {"projects": projects or [], "plugins": {}}
    if config_overrides:
        config.update(config_overrides)
    app.config = config
    app.state = {"plugin_state": {}}
    return app


def _make_loadout_status(
    skills: list[dict[str, Any]] | None = None,
    last_scan_at: str | None = None,
    stale: bool | None = None,
) -> dict[str, Any]:
    """Build a mock loadout status --json response."""
    return {
        "project": {"path": "/tmp/proj", "name": "proj", "signals": {}},
        "skills": skills or [],
        "summary": {"project": 0, "global": 0},
        "scan": {"lastScanAt": last_scan_at, "stale": stale},
    }


# ── Plugin Properties ─────────────────────────────────────────────────────────


class TestPluginProperties:
    def test_name(self):
        p = Plugin()
        assert p.name == "loadout"

    def test_description(self):
        p = Plugin()
        assert "loadout" in p.description.lower()

    def test_config_schema_has_expected_keys(self):
        p = Plugin()
        schema = p.config_schema()
        assert "scan_interval_days" in schema
        assert "auto_install_tiers" in schema
        assert "exclude_projects" in schema
        assert schema["scan_interval_days"] == 14


# ── Availability ──────────────────────────────────────────────────────────────


class TestIsAvailable:
    @patch("penny.plugins.loadout_plugin._find_loadout", return_value="/usr/local/bin/loadout")
    def test_available_when_found(self, mock_find):
        p = Plugin()
        assert p.is_available() is True

    @patch("penny.plugins.loadout_plugin._find_loadout", return_value=None)
    def test_unavailable_when_not_found(self, mock_find):
        p = Plugin()
        assert p.is_available() is False


# ── Preflight Checks ─────────────────────────────────────────────────────────


class TestPreflightChecks:
    @patch("penny.plugins.loadout_plugin._find_loadout", return_value=None)
    def test_warns_when_loadout_missing(self, mock_find):
        p = Plugin()
        issues = p.preflight_checks({})
        assert len(issues) == 1
        assert "not found" in issues[0].message

    @patch("penny.plugins.loadout_plugin._find_loadout", return_value="/usr/local/bin/loadout")
    def test_no_issues_when_loadout_present(self, mock_find):
        p = Plugin()
        issues = p.preflight_checks({})
        assert len(issues) == 0


# ── Install Command ──────────────────────────────────────────────────────────


class TestInstallCommand:
    def test_returns_install_command(self):
        p = Plugin()
        cmd = p.install_command()
        assert cmd is not None
        assert "curl" in cmd
        assert "loadout" in cmd

    @patch("penny.plugins.loadout_plugin._find_node_dirs")
    def test_install_command_includes_path_augmentation(self, mock_node_dirs):
        """Install command prepends node/pnpm dirs to PATH for launchd compat."""
        # Use /tmp as a stand-in since install_command filters by is_dir()
        mock_node_dirs.return_value = [Path("/tmp")]
        with patch("penny.plugins.loadout_plugin._EXTRA_BIN_DIRS", [Path("/usr/local/bin")]):
            p = Plugin()
            cmd = p.install_command()
            assert cmd is not None
            assert "export PATH=" in cmd
            assert "/tmp" in cmd
            assert "curl" in cmd

    def test_base_plugin_returns_none(self):
        from penny.plugin import PennyPlugin

        class MinimalPlugin(PennyPlugin):
            @property
            def name(self):
                return "test"

            @property
            def description(self):
                return "test"

            def on_activate(self, app):
                pass

            def on_deactivate(self):
                pass

            def on_agent_spawned(self, task, record, plugin_state):
                pass

            def on_agent_completed(self, record, plugin_state):
                pass

        p = MinimalPlugin()
        assert p.install_command() is None


# ── _format_scan_date ─────────────────────────────────────────────────────────


class TestFormatScanDate:
    def test_today(self):
        now = datetime.now(timezone.utc).isoformat()
        assert _format_scan_date(now) == "today"

    def test_yesterday(self):
        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        assert _format_scan_date(yesterday) == "yesterday"

    def test_days_ago(self):
        five_days = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
        assert _format_scan_date(five_days) == "5 days ago"

    def test_older_shows_date(self):
        old = "2026-01-15T10:00:00Z"
        assert _format_scan_date(old) == "Jan 15"

    def test_invalid_returns_raw(self):
        assert _format_scan_date("garbage") == "garbage"


# ── _needs_scan ───────────────────────────────────────────────────────────────


class TestNeedsScan:
    def test_stale_true_triggers_scan(self):
        cached = {"status": _make_loadout_status(stale=True, last_scan_at="2026-03-01T00:00:00Z")}
        assert _needs_scan(cached, {}) is True

    def test_never_scanned_triggers_scan(self):
        cached = {"status": _make_loadout_status(last_scan_at=None, stale=None)}
        assert _needs_scan(cached, {}) is True

    def test_empty_status_triggers_scan(self):
        cached = {"status": {}}
        assert _needs_scan(cached, {}) is True

    def test_fresh_scan_does_not_trigger(self):
        now = datetime.now(timezone.utc).isoformat()
        cached = {"status": _make_loadout_status(stale=False, last_scan_at=now)}
        assert _needs_scan(cached, {}) is False

    def test_old_scan_triggers_based_on_interval(self):
        old = (datetime.now(timezone.utc) - timedelta(days=15)).isoformat()
        cached = {"status": _make_loadout_status(stale=False, last_scan_at=old)}
        assert _needs_scan(cached, {"scan_interval_days": 14}) is True

    def test_custom_interval_respected(self):
        recent = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
        cached = {"status": _make_loadout_status(stale=False, last_scan_at=recent)}
        # 3-day interval → should trigger
        assert _needs_scan(cached, {"scan_interval_days": 3}) is True
        # 7-day interval → should not trigger
        assert _needs_scan(cached, {"scan_interval_days": 7}) is False

    def test_uses_penny_last_scanned_at_over_loadout(self):
        """Penny's own last_scanned_at is used when loadout's lastScanAt is None."""
        now = datetime.now(timezone.utc).isoformat()
        cached = {
            "status": _make_loadout_status(stale=False, last_scan_at=None),
            "last_scanned_at": now,
        }
        assert _needs_scan(cached, {}) is False

    def test_penny_timestamp_also_checked_for_staleness(self):
        """Penny's timestamp is subject to the same interval check."""
        old = (datetime.now(timezone.utc) - timedelta(days=20)).isoformat()
        cached = {
            "status": _make_loadout_status(stale=False, last_scan_at=None),
            "last_scanned_at": old,
        }
        assert _needs_scan(cached, {"scan_interval_days": 14}) is True


# ── _query_loadout_status ────────────────────────────────────────────────────


class TestQueryLoadoutStatus:
    @patch("penny.plugins.loadout_plugin._find_loadout", return_value=None)
    def test_returns_none_when_not_installed(self, mock_find):
        assert _query_loadout_status("/tmp/proj") is None

    @patch("penny.plugins.loadout_plugin._find_loadout", return_value="/usr/local/bin/loadout")
    @patch("penny.plugins.loadout_plugin.subprocess.run")
    def test_returns_parsed_json_on_success(self, mock_run, mock_find):
        status = _make_loadout_status(
            skills=[{"name": "react-best-practices", "scope": "project", "description": "React patterns"}],
            last_scan_at="2026-03-20T10:00:00Z",
            stale=False,
        )
        mock_run.return_value = MagicMock(
            returncode=0, stdout=json.dumps(status), stderr=""
        )
        result = _query_loadout_status("/tmp/proj")
        assert result is not None
        assert result["scan"]["stale"] is False
        assert len(result["skills"]) == 1

    @patch("penny.plugins.loadout_plugin._find_loadout", return_value="/usr/local/bin/loadout")
    @patch("penny.plugins.loadout_plugin.subprocess.run")
    def test_returns_none_on_nonzero_exit(self, mock_run, mock_find):
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="error")
        assert _query_loadout_status("/tmp/proj") is None

    @patch("penny.plugins.loadout_plugin._find_loadout", return_value="/usr/local/bin/loadout")
    @patch("penny.plugins.loadout_plugin.subprocess.run")
    def test_returns_none_on_invalid_json(self, mock_run, mock_find):
        mock_run.return_value = MagicMock(returncode=0, stdout="not json", stderr="")
        assert _query_loadout_status("/tmp/proj") is None

    @patch("penny.plugins.loadout_plugin._find_loadout", return_value="/usr/local/bin/loadout")
    @patch(
        "penny.plugins.loadout_plugin.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="loadout", timeout=15),
    )
    def test_returns_none_on_timeout(self, mock_run, mock_find):
        assert _query_loadout_status("/tmp/proj") is None


# ── get_tasks ─────────────────────────────────────────────────────────────────


class TestGetTasks:
    def _setup_plugin(
        self,
        projects: list[dict[str, Any]],
        cached_projects: dict[str, Any] | None = None,
        config_overrides: dict[str, Any] | None = None,
    ) -> Plugin:
        app = _make_app(projects, config_overrides)
        p = Plugin()
        p.on_activate(app)
        if cached_projects:
            ps = app.state["plugin_state"].setdefault("loadout", {})
            ps["projects"] = cached_projects
        return p

    def test_returns_task_for_stale_project(self):
        cached = {"/tmp/proj": {"status": _make_loadout_status(stale=True), "scan_in_progress": False}}
        p = self._setup_plugin([{"path": "/tmp/proj", "name": "proj"}], cached)
        tasks = p.get_tasks([{"path": "/tmp/proj", "name": "proj"}])
        assert len(tasks) == 1
        assert tasks[0].task_id == "loadout-scan-proj"
        assert tasks[0].priority == "P3"

    def test_returns_task_for_never_scanned(self):
        cached = {
            "/tmp/proj": {
                "status": _make_loadout_status(last_scan_at=None, stale=None),
                "scan_in_progress": False,
            }
        }
        p = self._setup_plugin([{"path": "/tmp/proj", "name": "proj"}], cached)
        tasks = p.get_tasks([{"path": "/tmp/proj", "name": "proj"}])
        assert len(tasks) == 1

    def test_no_task_for_fresh_project(self):
        now = datetime.now(timezone.utc).isoformat()
        cached = {
            "/tmp/proj": {
                "status": _make_loadout_status(stale=False, last_scan_at=now),
                "scan_in_progress": False,
            }
        }
        p = self._setup_plugin([{"path": "/tmp/proj", "name": "proj"}], cached)
        tasks = p.get_tasks([{"path": "/tmp/proj", "name": "proj"}])
        assert len(tasks) == 0

    def test_skips_excluded_projects(self):
        cached = {"/tmp/proj": {"status": _make_loadout_status(stale=True), "scan_in_progress": False}}
        p = self._setup_plugin(
            [{"path": "/tmp/proj", "name": "proj"}],
            cached,
            config_overrides={"plugins": {"loadout": {"exclude_projects": ["/tmp/proj"]}}},
        )
        tasks = p.get_tasks([{"path": "/tmp/proj", "name": "proj"}])
        assert len(tasks) == 0

    def test_skips_scan_in_progress(self):
        cached = {"/tmp/proj": {"status": _make_loadout_status(stale=True), "scan_in_progress": True}}
        p = self._setup_plugin([{"path": "/tmp/proj", "name": "proj"}], cached)
        tasks = p.get_tasks([{"path": "/tmp/proj", "name": "proj"}])
        assert len(tasks) == 0

    def test_empty_projects_returns_empty(self):
        p = self._setup_plugin([])
        tasks = p.get_tasks([])
        assert tasks == []

    @patch("penny.plugins.loadout_plugin._query_loadout_status")
    def test_lazy_init_queries_loadout(self, mock_query):
        """Projects not in cache get queried lazily on first get_tasks call."""
        mock_query.return_value = _make_loadout_status(stale=True, last_scan_at=None)
        p = self._setup_plugin([{"path": "/tmp/new", "name": "new"}], cached_projects={})
        tasks = p.get_tasks([{"path": "/tmp/new", "name": "new"}])
        mock_query.assert_called_with("/tmp/new")
        assert len(tasks) == 1


# ── Task Description ─────────────────────────────────────────────────────────


class TestTaskDescription:
    def test_returns_description_for_loadout_task(self):
        p = Plugin()
        app = _make_app()
        p.on_activate(app)
        cache = app.state["plugin_state"].setdefault("loadout", {}).setdefault("projects", {})
        cache["/tmp/proj"] = {
            "status": _make_loadout_status(
                skills=[{"name": "react", "scope": "project", "description": ""}],
                stale=True,
            ),
            "scan_in_progress": False,
        }
        task = Task(
            task_id="loadout-scan-proj",
            title="Scan proj",
            priority="P3",
            project_path="/tmp/proj",
            project_name="proj",
            metadata={"plugin": "loadout", "project_path": "/tmp/proj"},
        )
        desc = p.get_task_description(task)
        assert desc is not None
        assert "STALE" in desc
        assert "/tmp/proj" in desc

    def test_returns_none_for_non_loadout_task(self):
        p = Plugin()
        task = Task(
            task_id="other-1",
            title="Other task",
            priority="P2",
            project_path="/tmp/proj",
            project_name="proj",
            metadata={"plugin": "beads"},
        )
        assert p.get_task_description(task) is None


# ── Agent Prompt Template ────────────────────────────────────────────────────


class TestAgentPromptTemplate:
    def test_returns_custom_template(self):
        p = Plugin()
        tmpl = p.get_agent_prompt_template()
        assert tmpl is not None
        assert "loadout scan" in tmpl
        assert "{project_path}" in tmpl
        assert "{task_id}" in tmpl


# ── Agent Callbacks ──────────────────────────────────────────────────────────


class TestAgentCallbacks:
    def test_on_agent_spawned_marks_in_progress(self):
        p = Plugin()
        plugin_state: dict[str, Any] = {}
        task = Task(
            task_id="loadout-scan-proj",
            title="Scan proj",
            priority="P3",
            project_path="/tmp/proj",
            project_name="proj",
            metadata={"plugin": "loadout", "project_path": "/tmp/proj"},
        )
        p.on_agent_spawned(task, {}, plugin_state)
        assert plugin_state["projects"]["/tmp/proj"]["scan_in_progress"] is True

    def test_on_agent_spawned_ignores_non_loadout(self):
        p = Plugin()
        plugin_state: dict[str, Any] = {}
        task = Task(
            task_id="other-1",
            title="Other",
            priority="P2",
            project_path="/tmp/proj",
            project_name="proj",
            metadata={"plugin": "beads"},
        )
        p.on_agent_spawned(task, {}, plugin_state)
        assert "projects" not in plugin_state

    @patch("penny.plugins.loadout_plugin._query_loadout_status")
    def test_on_agent_completed_refreshes_status(self, mock_query):
        p = Plugin()
        new_status = _make_loadout_status(
            skills=[{"name": "react", "scope": "project", "description": ""}],
            stale=False,
            last_scan_at="2026-03-22T10:00:00Z",
        )
        mock_query.return_value = new_status
        plugin_state: dict[str, Any] = {
            "projects": {"/tmp/proj": {"status": {}, "scan_in_progress": True}}
        }
        p.on_agent_completed({}, plugin_state)
        proj = plugin_state["projects"]["/tmp/proj"]
        assert proj["scan_in_progress"] is False
        assert proj["status"]["scan"]["stale"] is False
        mock_query.assert_called_once_with("/tmp/proj")

    @patch("penny.plugins.loadout_plugin._query_loadout_status")
    def test_on_agent_completed_records_penny_timestamp(self, mock_query):
        """on_agent_completed sets last_scanned_at in penny's own state."""
        p = Plugin()
        mock_query.return_value = _make_loadout_status()
        plugin_state: dict[str, Any] = {
            "projects": {"/tmp/proj": {"status": {}, "scan_in_progress": True}}
        }
        p.on_agent_completed({}, plugin_state)
        proj = plugin_state["projects"]["/tmp/proj"]
        assert "last_scanned_at" in proj
        # Should be a valid ISO timestamp
        ts = datetime.fromisoformat(proj["last_scanned_at"].replace("Z", "+00:00"))
        assert (datetime.now(timezone.utc) - ts).total_seconds() < 5

    @patch("penny.plugins.loadout_plugin._query_loadout_status", return_value=None)
    def test_on_agent_completed_handles_query_failure(self, mock_query):
        p = Plugin()
        plugin_state: dict[str, Any] = {
            "projects": {"/tmp/proj": {"status": {"old": True}, "scan_in_progress": True}}
        }
        p.on_agent_completed({}, plugin_state)
        proj = plugin_state["projects"]["/tmp/proj"]
        assert proj["scan_in_progress"] is False
        # Timestamp still recorded even on query failure
        assert "last_scanned_at" in proj
        # Old status preserved on failure
        assert proj["status"] == {"old": True}


# ── Dashboard Card ───────────────────────────────────────────────────────────


class TestDashboardCard:
    def test_empty_state_returns_message(self):
        p = Plugin()
        html = p.dashboard_card_html({}, {})
        assert html is not None
        assert "No projects" in html

    def test_renders_global_section_and_project_table(self):
        state = {
            "plugin_state": {
                "loadout": {
                    "projects": {
                        "/tmp/proj": {
                            "status": _make_loadout_status(
                                skills=[
                                    {"name": "react", "scope": "project", "description": "React patterns"},
                                    {"name": "accessibility", "scope": "global", "description": "A11y"},
                                ],
                                stale=False,
                                last_scan_at="2026-03-20T10:00:00Z",
                            ),
                            "scan_in_progress": False,
                        }
                    }
                }
            }
        }
        p = Plugin()
        html = p.dashboard_card_html(state, {})
        assert html is not None
        assert "Skill Coverage" in html
        # Global section shown separately with expandable skill list
        assert "1 global skill" in html
        assert "accessibility" in html
        assert "shared across all projects" in html
        # Per-project table shows only project-specific count
        assert "Project Skills" in html
        assert "react" in html
        # Column headers
        assert "Last Analyzed" in html
        assert "Scan Status" in html
        assert "Current" in html

    def test_stale_shows_needs_rescan(self):
        state = {
            "plugin_state": {
                "loadout": {
                    "projects": {
                        "/tmp/proj": {
                            "status": _make_loadout_status(stale=True, last_scan_at="2026-03-01T00:00:00Z"),
                            "scan_in_progress": False,
                        }
                    }
                }
            }
        }
        p = Plugin()
        html = p.dashboard_card_html(state, {})
        assert "Needs rescan" in html

    def test_never_scanned_shows_not_analyzed(self):
        state = {
            "plugin_state": {
                "loadout": {
                    "projects": {
                        "/tmp/proj": {
                            "status": _make_loadout_status(stale=None, last_scan_at=None),
                            "scan_in_progress": False,
                        }
                    }
                }
            }
        }
        p = Plugin()
        html = p.dashboard_card_html(state, {})
        assert "Not analyzed" in html

    def test_global_only_project_shows_zero_project_skills(self):
        """Projects with only global skills show '0' in the project skills column."""
        state = {
            "plugin_state": {
                "loadout": {
                    "projects": {
                        "/tmp/proj": {
                            "status": _make_loadout_status(
                                skills=[
                                    {"name": "accessibility", "scope": "global", "description": "A11y"},
                                    {"name": "best-practices", "scope": "global", "description": "BP"},
                                ],
                                stale=False,
                                last_scan_at="2026-03-22T10:00:00Z",
                            ),
                            "scan_in_progress": False,
                        }
                    }
                }
            }
        }
        p = Plugin()
        html = p.dashboard_card_html(state, {})
        # Global section shows combined count
        assert "2 global skill" in html
        # Per-project row shows 0 project skills (plain text, no expandable)
        assert "<td>0</td>" in html

    def test_scan_button_present(self):
        """Dashboard card includes a Scan All button."""
        state = {
            "plugin_state": {
                "loadout": {
                    "projects": {
                        "/tmp/proj": {
                            "status": _make_loadout_status(stale=None, last_scan_at=None),
                            "scan_in_progress": False,
                        }
                    }
                }
            }
        }
        p = Plugin()
        html = p.dashboard_card_html(state, {})
        assert "Scan All" in html

    def test_scan_in_progress_shows_scanning_status(self):
        """Project with scan_in_progress shows Scanning indicator."""
        state = {
            "plugin_state": {
                "loadout": {
                    "projects": {
                        "/tmp/proj": {
                            "status": _make_loadout_status(stale=None, last_scan_at=None),
                            "scan_in_progress": True,
                        }
                    }
                }
            }
        }
        p = Plugin()
        html = p.dashboard_card_html(state, {})
        assert "Scanning" in html
        # Scan All button should be disabled while scanning
        assert "disabled" in html

    def test_penny_timestamp_used_for_last_analyzed(self):
        """Dashboard uses penny's last_scanned_at instead of loadout's broken lastScanAt."""
        now = datetime.now(timezone.utc).isoformat()
        state = {
            "plugin_state": {
                "loadout": {
                    "projects": {
                        "/tmp/proj": {
                            "status": _make_loadout_status(stale=False, last_scan_at=None),
                            "last_scanned_at": now,
                            "scan_in_progress": False,
                        }
                    }
                }
            }
        }
        p = Plugin()
        html = p.dashboard_card_html(state, {})
        assert "today" in html
        assert "Current" in html

    def test_global_skills_deduplicated_across_projects(self):
        """Global skills shown once even when multiple projects report them."""
        state = {
            "plugin_state": {
                "loadout": {
                    "projects": {
                        "/tmp/proj-a": {
                            "status": _make_loadout_status(
                                skills=[
                                    {"name": "accessibility", "scope": "global", "description": ""},
                                    {"name": "react-a", "scope": "project", "description": ""},
                                ],
                            ),
                            "scan_in_progress": False,
                        },
                        "/tmp/proj-b": {
                            "status": _make_loadout_status(
                                skills=[
                                    {"name": "accessibility", "scope": "global", "description": ""},
                                ],
                            ),
                            "scan_in_progress": False,
                        },
                    }
                }
            }
        }
        p = Plugin()
        html = p.dashboard_card_html(state, {})
        # "accessibility" appears in global section, counted once
        assert "1 global skill" in html


# ── Dashboard API ────────────────────────────────────────────────────────────


class TestDashboardApi:
    def test_status_endpoint(self):
        p = Plugin()
        app = _make_app()
        p.on_activate(app)
        cache = app.state["plugin_state"].setdefault("loadout", {}).setdefault("projects", {})
        cache["/tmp/proj"] = {"status": _make_loadout_status(), "scan_in_progress": False}

        result = p.dashboard_api_handler("GET", "status", {})
        assert result is not None
        assert "/tmp/proj" in result["projects"]

    def test_unknown_endpoint_returns_none(self):
        p = Plugin()
        assert p.dashboard_api_handler("GET", "unknown", {}) is None

    def test_scan_endpoint_starts_background_scan(self):
        p = Plugin()
        app = _make_app()
        p.on_activate(app)
        cache = app.state["plugin_state"].setdefault("loadout", {}).setdefault("projects", {})
        cache["/tmp/proj"] = {"status": _make_loadout_status(), "scan_in_progress": False}

        with patch.object(p, "_run_scan_background"):
            result = p.dashboard_api_handler("POST", "scan", {"path": "/tmp/proj"})
            assert result is not None
            assert result["ok"] is True
            assert "/tmp/proj" in result["scanning"]
            assert cache["/tmp/proj"]["scan_in_progress"] is True

    def test_scan_endpoint_skips_already_scanning(self):
        p = Plugin()
        app = _make_app()
        p.on_activate(app)
        cache = app.state["plugin_state"].setdefault("loadout", {}).setdefault("projects", {})
        cache["/tmp/proj"] = {"status": _make_loadout_status(), "scan_in_progress": True}

        with patch.object(p, "_run_scan_background"):
            result = p.dashboard_api_handler("POST", "scan", {"path": "/tmp/proj"})
            assert result["ok"] is True
            assert result["scanning"] == []

    def test_scan_endpoint_unknown_path_returns_error(self):
        p = Plugin()
        app = _make_app()
        p.on_activate(app)

        result = p.dashboard_api_handler("POST", "scan", {"path": "/tmp/nonexistent"})
        assert result is not None
        assert result["ok"] is False


# ── CLI Commands ─────────────────────────────────────────────────────────────


# ── Background Scan ──────────────────────────────────────────────────────────


class TestFinishScan:
    @patch("penny.plugins.loadout_plugin._query_loadout_status")
    def test_finish_scan_success_updates_cache(self, mock_query):
        new_status = _make_loadout_status(
            skills=[{"name": "react", "scope": "project", "description": ""}],
        )
        mock_query.return_value = new_status
        app = _make_app()
        p = Plugin()
        p.on_activate(app)
        cache = p._get_project_cache()
        cache["/tmp/proj"] = {"status": {}, "scan_in_progress": True}

        p._finish_scan("/tmp/proj", success=True)

        proj = cache["/tmp/proj"]
        assert proj["scan_in_progress"] is False
        assert "last_scanned_at" in proj
        assert len(proj["status"]["skills"]) == 1

    def test_finish_scan_failure_clears_in_progress(self):
        app = _make_app()
        p = Plugin()
        p.on_activate(app)
        cache = p._get_project_cache()
        cache["/tmp/proj"] = {"status": {"old": True}, "scan_in_progress": True}

        p._finish_scan("/tmp/proj", success=False)

        proj = cache["/tmp/proj"]
        assert proj["scan_in_progress"] is False
        # No timestamp recorded on failure
        assert "last_scanned_at" not in proj
        # Old status preserved
        assert proj["status"] == {"old": True}


class TestCliCommands:
    def test_registers_loadout_status(self):
        p = Plugin()
        cmds = p.cli_commands()
        assert len(cmds) == 1
        assert cmds[0]["name"] == "loadout-status"


# ── Lifecycle ────────────────────────────────────────────────────────────────


class TestLifecycle:
    @patch("penny.plugins.loadout_plugin._query_loadout_status", return_value=None)
    def test_activate_populates_cache(self, mock_query):
        app = _make_app(projects=[{"path": "/tmp/proj", "name": "proj"}])
        p = Plugin()
        p.on_activate(app)
        mock_query.assert_called_with("/tmp/proj")

    @patch("penny.plugins.loadout_plugin._query_loadout_status")
    def test_activate_requeries_empty_status(self, mock_query):
        """Re-query projects whose cached status is empty (previous query failed)."""
        new_status = _make_loadout_status(
            skills=[{"name": "react", "scope": "project", "description": ""}],
            stale=False,
            last_scan_at="2026-03-22T10:00:00Z",
        )
        mock_query.return_value = new_status
        app = _make_app(projects=[{"path": "/tmp/proj", "name": "proj"}])
        # Pre-populate cache with empty status (simulating a previous failed query)
        ps = app.state["plugin_state"].setdefault("loadout", {})
        ps["projects"] = {"/tmp/proj": {"status": {}, "scan_in_progress": False}}
        p = Plugin()
        p.on_activate(app)
        mock_query.assert_called_with("/tmp/proj")
        cached = ps["projects"]["/tmp/proj"]
        assert len(cached["status"]["skills"]) == 1

    @patch("penny.plugins.loadout_plugin._query_loadout_status")
    def test_activate_skips_populated_cache(self, mock_query):
        """Don't re-query projects with valid cached status."""
        app = _make_app(projects=[{"path": "/tmp/proj", "name": "proj"}])
        ps = app.state["plugin_state"].setdefault("loadout", {})
        ps["projects"] = {
            "/tmp/proj": {
                "status": _make_loadout_status(stale=False, last_scan_at="2026-03-22T10:00:00Z"),
                "scan_in_progress": False,
            }
        }
        p = Plugin()
        p.on_activate(app)
        mock_query.assert_not_called()

    def test_deactivate_clears_app(self):
        p = Plugin()
        app = _make_app()
        p.on_activate(app)
        p.on_deactivate()
        assert p._app is None

    def test_on_first_activated_prints_message(self, capsys):
        p = Plugin()
        app = _make_app()
        p.on_first_activated(app)
        captured = capsys.readouterr()
        assert "skill management enabled" in captured.out


# ── Error Handling and Edge Cases ────────────────────────────────────────────


class TestErrorHandling:
    def test_needs_scan_handles_invalid_iso_format(self):
        """When lastScanAt has invalid ISO format, triggers scan."""
        cached = {"status": _make_loadout_status(last_scan_at="not-a-date", stale=False)}
        assert _needs_scan(cached, {}) is True

    def test_needs_scan_handles_none_last_scan_with_stale_false(self):
        """When lastScanAt is None but stale is explicitly False, still triggers."""
        cached = {"status": {"scan": {"lastScanAt": None, "stale": False}}}
        assert _needs_scan(cached, {}) is True

    @patch("penny.plugins.loadout_plugin._find_loadout", return_value="/usr/local/bin/loadout")
    @patch("penny.plugins.loadout_plugin.subprocess.run")
    def test_query_loadout_status_handles_os_error(self, mock_run, mock_find):
        """When subprocess.run raises OSError, returns None gracefully."""
        mock_run.side_effect = OSError("file not found")
        assert _query_loadout_status("/tmp/proj") is None

    def test_get_tasks_handles_missing_path_in_project(self):
        """Projects without path field are skipped."""
        app = _make_app(projects=[{"name": "proj"}])  # No 'path'
        p = Plugin()
        p.on_activate(app)
        tasks = p.get_tasks([{"name": "proj"}])
        assert len(tasks) == 0

    def test_get_tasks_handles_empty_project_dict(self):
        """Empty project dict is skipped."""
        app = _make_app(projects=[{}])
        p = Plugin()
        p.on_activate(app)
        tasks = p.get_tasks([{}])
        assert len(tasks) == 0

    def test_plugin_config_returns_defaults_when_app_none(self):
        """When app is None, returns default config."""
        p = Plugin()
        config = p._plugin_config()
        assert config["scan_interval_days"] == 14

    def test_plugin_config_returns_defaults_when_app_has_no_config(self):
        """When app.config raises AttributeError, returns defaults."""
        app = MagicMock()
        app.config = None  # Will raise AttributeError on .get()
        p = Plugin()
        p._app = app
        config = p._plugin_config()
        assert config["scan_interval_days"] == 14

    def test_plugin_config_handles_boolean_config_value(self):
        """When plugins.loadout is a bool (disabled), returns defaults."""
        app = _make_app(config_overrides={"plugins": {"loadout": False}})
        p = Plugin()
        p._app = app
        config = p._plugin_config()
        assert config["scan_interval_days"] == 14

    def test_get_projects_returns_empty_when_app_none(self):
        """When app is None, get_projects returns empty list."""
        p = Plugin()
        assert p._get_projects() == []

    def test_get_projects_returns_empty_when_app_has_no_config(self):
        """When app.config has no 'projects', returns empty list."""
        app = MagicMock()
        app.config = {"plugins": {}}  # No 'projects' key
        p = Plugin()
        p._app = app
        assert p._get_projects() == []

    def test_get_project_cache_returns_empty_when_app_none(self):
        """When app is None, get_project_cache returns empty dict."""
        p = Plugin()
        assert p._get_project_cache() == {}

    def test_get_project_cache_initializes_structure(self):
        """get_project_cache creates nested structure if missing."""
        app = _make_app()
        app.state = {}
        p = Plugin()
        p._app = app
        cache = p._get_project_cache()
        assert isinstance(cache, dict)
        # Verify structure was created
        assert "plugin_state" in app.state
        assert "loadout" in app.state["plugin_state"]

    @patch("penny.plugins.loadout_plugin._query_loadout_status", return_value=None)
    def test_refresh_project_handles_query_failure(self, mock_query):
        """When _query_loadout_status returns None, still caches empty status."""
        app = _make_app()
        p = Plugin()
        p._app = app
        p._refresh_project("/tmp/proj")
        cache = p._get_project_cache()
        assert "/tmp/proj" in cache
        assert cache["/tmp/proj"]["scan_in_progress"] is False

    def test_task_description_uses_project_path_from_metadata(self):
        """get_task_description can use project_path from metadata fallback."""
        app = _make_app()
        p = Plugin()
        p._app = app
        cache = app.state["plugin_state"].setdefault("loadout", {}).setdefault("projects", {})
        cache["/tmp/other"] = {
            "status": _make_loadout_status(skills=[], stale=False),
            "scan_in_progress": False,
        }
        task = Task(
            task_id="loadout-scan-proj",
            title="Scan proj",
            priority="P3",
            project_path="/tmp/default",
            project_name="proj",
            metadata={"plugin": "loadout", "project_path": "/tmp/other"},
        )
        desc = p.get_task_description(task)
        assert "/tmp/other" in desc

    def test_get_tasks_lazy_init_on_missing_cache_entry(self):
        """When project is not in cache, get_tasks triggers _refresh_project."""
        app = _make_app(projects=[{"path": "/tmp/fresh", "name": "fresh"}])
        p = Plugin()
        p._app = app
        # Initialize loadout cache structure but leave it empty
        app.state["plugin_state"].setdefault("loadout", {})["projects"] = {}

        with patch("penny.plugins.loadout_plugin._query_loadout_status") as mock_query:
            mock_query.return_value = _make_loadout_status(stale=True)
            tasks = p.get_tasks([{"path": "/tmp/fresh", "name": "fresh"}])
            mock_query.assert_called_with("/tmp/fresh")
            assert len(tasks) == 1

    def test_task_description_shows_fresh_status_when_not_stale(self):
        """Task description shows fresh status for non-stale recent scan."""
        app = _make_app()
        p = Plugin()
        p._app = app
        cache = app.state["plugin_state"].setdefault("loadout", {}).setdefault("projects", {})
        cache["/tmp/proj"] = {
            "status": _make_loadout_status(
                skills=[],
                stale=False,
                last_scan_at="2026-03-22T10:00:00Z",
            ),
            "scan_in_progress": False,
        }
        task = Task(
            task_id="loadout-scan-proj",
            title="Scan proj",
            priority="P3",
            project_path="/tmp/proj",
            project_name="proj",
            metadata={"plugin": "loadout", "project_path": "/tmp/proj"},
        )
        desc = p.get_task_description(task)
        assert "Last scan:" in desc
        assert "2026-03-22" in desc

    def test_get_projects_handles_attribute_error_on_config(self):
        """When app.config.get raises AttributeError, returns empty list."""
        app = MagicMock()
        app.config = MagicMock()
        app.config.get.side_effect = AttributeError("bad config")
        p = Plugin()
        p._app = app
        assert p._get_projects() == []

    def test_get_projects_handles_type_error_on_config(self):
        """When app.config is not dict-like, returns empty list."""
        app = MagicMock()
        app.config = "not a dict"
        p = Plugin()
        p._app = app
        assert p._get_projects() == []

    def test_get_project_cache_handles_attribute_error_on_state(self):
        """When app.state raises AttributeError, returns empty dict."""
        app = MagicMock()
        app.state = MagicMock()
        app.state.setdefault.side_effect = AttributeError("bad state")
        p = Plugin()
        p._app = app
        assert p._get_project_cache() == {}

    def test_get_project_cache_handles_type_error_on_state(self):
        """When app.state is not dict-like, returns empty dict."""
        app = MagicMock()
        app.state = "not a dict"
        p = Plugin()
        p._app = app
        assert p._get_project_cache() == {}
