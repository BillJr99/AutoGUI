"""
End-to-end planner tests: scripted plans, replan-on-block, dependency
ordering, and plan merge semantics. Uses the StubClient so no real LLM
is needed.
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


def _cfg(tmp_path):
    return {
        "agent": {
            "max_iterations": 10,
            "controller": {
                "enabled": True,
                "step_max_iterations": 3,
                "step_max_retries": 1,
                "auto_resume": True,
                "replan_on_block": False,
                "critique_enabled": False,
                "preflight_enabled": False,
                "predicate_check_enabled": True,
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
            "budget": {},
        },
        "tools": {"allowed_desktop": False, "allowed_shell": False,
                  "allowed_browser": False, "allowed_filesystem": False},
        "safety": {},
    }


@pytest.mark.asyncio
async def test_planner_executes_dependency_ordered_steps(tmp_path):
    from agent import Agent
    sys.path.insert(0, str(ROOT / "tests"))
    from conftest import StubClient, StubRegistry, make_assistant_text

    client = StubClient()
    registry = StubRegistry()
    plan = {"steps": [
        {"id": "s2", "goal": "second", "depends_on": ["s1"]},
        {"id": "s1", "goal": "first"},
        {"id": "s3", "goal": "third", "depends_on": ["s2"]},
    ]}
    client.queue(_typed_plan_response(json.dumps(plan)))
    client.queue(make_assistant_text("STEP_DONE: first"))
    client.queue(make_assistant_text("STEP_DONE: second"))
    client.queue(make_assistant_text("STEP_DONE: third"))

    agent = Agent(client, registry, _cfg(tmp_path))
    events = []
    async for ev in agent.run("dep order"):
        events.append(ev)
    # step_done.data["step"] holds the full step dict; pull the id.
    step_done_order = [e.data["step"]["id"] for e in events
                       if e.kind == "step_done"]
    assert step_done_order == ["s1", "s2", "s3"]


@pytest.mark.asyncio
async def test_planner_emits_plan_event_with_step_count(tmp_path):
    from agent import Agent
    sys.path.insert(0, str(ROOT / "tests"))
    from conftest import StubClient, StubRegistry, make_assistant_text

    client = StubClient()
    registry = StubRegistry()
    plan = {"steps": [
        {"id": f"s{i}", "goal": f"goal {i}"} for i in range(4)
    ]}
    client.queue(_typed_plan_response(json.dumps(plan)))
    for i in range(4):
        client.queue(make_assistant_text(f"STEP_DONE: {i}"))

    agent = Agent(client, registry, _cfg(tmp_path))
    events = []
    async for ev in agent.run("plan emit"):
        events.append(ev)
    plan_events = [e for e in events if e.kind == "plan"]
    assert len(plan_events) >= 1
    assert len(plan_events[0].data["plan"]["steps"]) == 4
