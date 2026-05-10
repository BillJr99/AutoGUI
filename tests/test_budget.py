"""Budget tracker coverage."""

from __future__ import annotations

import time

from budget import BudgetTracker


def test_no_ceiling_never_exceeded():
    b = BudgetTracker()
    for _ in range(100):
        b.record_tool()
    b.record_chat({"usage": {"prompt_tokens": 1000, "completion_tokens": 1000}})
    assert b.exceeded is False


def test_tool_call_ceiling_trips():
    b = BudgetTracker(max_tool_calls=3)
    b.record_tool(); b.record_tool(); b.record_tool()
    assert b.exceeded is False
    b.record_tool()
    assert b.exceeded is True


def test_chat_recorder_extracts_usage():
    b = BudgetTracker()
    b.record_chat({"usage": {"prompt_tokens": 200, "completion_tokens": 50}})
    assert b.prompt_tokens == 200
    assert b.completion_tokens == 50
    assert b.total_tokens == 250


def test_text_fallback_when_provider_omits_usage():
    b = BudgetTracker()
    b.record_chat({"choices": []})  # no usage field
    b.record_text_fallback(prompt_chars=400, completion_chars=80)
    assert b.prompt_tokens >= 100
    assert b.completion_tokens >= 20


def test_snapshot_fraction_used_caps_at_max_lever():
    b = BudgetTracker(max_tool_calls=2, max_total_tokens=1000)
    b.record_tool()
    b.record_chat({"usage": {"prompt_tokens": 100, "completion_tokens": 0}})
    snap = b.snapshot()
    # tool_calls/2 = 0.5; tokens 100/1000 = 0.1; max-axis = 0.5
    assert snap.fraction_used >= 0.4 and snap.fraction_used <= 0.6


def test_reason_string_describes_overflow():
    b = BudgetTracker(max_tool_calls=1)
    b.record_tool(); b.record_tool()
    assert "tool_calls" in b.reason()
