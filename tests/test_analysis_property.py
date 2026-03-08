"""Property-based tests for penny/analysis.py token counting.

Uses Hypothesis to generate random JSONL payloads and verify invariants:
- Non-negative totals
- Monotonic accumulation (more messages → more or equal tokens)
- Correct Sonnet model filtering
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from penny.analysis import SONNET_MODELS, TokenUsage, count_tokens_since


# ---------------------------------------------------------------------------
# Shared test constants
# ---------------------------------------------------------------------------

_TIMESTAMP = "2025-06-01T10:00:00.000Z"
_SINCE = datetime(2025, 6, 1, 0, 0, 0, tzinfo=timezone.utc)

KNOWN_MODELS = list(SONNET_MODELS) + [
    "claude-opus-4-6",
    "claude-haiku-4-5-20251001",
    "claude-opus-4-20250514",
    "unknown-model",
    "",
]


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

@st.composite
def assistant_message(draw) -> str:
    """Generate a valid assistant JSONL line."""
    model = draw(st.sampled_from(KNOWN_MODELS))
    output_tokens = draw(st.integers(min_value=0, max_value=100_000))
    input_tokens = draw(st.integers(min_value=0, max_value=100_000))
    cache_create = draw(st.integers(min_value=0, max_value=50_000))
    cache_read = draw(st.integers(min_value=0, max_value=50_000))
    return json.dumps({
        "type": "assistant",
        "timestamp": _TIMESTAMP,
        "message": {
            "model": model,
            "usage": {
                "output_tokens": output_tokens,
                "input_tokens": input_tokens,
                "cache_creation_input_tokens": cache_create,
                "cache_read_input_tokens": cache_read,
            },
        },
    })


@st.composite
def non_assistant_message(draw) -> str:
    """Generate a non-assistant JSONL line that must be ignored."""
    msg_type = draw(st.sampled_from(["human", "system", "tool_result"]))
    return json.dumps({
        "type": msg_type,
        "timestamp": _TIMESTAMP,
        "message": {"usage": {"output_tokens": 99999}},
    })


@st.composite
def jsonl_payload(draw) -> tuple[list[str], list[dict]]:
    """Generate a list of JSONL lines and the expected assistant message dicts."""
    n_assistant = draw(st.integers(min_value=0, max_value=20))
    n_noise = draw(st.integers(min_value=0, max_value=10))
    n_malformed = draw(st.integers(min_value=0, max_value=5))

    assistant_lines = [draw(assistant_message()) for _ in range(n_assistant)]
    noise_lines = [draw(non_assistant_message()) for _ in range(n_noise)]
    malformed_lines = ["not json", "{bad}", ""] * n_malformed

    all_lines = assistant_lines + noise_lines + malformed_lines
    all_lines = draw(st.permutations(all_lines))

    assistant_dicts = [json.loads(ln) for ln in assistant_lines]
    return list(all_lines), assistant_dicts


# ---------------------------------------------------------------------------
# Helper: run count_tokens_since against an in-memory JSONL payload
# ---------------------------------------------------------------------------

def _count_from_lines(tmp_path: Path, lines: list[str]) -> TokenUsage:
    """Write JSONL lines to a temp dir and return the counted usage."""
    projects_dir = tmp_path / ".claude" / "projects" / "test-proj"
    projects_dir.mkdir(parents=True, exist_ok=True)
    convo = projects_dir / "session.jsonl"
    convo.write_text("\n".join(lines), encoding="utf-8")
    with patch("penny.analysis.Path.home", return_value=tmp_path):
        return count_tokens_since(_SINCE)


# ---------------------------------------------------------------------------
# Property: non-negative totals
# ---------------------------------------------------------------------------

@given(payload=jsonl_payload())
@settings(max_examples=100)
def test_token_totals_are_non_negative(payload):
    """All token counters must be >= 0 regardless of input content."""
    lines, _ = payload
    with tempfile.TemporaryDirectory() as tmp_dir:
        usage = _count_from_lines(Path(tmp_dir), lines)
    assert usage.output_all >= 0
    assert usage.output_sonnet >= 0
    assert usage.input_all >= 0
    assert usage.cache_create >= 0
    assert usage.cache_read >= 0
    assert usage.turns >= 0


# ---------------------------------------------------------------------------
# Property: Sonnet total never exceeds all-model total
# ---------------------------------------------------------------------------

@given(payload=jsonl_payload())
@settings(max_examples=100)
def test_sonnet_never_exceeds_all(payload):
    """output_sonnet is a subset of output_all — can never exceed it."""
    lines, _ = payload
    with tempfile.TemporaryDirectory() as tmp_dir:
        usage = _count_from_lines(Path(tmp_dir), lines)
    assert usage.output_sonnet <= usage.output_all


# ---------------------------------------------------------------------------
# Property: correct model filtering
# ---------------------------------------------------------------------------

@given(payload=jsonl_payload())
@settings(max_examples=100)
def test_only_sonnet_models_count_toward_sonnet(payload):
    """output_sonnet must equal the sum of output_tokens for Sonnet-model messages."""
    lines, assistant_dicts = payload
    with tempfile.TemporaryDirectory() as tmp_dir:
        usage = _count_from_lines(Path(tmp_dir), lines)

    expected_sonnet = sum(
        d["message"]["usage"]["output_tokens"]
        for d in assistant_dicts
        if d["message"].get("model", "") in SONNET_MODELS
    )
    assert usage.output_sonnet == expected_sonnet


# ---------------------------------------------------------------------------
# Property: output_all matches sum across all assistant messages
# ---------------------------------------------------------------------------

@given(payload=jsonl_payload())
@settings(max_examples=100)
def test_output_all_matches_sum_of_assistant_messages(payload):
    """output_all must equal the sum of output_tokens across all assistant messages."""
    lines, assistant_dicts = payload
    with tempfile.TemporaryDirectory() as tmp_dir:
        usage = _count_from_lines(Path(tmp_dir), lines)

    expected_all = sum(
        d["message"]["usage"]["output_tokens"]
        for d in assistant_dicts
    )
    assert usage.output_all == expected_all


# ---------------------------------------------------------------------------
# Property: monotonic accumulation (more messages → more or equal tokens)
# ---------------------------------------------------------------------------

@given(
    base_payload=jsonl_payload(),
    extra_output=st.integers(min_value=1, max_value=10_000),
    extra_model=st.sampled_from(KNOWN_MODELS),
)
@settings(max_examples=75)
def test_adding_assistant_message_increases_or_maintains_totals(
    base_payload, extra_output, extra_model
):
    """Appending a new assistant message with output_tokens > 0 must not decrease totals."""
    lines, _ = base_payload

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        usage_before = _count_from_lines(tmp_path, lines)

        extra_line = json.dumps({
            "type": "assistant",
            "timestamp": _TIMESTAMP,
            "message": {
                "model": extra_model,
                "usage": {
                    "output_tokens": extra_output,
                    "input_tokens": 0,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 0,
                },
            },
        })
        # Overwrite the same file with the extra line appended
        convo = tmp_path / ".claude" / "projects" / "test-proj" / "session.jsonl"
        convo.write_text("\n".join(lines + [extra_line]), encoding="utf-8")
        with patch("penny.analysis.Path.home", return_value=tmp_path):
            usage_after = count_tokens_since(_SINCE)

    assert usage_after.output_all >= usage_before.output_all
    assert usage_after.turns >= usage_before.turns


# ---------------------------------------------------------------------------
# Property: turns counter matches number of valid assistant messages
# ---------------------------------------------------------------------------

@given(payload=jsonl_payload())
@settings(max_examples=100)
def test_turns_equals_assistant_message_count(payload):
    """turns must equal the number of assistant messages with a usage block."""
    lines, assistant_dicts = payload
    with tempfile.TemporaryDirectory() as tmp_dir:
        usage = _count_from_lines(Path(tmp_dir), lines)
    assert usage.turns == len(assistant_dicts)


# ---------------------------------------------------------------------------
# Edge cases (deterministic, no hypothesis)
# ---------------------------------------------------------------------------

def test_empty_jsonl_gives_zero_usage(tmp_path):
    usage = _count_from_lines(tmp_path, [])
    assert usage == TokenUsage()


def test_only_malformed_lines_gives_zero_usage(tmp_path):
    lines = ["not json", "{ bad", "", "   "]
    usage = _count_from_lines(tmp_path, lines)
    assert usage == TokenUsage()


def test_non_sonnet_model_does_not_count_toward_sonnet(tmp_path):
    lines = [json.dumps({
        "type": "assistant",
        "timestamp": _TIMESTAMP,
        "message": {
            "model": "claude-opus-4-6",
            "usage": {"output_tokens": 500, "input_tokens": 0,
                      "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
        },
    })]
    usage = _count_from_lines(tmp_path, lines)
    assert usage.output_all == 500
    assert usage.output_sonnet == 0


def test_mixed_models_split_correctly(tmp_path):
    lines = [
        json.dumps({
            "type": "assistant",
            "timestamp": _TIMESTAMP,
            "message": {
                "model": "claude-sonnet-4-6",
                "usage": {"output_tokens": 100, "input_tokens": 10,
                          "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
            },
        }),
        json.dumps({
            "type": "assistant",
            "timestamp": _TIMESTAMP,
            "message": {
                "model": "claude-opus-4-6",
                "usage": {"output_tokens": 200, "input_tokens": 20,
                          "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
            },
        }),
    ]
    usage = _count_from_lines(tmp_path, lines)
    assert usage.output_all == 300
    assert usage.output_sonnet == 100
    assert usage.turns == 2
