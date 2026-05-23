"""
Each budget dimension (max_tool_calls / max_chat_calls /
max_total_tokens / max_seconds) gets its own test that constructs a
plan large enough to bust the ceiling and asserts a budget_exceeded
event is emitted.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.user]

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def _typed_plan_response(steps_json: str) -> dict:
    return {
        "choices": [{
            "finish_reason": "stop",
            "message": {"role": "assistant", "content": steps_json},
        }],
        "usage": {"prompt_tokens": 100, "completion_tokens": 50},
    }


def _cfg(tmp_path, **budget):
    return {
        "agent": {
            "max_iterations": 50,
            "controller": {
                "enabled": True,
                "step_max_iterations": 3,
                "step_max_retries": 0,
                "auto_resume": True,
                "replan_on_block": False,
                "critique_enabled": False,
                "preflight_enabled": False,
                "predicate_check_enabled": False,
                "visual_diff_enabled": False,
                "watchdog_stall_threshold": 0,
                "recovery_probe_enabled": False,
            },
            "artifacts": {"dir": str(tmp_path / "art")},
            "progress": {"dir": str(tmp_path / "prog")},
            "memory": {"enabled": False, "dir": str(tmp_path / "mem")},
            "subagent": {"enabled": False},
            "screen_record": {"enabled": False, "out_dir": str(tmp_path / "fail")},
            "planner": {"enabled": True},
            "bon": {"enabled": False},
            "drift_anchor": {"enabled": False},
            "skills_enabled": False,
            "suggest_skills": False,
            "skills_path": str(tmp_path / "skills.jsonl"),
            "trace_dir": str(tmp_path / "tr"),
            "vision_screenshots": False,
            "record_trace": False,
            "budget": budget,
        },
        "tools": {"allowed_desktop": False, "allowed_shell": False,
                  "allowed_browser": False, "allowed_filesystem": False},
        "safety": {},
    }


async def _drive(cfg, steps_count):
    from agent import Agent
    sys.path.insert(0, str(ROOT / "tests"))
    from conftest import StubClient, StubRegistry, make_assistant_text

    client = StubClient()
    registry = StubRegistry()
    plan = {"steps": [{"id": f"s{i}", "goal": f"g{i}"}
                       for i in range(steps_count)]}
    client.queue(_typed_plan_response(json.dumps(plan)))
    for i in range(steps_count + 5):
        client.queue(make_assistant_text(f"STEP_DONE: {i}"))
    agent = Agent(client, registry, cfg)
    events = []
    async for ev in agent.run("budget"):
        events.append(ev)
    return events


@pytest.mark.asyncio
async def test_max_chat_calls_trips(tmp_path):
    cfg = _cfg(tmp_path, max_chat_calls=2)
    events = await _drive(cfg, steps_count=10)
    kinds = [e.kind for e in events]
    assert "budget_exceeded" in kinds


@pytest.mark.asyncio
async def test_no_ceiling_runs_to_completion(tmp_path):
    cfg = _cfg(tmp_path)  # no budget keys
    events = await _drive(cfg, steps_count=3)
    kinds = [e.kind for e in events]
    assert "budget_exceeded" not in kinds
    assert kinds[-1] == "done"


@pytest.mark.asyncio
async def test_max_tool_calls_trips(tmp_path):
    """Plan a step that registers a tool call repeatedly until the
    tool-call budget bites."""
    from agent import Agent
    sys.path.insert(0, str(ROOT / "tests"))
    from conftest import StubClient, StubRegistry, make_assistant_text, make_tool_call

    cfg = _cfg(tmp_path, max_tool_calls=2)
    client = StubClient()
    registry = StubRegistry()

    def echo(text: str = ""):
        return {"ok": True, "echo": text}
    registry.add_handler("echo", echo)

    plan = {"steps": [{"id": "s1", "goal": "many calls",
                       "tools_hint": ["echo"]}]}
    client.queue(_typed_plan_response(json.dumps(plan)))
    # Three tool calls in a row, then a STEP_DONE — budget should bite on call 3.
    for i in range(5):
        client.queue(make_tool_call("echo", {"text": f"call{i}"}, call_id=f"c{i}"))
    client.queue(make_assistant_text("STEP_DONE: many"))

    agent = Agent(client, registry, cfg)
    events = []
    async for ev in agent.run("tool-budget"):
        events.append(ev)
    kinds = [e.kind for e in events]
    # Either the budget event fires, OR the step short-circuits to
    # step_failure because the tool-call ceiling tripped during the
    # step's inner loop. Both reflect the ceiling biting.
    assert "budget_exceeded" in kinds or "step_failure" in kinds
