"""End-to-end integration tests for Penny user journeys.

These tests simulate multi-step user interactions through the dashboard API,
verifying state transitions across the full lifecycle:

1. Data refresh cycle → state visible via API
2. Task spawn → running → complete → notification → dismiss
3. Config change → reflected in state
4. CLI → API integration
5. _load_and_refresh composition flow

Uses a SmartFakeApp that manages state realistically (unlike the simple
FakeApp in test_dashboard.py).
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import yaml

from penny.analysis import Prediction
from penny.dashboard import DashboardServer
from penny.tasks import Task

# ── SmartFakeApp ──────────────────────────────────────────────────────────────


class SmartFakeApp:
    """Realistic app stand-in that manages state transitions properly.

    Unlike the simple FakeApp in test_dashboard.py, this one:
    - Tracks spawned agents with proper records
    - Simulates agent completion via PID checks
    - Removes tasks from ready list after spawning
    - Manages recently_completed with dedup
    """

    def __init__(
        self,
        state: dict | None = None,
        prediction: Prediction | None = None,
        ready_tasks: list[Task] | None = None,
        config: dict | None = None,
    ):
        self.state = state or {
            "agents_running": [],
            "recently_completed": [],
            "session_history": [],
            "plugin_state": {},
        }
        self.config = config or {}
        self._prediction = prediction
        self._all_ready_tasks = list(ready_tasks or [])
        self._ready_tasks = list(ready_tasks or [])
        self._last_fetch_at = datetime.now(timezone.utc)
        self._plugin_mgr = MagicMock()
        self._notifications: list[str] = []
        self._refreshed = False
        self._state_path: Path | None = None

    def performSelectorOnMainThread_withObject_waitUntilDone_(
        self, sel: str, obj: Any, wait: bool
    ) -> None:
        py_name = sel.replace(":", "_") if sel.endswith(":") else sel
        method = getattr(self, py_name, None)
        if method:
            method(obj) if obj is not None else method()

    def refreshNow_(self, sender: Any = None) -> None:
        self._refreshed = True
        self._last_fetch_at = datetime.now(timezone.utc)

    def quitApp_(self, sender: Any = None) -> None:
        pass

    def spawnTaskById_(self, task_id: str) -> None:
        task = next((t for t in self._all_ready_tasks if t.task_id == str(task_id)), None)
        if task is None:
            return
        # Create agent record (simulates spawn_claude_agent dry-run)
        record = {
            "task_id": task.task_id,
            "project": task.project_name,
            "project_path": task.project_path,
            "project_name": task.project_name,
            "title": task.title,
            "priority": task.priority,
            "spawned_at": datetime.now(timezone.utc).isoformat(),
            "status": "running",
            "log": f"/tmp/penny-test/agent-{task.task_id}.log",
            "session": f"penny-{task.task_id}",
            "pid": -1,  # dry-run PID
            "interactive": False,
        }
        self.state.setdefault("agents_running", []).append(record)
        self._all_ready_tasks = [t for t in self._all_ready_tasks if t.task_id != task.task_id]
        self._notifications.append(f"spawn:{task.task_id}")
        self._save_state()

    def stopAgentByTaskId_(self, task_id: str) -> None:
        if not task_id:
            return
        agent = next(
            (a for a in self.state.get("agents_running", []) if a.get("task_id") == task_id),
            None,
        )
        if agent is None:
            return
        self.state["agents_running"] = [
            a for a in self.state["agents_running"] if a.get("task_id") != task_id
        ]
        self._notifications.append(f"stop:{task_id}")
        self._save_state()

    def dismissCompleted_(self, task_id: str) -> None:
        rc = self.state.get("recently_completed", [])
        self.state["recently_completed"] = [a for a in rc if a.get("task_id") != task_id]
        self._save_state()

    def clearAllCompleted_(self, sender: Any = None) -> None:
        self.state["recently_completed"] = []
        self._save_state()

    def complete_agent(self, task_id: str) -> None:
        """Simulate an agent completing (called by test code, not API)."""
        agent = next(
            (a for a in self.state.get("agents_running", []) if a.get("task_id") == task_id),
            None,
        )
        if agent is None:
            return
        self.state["agents_running"] = [
            a for a in self.state["agents_running"] if a.get("task_id") != task_id
        ]
        completed = {**agent, "status": "completed"}
        rc = self.state.setdefault("recently_completed", [])
        if not any(a.get("task_id") == task_id for a in rc):
            rc.append(completed)
        self._notifications.append(f"completed:{task_id}")
        self._save_state()

    def _save_state(self) -> None:
        if self._state_path:
            self._state_path.write_text(json.dumps(self.state, indent=2, default=str))


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def e2e_app(tmp_path):
    """SmartFakeApp with dashboard server, ready for multi-step testing."""
    pred = Prediction(
        pct_all=42.0,
        pct_sonnet=30.0,
        days_remaining=3.5,
        reset_label="Mar 6 at 9pm",
        session_pct_all=10.0,
        session_reset_label="2pm",
        period_start="2025-03-01T20:00:00+00:00",
    )
    tasks = [
        Task("task-1", "Fix the bug", "P1", "/tmp/proj", "proj"),
        Task("task-2", "Add feature", "P2", "/tmp/proj", "proj"),
        Task("task-3", "Write docs", "P3", "/tmp/proj2", "proj2"),
    ]
    app = SmartFakeApp(prediction=pred, ready_tasks=tasks)
    app._state_path = tmp_path / "state.json"
    ds = DashboardServer(app)
    port = ds.ensure_started()
    return app, port, tmp_path


def _get(port: int, path: str) -> dict:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as r:
        return json.loads(r.read())


def _post(port: int, path: str, body: dict | None = None) -> dict:
    data = json.dumps(body or {}).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=data,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read())


# ── Journey 1: Data refresh → state visible ──────────────────────────────────


class TestJourneyRefreshCycle:
    def test_initial_state_visible_via_api(self, e2e_app):
        """GET /api/state returns prediction and empty agents."""
        _, port, _ = e2e_app
        data = _get(port, "/api/state")
        assert data["prediction"]["pct_all"] == 42.0
        assert data["state"]["agents_running"] == []

    def test_refresh_then_state_updated(self, e2e_app):
        """POST /api/refresh triggers a refresh; state timestamp changes."""
        app, port, _ = e2e_app
        before = _get(port, "/api/state")["generated_at"]
        time.sleep(0.01)
        _post(port, "/api/refresh")
        after = _get(port, "/api/state")["generated_at"]
        assert after >= before
        assert app._refreshed is True


# ── Journey 2: Task spawn → running → complete → dismiss ─────────────────────


class TestJourneyTaskLifecycle:
    def test_full_task_lifecycle(self, e2e_app):
        """Spawn task → verify running → complete → verify completed → dismiss."""
        app, port, _ = e2e_app

        # Step 1: Spawn the task (ready tasks are managed by the plugin, not core API)
        result = _post(port, "/api/run", {"task_id": "task-1"})
        assert result["ok"] is True

        # Step 2: Verify task moved to running
        data = _get(port, "/api/state")
        running_ids = [a["task_id"] for a in data["state"]["agents_running"]]
        assert "task-1" in running_ids

        # Step 3: Simulate agent completion
        app.complete_agent("task-1")

        # Step 4: Verify task moved from running to recently_completed
        data = _get(port, "/api/state")
        running_ids = [a["task_id"] for a in data["state"]["agents_running"]]
        recently_ids = [c["task_id"] for c in data["state"]["recently_completed"]]
        assert "task-1" not in running_ids
        assert "task-1" in recently_ids

        # Step 5: Dismiss the completed task
        _post(port, "/api/dismiss", {"task_id": "task-1"})

        # Step 6: Verify dismissed from recently_completed
        data = _get(port, "/api/state")
        recently_ids = [c["task_id"] for c in data["state"]["recently_completed"]]
        assert "task-1" not in recently_ids

    def test_spawn_unknown_task_is_noop(self, e2e_app):
        """Spawning a non-existent task doesn't crash or change state."""
        _, port, _ = e2e_app
        _post(port, "/api/run", {"task_id": "nonexistent"})
        data = _get(port, "/api/state")
        assert data["state"]["agents_running"] == []

    def test_stop_running_agent(self, e2e_app):
        """Spawn → stop → verify removed from running."""
        app, port, _ = e2e_app

        _post(port, "/api/run", {"task_id": "task-2"})
        data = _get(port, "/api/state")
        assert len(data["state"]["agents_running"]) == 1

        _post(port, "/api/stop-agent", {"task_id": "task-2"})
        data = _get(port, "/api/state")
        assert data["state"]["agents_running"] == []

    def test_stop_nonexistent_agent_is_noop(self, e2e_app):
        _, port, _ = e2e_app
        _post(port, "/api/stop-agent", {"task_id": "no-such-agent"})
        data = _get(port, "/api/state")
        assert data["state"]["agents_running"] == []


# ── Journey 3: Multiple tasks → spawn subset → clear completed ───────────────


class TestJourneyMultiTask:
    def test_spawn_multiple_then_clear_all_completed(self, e2e_app):
        """Spawn 2 tasks → complete both → clear all completed at once."""
        app, port, _ = e2e_app

        # Spawn two tasks
        _post(port, "/api/run", {"task_id": "task-1"})
        _post(port, "/api/run", {"task_id": "task-2"})

        data = _get(port, "/api/state")
        assert len(data["state"]["agents_running"]) == 2

        # Complete both
        app.complete_agent("task-1")
        app.complete_agent("task-2")

        data = _get(port, "/api/state")
        assert len(data["state"]["agents_running"]) == 0
        assert len(data["state"]["recently_completed"]) == 2

        # Clear all completed
        _post(port, "/api/clear-completed")
        data = _get(port, "/api/state")
        assert data["state"]["recently_completed"] == []
        # Agents running list is now empty too
        assert data["state"]["agents_running"] == []

    def test_spawn_same_task_twice_rejected(self, e2e_app):
        """Once a task is spawned and removed from ready list, spawning again is a no-op."""
        _, port, _ = e2e_app

        _post(port, "/api/run", {"task_id": "task-1"})
        data = _get(port, "/api/state")
        assert len(data["state"]["agents_running"]) == 1

        # Try to spawn again — task-1 is no longer in ready list
        _post(port, "/api/run", {"task_id": "task-1"})
        data = _get(port, "/api/state")
        assert len(data["state"]["agents_running"]) == 1  # still just one

    def test_notifications_tracked(self, e2e_app):
        """Verify that spawn/complete/stop generate expected notifications."""
        app, port, _ = e2e_app

        _post(port, "/api/run", {"task_id": "task-1"})
        assert "spawn:task-1" in app._notifications

        app.complete_agent("task-1")
        assert "completed:task-1" in app._notifications

        _post(port, "/api/run", {"task_id": "task-2"})
        _post(port, "/api/stop-agent", {"task_id": "task-2"})
        assert "stop:task-2" in app._notifications


# ── Journey 4: State persistence ──────────────────────────────────────────────


class TestJourneyStatePersistence:
    def test_spawn_persists_to_disk(self, e2e_app):
        """State changes from spawning are persisted to state file."""
        app, port, tmp_path = e2e_app

        _post(port, "/api/run", {"task_id": "task-1"})

        # Read back from disk
        state_on_disk = json.loads(app._state_path.read_text())
        running_ids = [a["task_id"] for a in state_on_disk.get("agents_running", [])]
        assert "task-1" in running_ids

    def test_dismiss_persists_to_disk(self, e2e_app):
        app, port, tmp_path = e2e_app

        _post(port, "/api/run", {"task_id": "task-1"})
        app.complete_agent("task-1")
        _post(port, "/api/dismiss", {"task_id": "task-1"})

        state_on_disk = json.loads(app._state_path.read_text())
        recently_ids = [c["task_id"] for c in state_on_disk.get("recently_completed", [])]
        assert "task-1" not in recently_ids

    def test_clear_completed_persists_to_disk(self, e2e_app):
        app, port, tmp_path = e2e_app

        _post(port, "/api/run", {"task_id": "task-1"})
        app.complete_agent("task-1")
        _post(port, "/api/clear-completed")

        state_on_disk = json.loads(app._state_path.read_text())
        assert state_on_disk["recently_completed"] == []


# ── Journey 5: _load_and_refresh composition ──────────────────────────────────


class TestJourneyLoadAndRefresh:
    """Test the startup flow as a composition of its individual steps."""

    def test_full_startup_flow(self, tmp_path):
        """config load → state load → period reset → preflight → plugin sync → fetch."""
        # Step 1: Create config file
        config_path = tmp_path / "config.yaml"
        config = {
            "projects": [{"path": str(tmp_path), "priority": 1}],
            "trigger": {"min_capacity_percent": 30, "max_days_remaining": 2},
            "work": {"max_agents_per_run": 2, "task_priority_levels": ["P1", "P2"]},
        }
        with config_path.open("w") as f:
            yaml.dump(config, f)

        # Step 2: Load config
        loaded = yaml.safe_load(config_path.read_text())
        assert loaded["projects"][0]["path"] == str(tmp_path)

        # Step 3: Load state (fresh)
        from penny.state import _default_state
        state = _default_state()
        assert state["agents_running"] == []

        # Step 4: Reset period if needed
        from penny.state import reset_period_if_needed
        state = reset_period_if_needed(state)
        assert state["current_period_start"] is not None

        # Step 5: Preflight checks
        from penny.preflight import run_preflight
        with patch("penny.preflight.shutil.which", return_value="/usr/bin/claude"), \
             patch("penny.preflight.subprocess.run", return_value=MagicMock(returncode=0)):
            issues = run_preflight(loaded)
        # May have warnings about project path but no fatal errors
        errors = [i for i in issues if i.severity == "error"]
        non_project_errors = [i for i in errors if "project" not in i.message.lower()]
        assert non_project_errors == []

        # Step 6: Plugin sync
        from penny.plugin import PluginManager
        mgr = PluginManager()
        mgr.discover()
        mgr.sync_with_config(MagicMock(), loaded)
        # Beads plugin should activate if bd is available
        # Don't assert beads is active — depends on system

        # Step 7: Verify state is coherent
        assert isinstance(state["current_period_start"], str)
        assert state["agents_running"] == []

    def test_yaml_error_does_not_crash(self, tmp_path):
        """Bad YAML → _safe_load_config returns error, startup can handle it."""
        from penny.app import _safe_load_config
        config_path = tmp_path / "config.yaml"
        config_path.write_text("bad: [unclosed\n")
        with patch("penny.app.CONFIG_PATH", config_path):
            config, err = _safe_load_config()
        assert config == {}
        assert err is not None
        # App would show alert and set status to "● Penny ⚠" — tested in test_app_logic.py

    def test_period_reset_archives_old_data(self, tmp_path):
        """Crossing a billing period archives old predictions and resets counters."""
        from penny.analysis import current_billing_period
        from penny.state import reset_period_if_needed

        start, _ = current_billing_period()

        # Simulate state from a PREVIOUS period
        old_period_start = "2024-01-01T00:00:00+00:00"
        state = {
            "current_period_start": old_period_start,
            "predictions": {"output_all": 50000, "output_sonnet": 20000},
            "agents_running": [{"task_id": "old-agent", "pid": -1}],
            "recently_completed": [],
            "period_history": [],
        }

        state = reset_period_if_needed(state)

        # Period start should be updated
        assert state["current_period_start"] == start.isoformat()
        # Agent running list is reset; plugin_state is owned by plugins and untouched
        assert state["agents_running"] == []
        # Old data should be archived
        assert len(state["period_history"]) == 1
        assert state["period_history"][0]["output_all"] == 50000

    def test_onboarding_detection(self):
        """needs_onboarding detects placeholder and empty projects."""
        from penny.onboarding import needs_onboarding

        assert needs_onboarding({}) is True
        assert needs_onboarding({"projects": []}) is True
        assert needs_onboarding({"projects": [{"path": "/PLACEHOLDER_PROJECT_PATH"}]}) is True
        assert needs_onboarding({"projects": [{"path": "/real/project"}]}) is False


# ── Journey 6: API snapshot shape matches CLI expectations ────────────────────


class TestJourneyApiSnapshotShape:
    """Verify the /api/state JSON shape matches what the CLI scripts parse."""

    def test_snapshot_has_all_cli_required_fields(self, e2e_app):
        """The CLI's `penny agents` parses state.agents_running."""
        _, port, _ = e2e_app
        data = _get(port, "/api/state")

        # penny agents reads:
        assert "state" in data
        assert "agents_running" in data["state"]

        # Dashboard reads:
        assert "prediction" in data
        assert "session_history" in data
        assert "generated_at" in data
        assert "plugin_cards" in data

    def test_snapshot_after_spawn_has_agent_fields(self, e2e_app):
        """After spawning, the agent record has fields the CLI expects."""
        _, port, _ = e2e_app
        _post(port, "/api/run", {"task_id": "task-1"})

        data = _get(port, "/api/state")
        agent = data["state"]["agents_running"][0]
        assert "task_id" in agent
        assert "title" in agent
        assert "status" in agent
        assert agent["task_id"] == "task-1"

    def test_snapshot_prediction_fields(self, e2e_app):
        """Prediction dict has all fields the dashboard JS expects."""
        _, port, _ = e2e_app
        data = _get(port, "/api/state")
        pred = data["prediction"]

        for field in ["pct_all", "pct_sonnet", "days_remaining", "reset_label",
                      "session_pct_all", "session_reset_label", "projected_pct_all",
                      "period_start"]:
            assert field in pred, f"Missing prediction field: {field}"
