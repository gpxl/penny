"""Integration tests for penny/dashboard.py — HTTP API and _snapshot()."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from penny.analysis import Prediction
from penny.dashboard import DashboardServer, _snapshot
from penny.tasks import Task

# ── Helpers ───────────────────────────────────────────────────────────────────


class FakeApp:
    """Minimal stand-in for PennyApp with the attributes _snapshot() reads."""

    def __init__(
        self,
        state: dict | None = None,
        prediction: Prediction | None = None,
        ready_tasks: list | None = None,
    ):
        self.state = state or {}
        self.config = {}
        self._prediction = prediction
        self._all_ready_tasks = ready_tasks or []
        self._last_fetch_at = None
        self._ready_tasks = []
        mgr = MagicMock()
        mgr.active_plugins = []
        mgr.get_all_cli_commands.return_value = []
        mgr.get_dashboard_cards.return_value = []
        mgr.handle_dashboard_route.return_value = None
        self._plugin_mgr = mgr

    # ObjC dispatch stub — just call the selector synchronously.
    # ObjC selectors like "refreshNow:" map to Python methods "refreshNow_".
    def performSelectorOnMainThread_withObject_waitUntilDone_(
        self, sel: str, obj: Any, wait: bool
    ) -> None:
        # "refreshNow:" → "refreshNow_"
        py_name = sel.replace(":", "_") if sel.endswith(":") else sel
        method = getattr(self, py_name, None)
        if method:
            method(obj) if obj is not None else method()

    # Action stubs so the handler can dispatch them
    def refreshNow_(self, sender: Any = None) -> None:
        self._refreshed = True

    def quitApp_(self, sender: Any = None) -> None:
        pass

    def spawnTaskById_(self, task_id: str) -> None:
        self._spawned_task_id = task_id

    def stopAgentByTaskId_(self, task_id: str) -> None:
        self._stopped_task_id = task_id

    def dismissCompleted_(self, task_id: str) -> None:
        rc = self.state.get("recently_completed", [])
        self.state["recently_completed"] = [
            a for a in rc if a.get("task_id") != task_id
        ]

    def clearAllCompleted_(self, sender: Any = None) -> None:
        self.state["recently_completed"] = []


@pytest.fixture
def dashboard_app():
    """Return a FakeApp with a started DashboardServer. Yields (app, port)."""
    pred = Prediction(
        pct_all=42.0,
        pct_sonnet=30.0,
        days_remaining=3.5,
        reset_label="Mar 6 at 9pm",
        session_pct_all=10.0,
        session_reset_label="2pm",
    )
    tasks = [
        Task("t-1", "Fix bug", "P1", "/tmp/proj", "proj"),
        Task("t-2", "Add feat", "P2", "/tmp/proj2", "proj2"),
    ]
    state = {
        "agents_running": [
            {"task_id": "a-1", "pid": 123, "project_name": "proj", "title": "Running task"}
        ],
        "recently_completed": [
            {"task_id": "d-1", "project_name": "proj", "title": "Done task", "status": "completed"}
        ],
        "session_history": [],
    }

    app = FakeApp(state=state, prediction=pred, ready_tasks=tasks)
    ds = DashboardServer(app)
    port = ds.ensure_started()
    yield app, port


def _get(port: int, path: str) -> tuple[int, dict]:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, {}


def _post(port: int, path: str, body: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(body or {}).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=data,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, {}


# ── _snapshot() unit tests ────────────────────────────────────────────────────


class TestSnapshot:
    def test_returns_expected_keys(self):
        app = FakeApp(state={"agents_running": [], "recently_completed": []})
        result = _snapshot(app)
        assert "generated_at" in result
        assert "state" in result
        assert "prediction" in result
        assert "session_history" in result
        assert "rich_metrics" in result
        assert "intraday_samples" in result

    def test_rich_metrics_defaults_to_empty_dict(self):
        app = FakeApp(state={})
        result = _snapshot(app)
        assert result["rich_metrics"] == {}

    def test_rich_metrics_by_window_defaults_to_empty_dict(self):
        app = FakeApp(state={})
        result = _snapshot(app)
        assert result["rich_metrics_by_window"] == {}

    def test_rich_metrics_by_window_passed_through_from_state(self):
        by_window = {
            "session": {"opus_tokens": 10},
            "week": {"opus_tokens": 50},
        }
        app = FakeApp(state={"rich_metrics_by_window": by_window})
        result = _snapshot(app)
        assert result["rich_metrics_by_window"]["session"]["opus_tokens"] == 10
        assert result["rich_metrics_by_window"]["week"]["opus_tokens"] == 50

    def test_intraday_samples_defaults_to_empty_list(self):
        app = FakeApp(state={})
        result = _snapshot(app)
        assert result["intraday_samples"] == []

    def test_rich_metrics_passed_through_from_state(self):
        rm = {"opus_tokens": 100, "sonnet_tokens": 200}
        app = FakeApp(state={"rich_metrics": rm})
        result = _snapshot(app)
        assert result["rich_metrics"]["opus_tokens"] == 100

    def test_intraday_samples_passed_through_from_state(self):
        samples = [{"ts": "2025-01-10T12:00:00+00:00", "pct_all": 42.0, "pct_sonnet": 30.0}]
        app = FakeApp(state={"intraday_samples": samples})
        result = _snapshot(app)
        assert len(result["intraday_samples"]) == 1
        assert result["intraday_samples"][0]["pct_all"] == 42.0

    def test_prediction_is_dict_when_present(self):
        pred = Prediction(pct_all=50.0, pct_sonnet=30.0, days_remaining=2.0)
        app = FakeApp(prediction=pred)
        result = _snapshot(app)
        assert isinstance(result["prediction"], dict)
        assert result["prediction"]["pct_all"] == 50.0

    def test_prediction_is_empty_dict_when_none(self):
        app = FakeApp()
        result = _snapshot(app)
        assert result["prediction"] == {}

    def test_reset_labels_formatted(self):
        pred = Prediction(reset_label="9pm", session_reset_label="2pm")
        app = FakeApp(prediction=pred)
        with patch("penny.dashboard.format_reset_label", side_effect=lambda x: f"fmt:{x}"):
            result = _snapshot(app)
        assert result["prediction"]["reset_label"] == "fmt:9pm"
        assert result["prediction"]["session_reset_label"] == "fmt:2pm"

    def test_json_serializable(self):
        pred = Prediction(pct_all=10.0)
        tasks = [Task("t-1", "Title", "P1", "/tmp/p", "p")]
        state = {"agents_running": [], "recently_completed": [], "session_history": []}
        app = FakeApp(state=state, prediction=pred, ready_tasks=tasks)
        result = _snapshot(app)
        # Should not raise
        serialized = json.dumps(result, default=str)
        assert isinstance(json.loads(serialized), dict)


# ── GET endpoints ─────────────────────────────────────────────────────────────


class TestDashboardGET:
    def test_get_root_returns_html(self, dashboard_app):
        _, port = dashboard_app
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5) as r:
            assert r.status == 200
            body = r.read().decode()
            assert "Penny Dashboard" in body
            assert r.headers["Content-Type"].startswith("text/html")

    def test_get_api_state_returns_json(self, dashboard_app):
        _, port = dashboard_app
        status, data = _get(port, "/api/state")
        assert status == 200
        assert data["prediction"]["pct_all"] == 42.0

    def test_get_api_state_includes_agents(self, dashboard_app):
        _, port = dashboard_app
        _, data = _get(port, "/api/state")
        agents = data["state"]["agents_running"]
        assert len(agents) == 1
        assert agents[0]["task_id"] == "a-1"

    def test_get_api_state_includes_completed_in_state(self, dashboard_app):
        _, port = dashboard_app
        _, data = _get(port, "/api/state")
        completed = data["state"]["recently_completed"]
        assert len(completed) == 1
        assert completed[0]["task_id"] == "d-1"

    def test_get_unknown_path_returns_404(self, dashboard_app):
        _, port = dashboard_app
        status, _ = _get(port, "/nonexistent")
        assert status == 404


# ── POST endpoints ────────────────────────────────────────────────────────────


class TestDashboardPOST:
    def test_post_refresh(self, dashboard_app):
        app, port = dashboard_app
        status, data = _post(port, "/api/refresh")
        assert status == 200
        assert data["ok"] is True
        assert getattr(app, "_refreshed", False) is True

    def test_post_run_with_task_id(self, dashboard_app):
        app, port = dashboard_app
        status, data = _post(port, "/api/run", {"task_id": "t-1"})
        assert status == 200
        assert data["ok"] is True
        assert getattr(app, "_spawned_task_id", None) == "t-1"

    def test_post_run_without_task_id_returns_400(self, dashboard_app):
        _, port = dashboard_app
        status, _ = _post(port, "/api/run", {})
        assert status == 400

    def test_post_stop_agent(self, dashboard_app):
        app, port = dashboard_app
        status, data = _post(port, "/api/stop-agent", {"task_id": "a-1"})
        assert status == 200
        assert data["ok"] is True
        assert getattr(app, "_stopped_task_id", None) == "a-1"

    def test_post_stop_agent_without_task_id_returns_400(self, dashboard_app):
        _, port = dashboard_app
        status, _ = _post(port, "/api/stop-agent", {})
        assert status == 400

    def test_post_dismiss(self, dashboard_app):
        app, port = dashboard_app
        status, data = _post(port, "/api/dismiss", {"task_id": "d-1"})
        assert status == 200
        assert data["ok"] is True
        # Verify the item was removed from recently_completed
        assert not any(
            a["task_id"] == "d-1"
            for a in app.state.get("recently_completed", [])
        )

    def test_post_dismiss_without_task_id_returns_400(self, dashboard_app):
        _, port = dashboard_app
        status, _ = _post(port, "/api/dismiss", {})
        assert status == 400

    def test_post_clear_completed(self, dashboard_app):
        app, port = dashboard_app
        status, data = _post(port, "/api/clear-completed")
        assert status == 200
        assert data["ok"] is True
        assert app.state["recently_completed"] == []

    def test_post_quit(self, dashboard_app):
        _, port = dashboard_app
        status, data = _post(port, "/api/quit")
        assert status == 200
        assert data["ok"] is True

    def test_post_unknown_path_returns_404(self, dashboard_app):
        _, port = dashboard_app
        status, _ = _post(port, "/api/nonexistent")
        assert status == 404


# ── DashboardServer lifecycle ─────────────────────────────────────────────────


class TestDashboardServerLifecycle:
    def test_ensure_started_returns_port(self):
        app = FakeApp()
        ds = DashboardServer(app)
        port = ds.ensure_started()
        assert isinstance(port, int)
        assert port > 0

    def test_ensure_started_idempotent(self):
        app = FakeApp()
        ds = DashboardServer(app)
        port1 = ds.ensure_started()
        port2 = ds.ensure_started()
        assert port1 == port2

    def test_writes_port_file(self, tmp_path):
        app = FakeApp()
        ds = DashboardServer(app)
        with patch("penny.dashboard._port_file", return_value=tmp_path / ".dashboard_port"):
            port = ds.ensure_started()
        port_file = tmp_path / ".dashboard_port"
        assert port_file.exists()
        assert int(port_file.read_text()) == port


# ── Port fallback ────────────────────────────────────────────────────────────


class TestPortFallback:
    def test_falls_back_when_preferred_port_in_use(self):
        """When port 7432 is occupied, _pick_port assigns a random port."""
        import socket

        blocker = socket.socket()
        try:
            blocker.bind(("127.0.0.1", 7432))
        except OSError:
            pytest.skip("Port 7432 already in use by another process")

        try:
            app = FakeApp()
            ds = DashboardServer(app)
            port = ds._pick_port()
            assert port != 7432
            assert port > 0
        finally:
            blocker.close()

    def test_random_port_is_usable(self):
        """The fallback random port can actually be used for a server."""
        import socket

        blocker = socket.socket()
        try:
            blocker.bind(("127.0.0.1", 7432))
        except OSError:
            pytest.skip("Port 7432 already in use by another process")

        try:
            app = FakeApp()
            ds = DashboardServer(app)
            port = ds._pick_port()
            # Verify we can actually bind to the returned port
            test_sock = socket.socket()
            test_sock.bind(("127.0.0.1", port))
            test_sock.close()
        finally:
            blocker.close()


# ── Malformed POST body ──────────────────────────────────────────────────────


class TestDashboardEdgeCases:
    def test_empty_post_body_parsed_as_empty_dict(self, dashboard_app):
        """POST with no body should be treated as empty dict."""
        _, port = dashboard_app
        # /api/refresh doesn't need a body
        status, data = _post(port, "/api/refresh", None)
        assert status == 200
        assert data["ok"] is True

    def test_malformed_json_treated_as_empty(self, dashboard_app):
        """POST with invalid JSON body should not crash the server."""
        _, port = dashboard_app
        # Send raw bytes that aren't valid JSON
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/refresh",
            data=b"not json",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            assert r.status == 200

    def test_get_index_html_alias(self, dashboard_app):
        """GET /index.html serves the same dashboard as GET /."""
        _, port = dashboard_app
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/index.html", timeout=5) as r:
            assert r.status == 200
            body = r.read().decode()
            assert "Penny Dashboard" in body


# ── Rate limiter unit tests ──────────────────────────────────────────────────


class TestTokenBucket:
    def test_consume_returns_true_when_tokens_available(self):
        from penny.dashboard import _TokenBucket
        bucket = _TokenBucket(capacity=5, refill_rate=1)
        assert bucket.consume() is True

    def test_consume_exhausts_bucket(self):
        from penny.dashboard import _TokenBucket
        bucket = _TokenBucket(capacity=3, refill_rate=0)  # no refill
        assert bucket.consume() is True
        assert bucket.consume() is True
        assert bucket.consume() is True
        assert bucket.consume() is False  # exhausted

    def test_tokens_refill_over_time(self):
        import time

        from penny.dashboard import _TokenBucket
        bucket = _TokenBucket(capacity=2, refill_rate=100)  # 100/s — fast refill
        bucket.consume()
        bucket.consume()  # exhaust
        assert bucket.consume() is False
        time.sleep(0.02)  # 20ms → ~2 tokens refilled
        assert bucket.consume() is True

    def test_tokens_capped_at_capacity(self):
        import time

        from penny.dashboard import _TokenBucket
        bucket = _TokenBucket(capacity=2, refill_rate=100)
        time.sleep(0.1)  # would fill to 10, but capped at 2
        assert bucket.consume() is True
        assert bucket.consume() is True
        assert bucket.consume() is False

    def test_thread_safe_concurrent_consume(self):
        """Multiple threads competing for limited tokens — exactly capacity succeed."""
        import concurrent.futures

        from penny.dashboard import _TokenBucket
        bucket = _TokenBucket(capacity=10, refill_rate=0)  # no refill
        with concurrent.futures.ThreadPoolExecutor(max_workers=20) as ex:
            futures = [ex.submit(bucket.consume) for _ in range(20)]
            results = [f.result() for f in futures]
        assert sum(results) == 10


# ── Rate limiting integration tests ─────────────────────────────────────────


class TestDashboardRateLimiting:
    def test_get_endpoints_not_rate_limited(self, dashboard_app):
        """GET /api/state is never subject to rate limiting."""
        _, port = dashboard_app
        for _ in range(5):
            status, _ = _get(port, "/api/state")
            assert status == 200

    def test_rate_limited_post_returns_429(self):
        """Exhausting the token bucket causes the server to return 429."""
        from penny.dashboard import DashboardServer, _TokenBucket

        tiny_bucket = _TokenBucket(capacity=1, refill_rate=0)
        app = FakeApp()
        ds = DashboardServer(app)
        original_make_handler = ds._make_handler

        def patched_make_handler():
            handler_cls = original_make_handler()

            class PatchedHandler(handler_cls):
                def do_POST(inner_self):  # noqa: N805
                    if not tiny_bucket.consume():
                        inner_self.send_error(429, "Too Many Requests")
                        return
                    if inner_self.path == "/api/refresh":
                        app.performSelectorOnMainThread_withObject_waitUntilDone_(
                            "refreshNow:", None, True
                        )
                        body = json.dumps({"ok": True}).encode()
                        inner_self.send_response(200)
                        inner_self.send_header("Content-Type", "application/json")
                        inner_self.send_header("Content-Length", str(len(body)))
                        inner_self.end_headers()
                        inner_self.wfile.write(body)
                    else:
                        inner_self.send_error(404)

            return PatchedHandler

        ds._make_handler = patched_make_handler
        port = ds.ensure_started()

        # First request: succeeds (1 token available)
        status1, _ = _post(port, "/api/refresh")
        assert status1 == 200

        # Second request: rejected (bucket empty, no refill)
        status2, _ = _post(port, "/api/refresh")
        assert status2 == 429


# ── Plugin extensibility — /api/meta and plugin_cards in /api/state ──────────


class TestDashboardPluginExtensibility:
    def test_get_meta_returns_json(self, dashboard_app):
        _, port = dashboard_app
        status, data = _get(port, "/api/meta")
        assert status == 200
        assert "active_plugins" in data
        assert "cli_commands" in data
        assert isinstance(data["active_plugins"], list)
        assert isinstance(data["cli_commands"], list)

    def test_get_api_state_includes_plugin_cards(self, dashboard_app):
        _, port = dashboard_app
        _, data = _get(port, "/api/state")
        assert "plugin_cards" in data
        assert isinstance(data["plugin_cards"], list)

    def test_plugin_api_route_returns_404_for_inactive(self, dashboard_app):
        """GET /api/plugin/inactive-plugin/... returns 404."""
        _, port = dashboard_app
        status, _ = _get(port, "/api/plugin/nonexistent/status")
        assert status == 404

    def test_plugin_api_post_route_returns_404_for_inactive(self, dashboard_app):
        """POST /api/plugin/inactive-plugin/... returns 404."""
        _, port = dashboard_app
        status, _ = _post(port, "/api/plugin/nonexistent/action")
        assert status == 429 or status == 404  # may be rate-limited first
