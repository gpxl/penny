"""Shared pytest fixtures for Nae Nae tests."""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest


@pytest.fixture
def tmp_state():
    """Return a fresh empty state dict."""
    return {
        "last_check": None,
        "current_period_start": None,
        "predictions": {},
        "agents_running": [],
        "spawned_this_week": [],
        "recently_completed": [],
        "period_history": [],
        "session_history": [],
        "last_session_scan": None,
    }


@pytest.fixture
def sample_jsonl_dir(tmp_path):
    """Create a temp dir with synthetic .jsonl files for token counting tests.

    Mirrors the real layout: <home>/.claude/projects/<proj>/<session>.jsonl
    Use ``patch("naenae.analysis.Path.home", return_value=tmp_path)`` in tests.
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
def mock_subprocess():
    """Patch subprocess.run and subprocess.Popen."""
    with patch("subprocess.run") as mock_run, patch("subprocess.Popen") as mock_popen:
        yield mock_run, mock_popen


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
