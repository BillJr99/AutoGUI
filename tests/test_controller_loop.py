"""
Integration test: drive Agent._run_with_controller end-to-end with a
mocked LLM client + tool registry.  Verifies the full plan → preflight
→ step execution → predicate verification → progress persistence
pipeline with no live model and no desktop backend.
"""

from __future__ import annotations

import json

import pytest

from agent import Agent
from controller import StepStatus

from .conftest import StubClient, StubRegistry, make_assistant_text, make_tool_call


def _config(tmp_path) -> dict:
    return {
        "agent": {
            "max_iterations": 8,
            "controller": {
                "enabled": True,
                "step_max_iterations": 3,
                "step_max_retries": 1,
                "auto_resume": True,
                "replan_on_block": False,    # keep test deterministic
                "critique_enabled": False,    # avoid extra LLM call
                "preflight_enabled": False,
                "predicate_check_enabled": True,
                "visual_diff_enabled": False,
                "watchdog_stall_threshold": 0,
            },
            "artifacts": {"dir": str(tmp_path / "artifacts")},
            "progress": {"dir": str(tmp_path / "progress")},
            "memory": {"dir": str(tmp_path / "memory")},
            "subagent": {"enabled": False},
            "screen_record": {"enabled": False},
            "planner": {"enabled": True},
            "skills_enabled": False,
            "skills_path": str(tmp_path / "skills" / "skills.jsonl"),
            "trace_dir": str(tmp_path / "traces"),
            "vision_screenshots": False,
            "record_trace": False,
        },
        "tools": {"allowed_desktop": False, "allowed_shell": False, "allowed_browser": False},
        "safety": {},
    }


def _typed_plan_response(steps_json: str) -> dict:
    """Wrap a raw JSON plan string in an OpenAI-style chat response."""
    return {
        "choices": [{
            "finish_reason": "stop",
            "message": {"role": "assistant", "content": steps_json},
        }],
        "usage": {"prompt_tokens": 100, "completion_tokens": 50},
    }


@pytest.mark.asyncio
async def test_controller_runs_simple_two_step_plan(tmp_path):
    cfg = _config(tmp_path)
    client = StubClient()
    registry = StubRegistry()

    plan_json = json.dumps({
        "steps": [
            {"id": "s1", "goal": "step one", "expected": "step one done"},
            {"id": "s2", "goal": "step two", "expected": "step two done",
             "depends_on": ["s1"]},
        ],
    })
    # 1) typed-plan call returns the plan
    client.queue(_typed_plan_response(plan_json))
    # 2) step s1 final assistant message
    client.queue(make_assistant_text("STEP_DONE: step one"))
    # 3) step s2 final assistant message
    client.queue(make_assistant_text("STEP_DONE: step two"))

    agent = Agent(client, registry, cfg)

    events = []
    async for event in agent.run("test task"):
        events.append(event)

    kinds = [e.kind for e in events]
    assert "plan" in kinds
    assert kinds.count("step_done") == 2
    assert kinds[-1] == "done"
    assert all(s.status == StepStatus.DONE for s in agent._plan.steps)


@pytest.mark.asyncio
async def test_controller_treats_predicate_miss_as_blocked(tmp_path):
    cfg = _config(tmp_path)
    cfg["agent"]["controller"]["replan_on_block"] = False
    client = StubClient()
    registry = StubRegistry()

    plan_json = json.dumps({
        "steps": [
            {"id": "s1", "goal": "make a file",
             "predicate": {"kind": "file_exists",
                           "path": str(tmp_path / "definitely-missing.txt")}},
        ],
    })
    client.queue(_typed_plan_response(plan_json))
    # Step will declare DONE but the predicate will fail.
    client.queue(make_assistant_text("STEP_DONE: claimed file written"))
    # The retry attempt (after BLOCKED) will also claim done; both fail.
    client.queue(make_assistant_text("STEP_DONE: still claiming"))

    agent = Agent(client, registry, cfg)

    events = []
    async for event in agent.run("test"):
        events.append(event)

    predicate_events = [e for e in events if e.kind == "predicate"]
    assert any(not e.data["ok"] for e in predicate_events)
    failure_events = [e for e in events if e.kind == "step_failure"]
    assert failure_events  # at least one failure event was emitted


@pytest.mark.asyncio
async def test_controller_dispatches_a_tool_call(tmp_path):
    cfg = _config(tmp_path)
    client = StubClient()
    registry = StubRegistry()

    # Register a fake tool the model can call.
    captured = {"called": False}
    def fake_echo(text: str = ""):
        captured["called"] = True
        return {"ok": True, "echo": text}
    registry.add_handler("echo", fake_echo)

    plan_json = json.dumps({
        "steps": [{"id": "s1", "goal": "echo something",
                   "tools_hint": ["echo"]}],
    })
    client.queue(_typed_plan_response(plan_json))
    client.queue(make_tool_call("echo", {"text": "hi"}))
    client.queue(make_assistant_text("STEP_DONE: echoed"))

    agent = Agent(client, registry, cfg)

    events = []
    async for event in agent.run("echo task"):
        events.append(event)

    assert captured["called"] is True
    # The registry should have logged the dispatch.
    assert any(name == "echo" for name, _ in registry.calls)
    assert any(e.kind == "step_done" for e in events)


@pytest.mark.asyncio
async def test_controller_budget_ceiling_stops_loop(tmp_path):
    cfg = _config(tmp_path)
    cfg["agent"]["budget"] = {"max_chat_calls": 2}
    client = StubClient()
    registry = StubRegistry()

    plan_json = json.dumps({
        "steps": [
            {"id": "s1", "goal": "one"},
            {"id": "s2", "goal": "two"},
            {"id": "s3", "goal": "three"},
        ],
    })
    client.queue(_typed_plan_response(plan_json))
    client.queue(make_assistant_text("STEP_DONE: 1"))
    client.queue(make_assistant_text("STEP_DONE: 2"))
    client.queue(make_assistant_text("STEP_DONE: 3"))

    agent = Agent(client, registry, cfg)
    events = []
    async for event in agent.run("budget task"):
        events.append(event)

    kinds = [e.kind for e in events]
    assert "budget_exceeded" in kinds
    # Should not have reached the third step's STEP_DONE.
    assert kinds.count("step_done") < 3
