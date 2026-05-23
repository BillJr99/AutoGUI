"""
Failure handling + recovery_probe + screen_record gates.

Triggers a tool failure via the StubRegistry, verifies failures.py
classifies it correctly and the controller emits the expected events.
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


def _cfg(tmp_path, **agent_overrides):
    cfg = {
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
                "predicate_check_enabled": False,
                "visual_diff_enabled": False,
                "watchdog_stall_threshold": 0,
                "recovery_probe_enabled": False,
            },
            "artifacts": {"dir": str(tmp_path / "art")},
            "progress": {"dir": str(tmp_path / "prog")},
            "memory": {"enabled": False, "dir": str(tmp_path / "mem")},
            "subagent": {"enabled": False},
            "screen_record": {"enabled": False,
                              "fps": 1, "buffer_seconds": 1.0,
                              "max_width": 320,
                              "out_dir": str(tmp_path / "failures")},
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
    cfg["agent"].update(agent_overrides.get("agent", {}))
    if "controller" in agent_overrides:
        cfg["agent"]["controller"].update(agent_overrides["controller"])
    return cfg


# ---------------------------------------------------------------------------
# failures.py classifier
# ---------------------------------------------------------------------------

class TestFailureClassifier:
    def test_permission_error_maps_to_permission(self):
        from failures import classify, FailureClass
        v = classify(tool_name="desktop_click", error_message="Permission denied")
        assert v.cls == FailureClass.PERMISSION

    def test_missing_element_maps_to_missing_element(self):
        from failures import classify, FailureClass
        v = classify(tool_name="desktop_click_element",
                     error_message="Element not found: button[name=Login]")
        assert v.cls == FailureClass.MISSING_ELEMENT

    def test_timeout_maps_to_app_not_ready(self):
        from failures import classify, FailureClass
        v = classify(tool_name="desktop_screenshot",
                     error_message="operation timed out after 5s")
        assert v.cls == FailureClass.APP_NOT_READY

    def test_predicate_failed_flag_short_circuits(self):
        from failures import classify, FailureClass
        v = classify(tool_name="any", error_message="",
                     predicate_failed=True)
        assert v.cls == FailureClass.PREDICATE_NOT_MET


# ---------------------------------------------------------------------------
# tool-call failure surfaces in event stream
# ---------------------------------------------------------------------------

class TestStepFailureFlow:
    @pytest.mark.asyncio
    async def test_step_failure_emitted_when_step_exhausts_retries(self, tmp_path):
        from agent import Agent
        sys.path.insert(0, str(ROOT / "tests"))
        from conftest import StubClient, StubRegistry, make_assistant_text, make_tool_call

        cfg = _cfg(tmp_path)
        client = StubClient()
        registry = StubRegistry()

        def bad_tool(**kw):
            return {"error": "Permission denied"}
        registry.add_handler("bad_tool", bad_tool)

        plan = {"steps": [{"id": "s1", "goal": "must fail",
                            "tools_hint": ["bad_tool"]}]}
        client.queue(_typed_plan_response(json.dumps(plan)))
        # Model never produces STEP_DONE; we feed only tool-calls so
        # the step exhausts its iteration budget.
        for i in range(10):
            client.queue(make_tool_call("bad_tool", {}, call_id=f"c{i}"))

        agent = Agent(client, registry, cfg)
        events = []
        async for ev in agent.run("fail"):
            events.append(ev)
        kinds = [e.kind for e in events]
        assert "step_failure" in kinds or "step_escalate" in kinds


# ---------------------------------------------------------------------------
# recovery_probe gate
# ---------------------------------------------------------------------------

class TestRecoveryProbe:
    @pytest.mark.asyncio
    async def test_recovery_probe_event_when_enabled_and_predicate_fails(self, tmp_path):
        from agent import Agent
        sys.path.insert(0, str(ROOT / "tests"))
        from conftest import StubClient, StubRegistry, make_assistant_text

        cfg = _cfg(tmp_path, controller={
            "predicate_check_enabled": True,
            "recovery_probe_enabled": True,
            "recovery_probe_max_per_step": 2,
        })
        client = StubClient()
        registry = StubRegistry()

        plan = {"steps": [
            {"id": "s1", "goal": "make file",
             "predicate": {"kind": "file_exists",
                           "path": str(tmp_path / "absent.txt")}},
        ]}
        client.queue(_typed_plan_response(json.dumps(plan)))
        client.queue(make_assistant_text("STEP_DONE: claimed"))
        client.queue(make_assistant_text("STEP_DONE: still claiming"))

        agent = Agent(client, registry, cfg)
        events = []
        async for ev in agent.run("rp"):
            events.append(ev)
        kinds = [e.kind for e in events]
        # recovery_probe event should fire because the predicate failed
        # AND the probe is enabled.
        assert "recovery_probe" in kinds
