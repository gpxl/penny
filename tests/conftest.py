"""Shared pytest fixtures for Penny tests."""

from __future__ import annotations

import json
import sys
import types as _types
from unittest.mock import patch

# ---------------------------------------------------------------------------
# Stub macOS-only frameworks so pure-Python functions in penny.app/popover_vc
# can be imported and tested in CI (Linux). Each AppKit/Foundation name
# resolves to a freshly-created empty class, allowing ``class Foo(NSObject)``
# to succeed without PyObjC installed.
# ---------------------------------------------------------------------------
if "objc" not in sys.modules:
    def _stub_module(name: str) -> _types.ModuleType:
        class _StubMod(_types.ModuleType):
            # Set __file__ to None so module-introspecting tools (e.g. Hypothesis)
            # don't receive a synthetic type object instead of a path string.
            __file__ = None  # type: ignore[assignment]

            def __getattr__(self, attr: str):
                t = type(attr, (), {})
                object.__setattr__(self, attr, t)
                return t
        mod = _StubMod(name)
        sys.modules[name] = mod
        return mod

    _objc = _stub_module("objc")
    _objc.python_method = lambda fn: fn  # passthrough decorator
    _stub_module("AppKit")
    _stub_module("Foundation")
    _stub_module("setproctitle")

import pytest


@pytest.fixture
def tmp_state():
    """Return a fresh empty state dict matching _default_state()."""
    return {
        "last_check": None,
        "current_period_start": None,
        "predictions": {},
        "agents_running": [],
        "recently_completed": [],
        "period_history": [],
        "session_history": [],
        "last_session_scan": None,
        "plugin_state": {},
    }


@pytest.fixture
def sample_jsonl_dir(tmp_path):
    """Create a temp dir with synthetic .jsonl files for token counting tests.

    Mirrors the real layout: <home>/.claude/projects/<proj>/<session>.jsonl
    Use ``patch("penny.analysis.Path.home", return_value=tmp_path)`` in tests.
    """
    projects_dir = tmp_path / ".claude" / "projects" / "proj-abc"
    projects_dir.mkdir(parents=True)

    # File with valid assistant messages in the billing period
    convo = projects_dir / "session1.jsonl"
    lines = [
        json.dumps({
            "type": "assistant",
            "timestamp": "2025-01-01T10:00:00.000Z",
            "message": {
                "model": "claude-sonnet-4-6",
                "usage": {
                    "output_tokens": 100,
                    "input_tokens": 50,
                    "cache_creation_input_tokens": 10,
                    "cache_read_input_tokens": 5,
                },
            },
        }),
        json.dumps({
            "type": "assistant",
            "timestamp": "2025-01-01T11:00:00.000Z",
            "message": {
                "model": "claude-opus-4-6",
                "usage": {
                    "output_tokens": 200,
                    "input_tokens": 80,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 0,
                },
            },
        }),
        # Human message — should be skipped
        json.dumps({
            "type": "human",
            "timestamp": "2025-01-01T10:30:00.000Z",
            "message": {"content": "hello"},
        }),
        # Malformed JSON — should be skipped gracefully
        "not valid json at all",
        # Empty line — should be skipped
        "",
    ]
    convo.write_text("\n".join(lines), encoding="utf-8")

    return tmp_path


@pytest.fixture
def rich_jsonl_dir(tmp_path):
    """Create a temp dir with JSONL entries for scan_rich_metrics tests.

    Includes tool_use blocks, isSidechain, pr-link, and multi-model entries.
    """
    projects_dir = tmp_path / ".claude" / "projects" / "proj-rich"
    projects_dir.mkdir(parents=True)

    convo = projects_dir / "session1.jsonl"
    lines = [
        # Opus turn with tool_use (Bash x2)
        json.dumps({
            "type": "assistant",
            "timestamp": "2025-01-10T14:00:00.000Z",
            "sessionId": "sess-111",
            "cwd": "/home/user/proj-a",
            "gitBranch": "main",
            "message": {
                "model": "claude-opus-4-6",
                "usage": {
                    "output_tokens": 500,
                    "input_tokens": 100,
                    "cache_creation_input_tokens": 200,
                    "cache_read_input_tokens": 800,
                },
                "content": [
                    {"type": "tool_use", "name": "Bash"},
                    {"type": "tool_use", "name": "Bash"},
                    {"type": "text", "text": "done"},
                ],
            },
        }),
        # Sonnet turn with tool_use (Read, Edit) — subagent
        json.dumps({
            "type": "assistant",
            "timestamp": "2025-01-10T15:00:00.000Z",
            "sessionId": "sess-222",
            "isSidechain": True,
            "cwd": "/home/user/proj-b",
            "gitBranch": "feature-x",
            "message": {
                "model": "claude-sonnet-4-6",
                "usage": {
                    "output_tokens": 300,
                    "input_tokens": 80,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 400,
                },
                "content": [
                    {"type": "tool_use", "name": "Read"},
                    {"type": "tool_use", "name": "Edit"},
                ],
            },
        }),
        # Haiku turn (no tools)
        json.dumps({
            "type": "assistant",
            "timestamp": "2025-01-10T16:00:00.000Z",
            "sessionId": "sess-111",
            "cwd": "/home/user/proj-a",
            "gitBranch": "main",
            "message": {
                "model": "claude-haiku-4-5-20251001",
                "usage": {
                    "output_tokens": 100,
                    "input_tokens": 20,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 0,
                },
                "content": [{"type": "text", "text": "ok"}],
            },
        }),
        # PR link entry
        json.dumps({
            "type": "pr-link",
            "timestamp": "2025-01-10T16:30:00.000Z",
            "url": "https://github.com/example/repo/pull/42",
        }),
        # Human message — should be skipped
        json.dumps({
            "type": "human",
            "timestamp": "2025-01-10T14:30:00.000Z",
            "message": {"content": "hello"},
        }),
        # Malformed JSON — should be skipped gracefully
        "not valid json at all",
        # Empty line
        "",
    ]
    convo.write_text("\n".join(lines), encoding="utf-8")

    return tmp_path


@pytest.fixture
def mock_subprocess():
    """Patch subprocess.run and subprocess.Popen."""
    with patch("subprocess.run") as mock_run, patch("subprocess.Popen") as mock_popen:
        yield mock_run, mock_popen


@pytest.fixture
def multi_project_jsonl_dir(tmp_path):
    """JSONL data spanning two projects and three sessions for project_usage tests.

    Layout:
      proj-a: sess-aaa (Opus 500, Sonnet 200), sess-bbb (Haiku 100)
      proj-b: sess-ccc (Sonnet 300)
    """
    import os

    projects_dir = tmp_path / ".claude" / "projects" / "proj-multi"
    projects_dir.mkdir(parents=True)

    convo = projects_dir / "session1.jsonl"
    lines = [
        # proj-a, sess-aaa, Opus 500
        json.dumps({
            "type": "assistant",
            "timestamp": "2025-01-10T10:00:00.000Z",
            "sessionId": "sess-aaa",
            "cwd": "/home/user/proj-a",
            "message": {
                "model": "claude-opus-4-6",
                "usage": {"output_tokens": 500, "input_tokens": 0,
                          "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
                "content": [],
            },
        }),
        # proj-a, sess-aaa, Sonnet 200 (same session, different model)
        json.dumps({
            "type": "assistant",
            "timestamp": "2025-01-10T11:00:00.000Z",
            "sessionId": "sess-aaa",
            "cwd": "/home/user/proj-a",
            "message": {
                "model": "claude-sonnet-4-6",
                "usage": {"output_tokens": 200, "input_tokens": 0,
                          "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
                "content": [],
            },
        }),
        # proj-a, sess-bbb, Haiku 100 (second session in same project)
        json.dumps({
            "type": "assistant",
            "timestamp": "2025-01-10T12:00:00.000Z",
            "sessionId": "sess-bbb",
            "cwd": "/home/user/proj-a",
            "message": {
                "model": "claude-haiku-4-5-20251001",
                "usage": {"output_tokens": 100, "input_tokens": 0,
                          "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
                "content": [],
            },
        }),
        # proj-b, sess-ccc, Sonnet 300 (different project)
        json.dumps({
            "type": "assistant",
            "timestamp": "2025-01-10T14:00:00.000Z",
            "sessionId": "sess-ccc",
            "cwd": "/home/user/proj-b",
            "message": {
                "model": "claude-sonnet-4-6",
                "usage": {"output_tokens": 300, "input_tokens": 0,
                          "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
                "content": [],
            },
        }),
    ]
    convo.write_text("\n".join(lines), encoding="utf-8")
    os.utime(convo, None)

    return tmp_path


@pytest.fixture
def sample_config():
    """Return a minimal valid config dict."""
    return {
        "projects": [
            {"path": "/tmp/test-project", "priority": 1},
        ],
        "trigger": {
            "min_capacity_percent": 30,
            "max_days_remaining": 2,
        },
        "work": {
            "max_agents_per_run": 2,
            "task_priority_levels": ["P1", "P2", "P3"],
        },
        "stats_cache_path": "~/.claude/stats-cache.json",
    }
