"""
Subagent registration + dispatch.

When subagent.enabled=true, the `ask_subagent` tool must be registered and
calling it must respect the configured `max_tool_calls` ceiling.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.user]

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def _cfg(tmp_path, *, subagent_enabled: bool, max_tool_calls: int = 4):
    return {
        "agent": {
            "max_iterations": 4,
            "controller": {"enabled": False},
            "artifacts": {"dir": str(tmp_path / "art")},
            "progress": {"dir": str(tmp_path / "prog")},
            "memory": {"enabled": False, "dir": str(tmp_path / "mem")},
            "subagent": {"enabled": subagent_enabled,
                          "max_tool_calls": max_tool_calls},
            "screen_record": {"enabled": False, "out_dir": str(tmp_path / "fail")},
            "planner": {"enabled": False},
            "bon": {"enabled": False},
            "drift_anchor": {"enabled": False},
            "skills_enabled": False, "suggest_skills": False,
            "skills_path": str(tmp_path / "s.jsonl"),
            "trace_dir": str(tmp_path / "tr"),
            "vision_screenshots": False,
            "record_trace": False,
            "budget": {},
        },
        "tools": {"allowed_desktop": False, "allowed_shell": False,
                  "allowed_browser": False, "allowed_filesystem": False},
        "safety": {},
    }


def test_subagent_disabled_omits_tool(tmp_path):
    from agent import Agent
    sys.path.insert(0, str(ROOT / "tests"))
    from conftest import StubClient, StubRegistry
    agent = Agent(StubClient(), StubRegistry(),
                   _cfg(tmp_path, subagent_enabled=False))
    assert "ask_subagent" not in agent._registry.list_tools()


def test_subagent_enabled_registers_ask_subagent(tmp_path):
    from agent import Agent
    sys.path.insert(0, str(ROOT / "tests"))
    from conftest import StubClient, StubRegistry
    agent = Agent(StubClient(), StubRegistry(),
                   _cfg(tmp_path, subagent_enabled=True))
    assert "ask_subagent" in agent._registry.list_tools()


def test_ask_subagent_returns_envelope(tmp_path):
    """Dispatch ask_subagent with a scripted reply queued; the call
    must round-trip without crashing and return an envelope."""
    from agent import Agent
    sys.path.insert(0, str(ROOT / "tests"))
    from conftest import StubClient, StubRegistry, make_assistant_text
    client = StubClient()
    # Pre-queue a "final answer" from the subagent's scripted client.
    client.queue(make_assistant_text("subagent says: 42"))
    agent = Agent(client, StubRegistry(),
                   _cfg(tmp_path, subagent_enabled=True, max_tool_calls=2))
    result = asyncio.run(agent._registry.dispatch(
        "ask_subagent", {"question": "what is the answer?"}
    ))
    parsed = json.loads(result) if isinstance(result, str) else result
    assert isinstance(parsed, dict)
    # The subagent should report some kind of answer/error/result field.
    assert any(k in parsed for k in ("answer", "result", "error", "content"))
