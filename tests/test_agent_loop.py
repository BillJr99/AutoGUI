"""
test_agent_loop.py — core agent-loop tests (CI tier).

Drives the real Agent code paths — the legacy ReAct executor
(_run_inner), the controller step executor (_run_step /
_run_with_controller), and the streaming variants of both — with a
scripted FakeClient and a stub tool registry.  No live model, no
desktop backend.  This suite is the safety net for a later
decomposition of agent.py: it exercises real Agent behaviour (tool
dispatch, history feedback, retry directives, hallucination guards,
budget ceilings, progress persistence/resume, streaming), not mocks of
the Agent itself.
"""

from __future__ import annotations

import json

from agent import Agent, AgentEvent
from controller import StepStatus
from progress import ProgressStore

from .conftest import StubClient, StubRegistry, make_assistant_text, make_tool_call

# ---------------------------------------------------------------------------
# Scripted streaming client
# ---------------------------------------------------------------------------

STREAM_FAIL = "FAIL"  # sentinel script: raise before yielding anything


class FakeStreamClient(StubClient):
    """StubClient plus a scripted ``chat_stream``.

    Each entry in ``stream_scripts`` is either a list of delta-event dicts
    (yielded in order) or the STREAM_FAIL sentinel (raises immediately, so
    the agent's non-streaming fallback can be observed).
    """

    def __init__(self, stream_scripts: list | None = None):
        super().__init__()
        self._stream_scripts = list(stream_scripts or [])
        self.stream_calls = 0

    def queue_stream(self, script) -> None:
        self._stream_scripts.append(script)

    async def chat_stream(self, messages, tools=None, temperature=None):
        self.stream_calls += 1
        if not self._stream_scripts:
            raise RuntimeError("FakeStreamClient: no scripted stream left")
        script = self._stream_scripts.pop(0)
        if script == STREAM_FAIL:
            raise RuntimeError("scripted stream failure")
        for event in script:
            yield event


def _text_stream(*fragments: str) -> list[dict]:
    events: list[dict] = [{"type": "meta", "id": "s", "model": "m", "created": 1}]
    events += [{"type": "text_delta", "text": f} for f in fragments]
    events.append({"type": "finish", "finish_reason": "stop"})
    return events


def _tool_stream(name: str, *arg_fragments: str, call_id: str = "call_s1") -> list[dict]:
    events: list[dict] = [
        {"type": "meta", "id": "s", "model": "m", "created": 1},
        {"type": "tool_call_delta", "index": 0, "id": call_id, "name": name,
         "arguments": ""},
    ]
    events += [
        {"type": "tool_call_delta", "index": 0, "id": None, "name": None,
         "arguments": frag}
        for frag in arg_fragments
    ]
    events.append({"type": "finish", "finish_reason": "tool_calls"})
    return events


# ---------------------------------------------------------------------------
# Config builders
# ---------------------------------------------------------------------------

def _base_agent_cfg(tmp_path) -> dict:
    return {
        "max_iterations": 8,
        "vision_screenshots": False,
        "record_trace": False,
        "trace_dir": str(tmp_path / "traces"),
        "skills_enabled": False,
        "suggest_skills": False,
        "skills_path": str(tmp_path / "skills" / "skills.jsonl"),
        "screenshots_dir": str(tmp_path / "screenshots"),
        "artifacts": {"dir": str(tmp_path / "artifacts")},
        "progress": {"dir": str(tmp_path / "progress")},
        "memory": {"dir": str(tmp_path / "memory")},
        "subagent": {"enabled": False},
        "screen_record": {"enabled": False},
        "bon": {"enabled": False},
        "drift_anchor": {"enabled": False},
    }


def _legacy_config(tmp_path, *, stream: bool = False) -> dict:
    """Legacy single-loop ReAct executor (controller + planner off)."""
    cfg_agent = _base_agent_cfg(tmp_path)
    cfg_agent["controller"] = {"enabled": False}
    cfg_agent["planner"] = {"enabled": False}
    return {
        "openwebui": {"stream": stream},
        "agent": cfg_agent,
        "tools": {"allowed_desktop": False, "allowed_shell": False, "allowed_browser": False},
        "safety": {"command_confirm_delay_seconds": 0},
    }


def _controller_config(tmp_path, *, stream: bool = False) -> dict:
    """Controller path with the extra LLM passes disabled for determinism."""
    cfg_agent = _base_agent_cfg(tmp_path)
    cfg_agent["controller"] = {
        "enabled": True,
        "step_max_iterations": 4,
        "step_max_retries": 0,
        "auto_resume": True,
        "replan_on_block": False,
        "critique_enabled": False,
        "preflight_enabled": False,
        "predicate_check_enabled": False,
        "visual_diff_enabled": False,
        "watchdog_stall_threshold": 0,
        "recovery_probe_enabled": False,
    }
    cfg_agent["planner"] = {"enabled": True}
    return {
        "openwebui": {"stream": stream},
        "agent": cfg_agent,
        "tools": {"allowed_desktop": False, "allowed_shell": False, "allowed_browser": False},
        "safety": {"command_confirm_delay_seconds": 0},
    }


def _plan_response(*steps: dict) -> dict:
    """Wrap a typed plan in an OpenAI-style chat response."""
    return make_assistant_text(json.dumps({"steps": list(steps)}))


async def _collect(agent: Agent, task: str) -> list[AgentEvent]:
    return [event async for event in agent.run(task)]


def _kinds(events: list[AgentEvent]) -> list[str]:
    return [e.kind for e in events]


# ---------------------------------------------------------------------------
# Legacy ReAct loop: iterate-until-done + tool dispatch + result feedback
# ---------------------------------------------------------------------------

class TestReactLoop:
    async def test_iterates_tool_calls_until_final_text(self, tmp_path):
        client = StubClient()
        registry = StubRegistry()
        registry.add_handler("make_widget", lambda size=0: {"ok": True, "made": size})

        client.queue(make_tool_call("make_widget", {"size": 3}, call_id="c1"))
        client.queue(make_tool_call("make_widget", {"size": 4}, call_id="c2"))
        client.queue(make_assistant_text("Both widgets made."))

        agent = Agent(client, registry, _legacy_config(tmp_path))
        events = await _collect(agent, "make two widgets")

        kinds = _kinds(events)
        assert kinds.count("tool_call") == 2
        assert kinds.count("tool_result") == 2
        assert kinds[-2:] == ["text", "done"]
        assert registry.calls == [("make_widget", {"size": 3}),
                                  ("make_widget", {"size": 4})]
        done = events[-1]
        assert done.data["iterations"] == 3
        assert done.data["finish_reason"] == "stop"

    async def test_tool_result_fed_back_into_history(self, tmp_path):
        client = StubClient()
        registry = StubRegistry()
        registry.add_handler("make_widget", lambda: {"ok": True, "serial": "W-77"})

        client.queue(make_tool_call("make_widget", {}, call_id="c1"))
        client.queue(make_assistant_text("done"))

        agent = Agent(client, registry, _legacy_config(tmp_path))
        await _collect(agent, "widget please")

        tool_msgs = [m for m in agent.history if m.get("role") == "tool"]
        assert len(tool_msgs) == 1
        assert tool_msgs[0]["tool_call_id"] == "c1"
        assert "W-77" in tool_msgs[0]["content"]
        # The model's second call saw the tool result in its message list.
        assert any(m.get("role") == "tool" and "W-77" in m.get("content", "")
                   for m in client.calls[-1])

    async def test_failed_tool_injects_retry_directive(self, tmp_path):
        client = StubClient()
        registry = StubRegistry()
        attempts = {"n": 0}

        def flaky():
            attempts["n"] += 1
            if attempts["n"] == 1:
                return {"error": "widget press jammed"}
            return {"ok": True}

        registry.add_handler("make_widget", flaky)
        client.queue(make_tool_call("make_widget", {}, call_id="c1"))
        client.queue(make_tool_call("make_widget", {}, call_id="c2"))
        client.queue(make_assistant_text("made it on retry"))

        agent = Agent(client, registry, _legacy_config(tmp_path))
        events = await _collect(agent, "make a widget")

        assert attempts["n"] == 2
        assert _kinds(events)[-1] == "done"
        # The failed result was loudly marked and followed by a user-role
        # retry directive naming the failed tool.
        history = agent.history
        assert any(m.get("role") == "tool" and "[TOOL FAILED: make_widget]"
                   in m.get("content", "") for m in history)
        assert any(m.get("role") == "user" and "[RETRY REQUIRED]" in str(m.get("content"))
                   and "make_widget" in str(m.get("content")) for m in history)


# ---------------------------------------------------------------------------
# Hallucination guards
# ---------------------------------------------------------------------------

class TestHallucinationGuards:
    async def test_narrated_action_triggers_correction_and_continues(self, tmp_path):
        client = StubClient()
        client.queue(make_assistant_text("I will now click the OK button."))
        client.queue(make_assistant_text("Task finished."))

        agent = Agent(client, StubRegistry(), _legacy_config(tmp_path))
        events = await _collect(agent, "click ok")

        assert _kinds(events)[-1] == "done"
        assert len(client.calls) == 2  # guard forced a second iteration
        corrections = [m for m in agent.history if m.get("role") == "user"
                       and "[AGENT POLICY — CRITICAL]" in str(m.get("content"))]
        assert len(corrections) == 1

    async def test_giving_up_triggers_retry_directive_and_continues(self, tmp_path):
        client = StubClient()
        client.queue(make_assistant_text("I'm sorry, but I cannot help with this."))
        client.queue(make_assistant_text("Recovered and finished the task."))

        agent = Agent(client, StubRegistry(), _legacy_config(tmp_path))
        events = await _collect(agent, "do the thing")

        assert _kinds(events)[-1] == "done"
        assert len(client.calls) == 2
        retries = [m for m in agent.history if m.get("role") == "user"
                   and "DO NOT give up" in str(m.get("content"))]
        assert len(retries) == 1

    async def test_leaked_tool_call_syntax_gets_format_reminder(self, tmp_path):
        client = StubClient()
        client.queue(_plan_response({"id": "s1", "goal": "make a widget"}))
        # Gemma-style tool-call syntax leaked into assistant TEXT.
        client.queue(make_assistant_text('action: make_widget{"size": 1}'))
        client.queue(make_assistant_text("STEP_DONE: widget made"))

        agent = Agent(client, StubRegistry(), _controller_config(tmp_path))
        events = await _collect(agent, "make a widget")

        kinds = _kinds(events)
        assert "step_done" in kinds
        assert kinds[-1] == "done"
        # The step's second chat call carried the one-shot format reminder.
        last_step_messages = client.calls[-1]
        assert any("tool-call" in str(m.get("content")) and "[CONTROLLER]"
                   in str(m.get("content")) for m in last_step_messages
                   if m.get("role") == "user")

    async def test_guards_do_not_fire_on_clean_answers(self, tmp_path):
        client = StubClient()
        client.queue(make_assistant_text("The widget count is 4."))
        agent = Agent(client, StubRegistry(), _legacy_config(tmp_path))
        events = await _collect(agent, "how many widgets?")
        assert _kinds(events) == ["text", "done"]
        assert len(client.calls) == 1


# ---------------------------------------------------------------------------
# Controller: step loop, tool feedback, budget stop
# ---------------------------------------------------------------------------

class TestControllerLoop:
    async def test_steps_dispatch_tools_and_complete(self, tmp_path):
        client = StubClient()
        registry = StubRegistry()
        registry.add_handler("make_widget", lambda size=0: {"ok": True, "size": size})

        client.queue(_plan_response(
            {"id": "s1", "goal": "make widget", "expected": "widget exists"},
            {"id": "s2", "goal": "confirm", "expected": "confirmed"},
        ))
        client.queue(make_tool_call("make_widget", {"size": 9}))
        client.queue(make_assistant_text("STEP_DONE: widget made"))
        client.queue(make_assistant_text("STEP_DONE: confirmed"))

        agent = Agent(client, registry, _controller_config(tmp_path))
        events = await _collect(agent, "make and confirm a widget")

        kinds = _kinds(events)
        assert kinds.count("step_start") == 2
        assert kinds.count("step_done") == 2
        assert kinds[-1] == "done"
        assert registry.calls == [("make_widget", {"size": 9})]
        assert all(s.status == StepStatus.DONE for s in agent._plan.steps)

    async def test_budget_ceiling_stops_before_next_step(self, tmp_path):
        cfg = _controller_config(tmp_path)
        # Ceilings are strict (> max): the planner's call plus s1's chat
        # crosses max_chat_calls=1, so the stop fires right after s1.
        cfg["agent"]["budget"] = {"max_chat_calls": 1}
        client = StubClient()
        client.queue(_plan_response(
            {"id": "s1", "goal": "one"},
            {"id": "s2", "goal": "two"},
        ))
        client.queue(make_assistant_text("STEP_DONE: 1"))
        client.queue(make_assistant_text("STEP_DONE: 2"))

        agent = Agent(client, StubRegistry(), cfg)
        events = await _collect(agent, "two step budget task")

        kinds = _kinds(events)
        assert "budget_exceeded" in kinds
        assert kinds.count("step_done") == 1  # s2 never ran
        assert events[-1].data.get("finish_reason") == "budget_exceeded"
        # Only two chat calls were ever made.
        assert len(client.calls) == 2


# ---------------------------------------------------------------------------
# Progress persistence + resume
# ---------------------------------------------------------------------------

TWO_STEP_PLAN = (
    {"id": "s1", "goal": "make widget", "expected": "widget exists"},
    {"id": "s2", "goal": "ship widget", "expected": "widget shipped"},
)


class TestPersistenceAndResume:
    async def test_progress_snapshot_written_after_each_step(self, tmp_path):
        cfg = _controller_config(tmp_path)
        client = StubClient()
        client.queue(_plan_response(*TWO_STEP_PLAN))
        client.queue(make_assistant_text("STEP_DONE: made"))

        agent = Agent(client, StubRegistry(), cfg)
        task_text = "make and ship a widget"

        # Simulate a crash: abandon the run right after s1 completes.
        gen = agent.run(task_text)
        async for event in gen:
            if event.kind == "step_done":
                break
        await gen.aclose()

        task_id = ProgressStore.derive_task_id(task_text)
        record = json.loads((tmp_path / "progress" / f"{task_id}.json").read_text())
        assert record["status"] == "running"  # never finalized — crashed
        assert record["completed_step_ids"] == ["s1"]
        snapshot_ids = [s["id"] for s in record["plan_snapshot"]["steps"]]
        assert snapshot_ids == ["s1", "s2"]

    async def test_resume_skips_completed_steps(self, tmp_path):
        cfg = _controller_config(tmp_path)
        task_text = "make and ship a widget"

        # Run 1: crash after s1.
        client1 = StubClient()
        client1.queue(_plan_response(*TWO_STEP_PLAN))
        client1.queue(make_assistant_text("STEP_DONE: made"))
        agent1 = Agent(client1, StubRegistry(), cfg)
        gen = agent1.run(task_text)
        async for event in gen:
            if event.kind == "step_done":
                break
        await gen.aclose()

        # Run 2: same task text, same plan shape — s1 must be skipped.
        client2 = StubClient()
        client2.queue(_plan_response(*TWO_STEP_PLAN))
        client2.queue(make_assistant_text("STEP_DONE: shipped"))
        agent2 = Agent(client2, StubRegistry(), cfg)
        events = await _collect(agent2, task_text)

        started = [e.data["step"]["id"] for e in events if e.kind == "step_start"]
        assert started == ["s2"]
        assert all(s.status == StepStatus.DONE for s in agent2._plan.steps)
        assert events[-1].data.get("status") == "done"

    async def test_resume_ignores_mismatched_plan_shape(self, tmp_path):
        cfg = _controller_config(tmp_path)
        task_text = "make and ship a widget"

        client1 = StubClient()
        client1.queue(_plan_response(*TWO_STEP_PLAN))
        client1.queue(make_assistant_text("STEP_DONE: made"))
        agent1 = Agent(client1, StubRegistry(), cfg)
        gen = agent1.run(task_text)
        async for event in gen:
            if event.kind == "step_done":
                break
        await gen.aclose()

        # Run 2 plans DIFFERENT goals under the same auto-generated ids —
        # persisted completion state must NOT carry over.
        client2 = StubClient()
        client2.queue(_plan_response(
            {"id": "s1", "goal": "launch chrome"},
            {"id": "s2", "goal": "close chrome"},
        ))
        client2.queue(make_assistant_text("STEP_DONE: launched"))
        client2.queue(make_assistant_text("STEP_DONE: closed"))
        agent2 = Agent(client2, StubRegistry(), cfg)
        events = await _collect(agent2, task_text)

        started = [e.data["step"]["id"] for e in events if e.kind == "step_start"]
        assert started == ["s1", "s2"]


# ---------------------------------------------------------------------------
# Streaming paths
# ---------------------------------------------------------------------------

class TestStreamingPaths:
    async def test_legacy_loop_emits_text_deltas_before_text(self, tmp_path):
        client = FakeStreamClient([_text_stream("All ", "done.")])
        agent = Agent(client, StubRegistry(), _legacy_config(tmp_path, stream=True))
        events = await _collect(agent, "do a thing")

        kinds = _kinds(events)
        assert kinds == ["text_delta", "text_delta", "text", "done"]
        deltas = [e.content for e in events if e.kind == "text_delta"]
        assert "".join(deltas) == "All done."
        assert events[2].content == "All done."  # final text = joined deltas
        assert client.stream_calls == 1
        assert client.calls == []  # blocking chat() never used

    async def test_streamed_tool_call_fragments_dispatch_tool(self, tmp_path):
        registry = StubRegistry()
        registry.add_handler("make_widget", lambda size=0: {"ok": True})
        client = FakeStreamClient([
            _tool_stream("make_widget", '{"si', 'ze": 5}'),
            _text_stream("Widget made."),
        ])
        agent = Agent(client, registry, _legacy_config(tmp_path, stream=True))
        events = await _collect(agent, "make a widget")

        assert registry.calls == [("make_widget", {"size": 5})]
        assert _kinds(events)[-1] == "done"

    async def test_stream_failure_falls_back_to_blocking_chat(self, tmp_path):
        client = FakeStreamClient([STREAM_FAIL])
        client.queue(make_assistant_text("fallback answer"))
        agent = Agent(client, StubRegistry(), _legacy_config(tmp_path, stream=True))
        events = await _collect(agent, "do a thing")

        kinds = _kinds(events)
        assert "text_delta" not in kinds
        assert kinds == ["text", "done"]
        assert events[0].content == "fallback answer"
        assert client.stream_calls == 1
        assert len(client.calls) == 1  # blocking fallback used

    async def test_controller_step_streams_deltas_live(self, tmp_path):
        client = FakeStreamClient([_text_stream("STEP_DONE: ", "widget made")])
        client.queue(_plan_response({"id": "s1", "goal": "make widget"}))
        agent = Agent(client, StubRegistry(), _controller_config(tmp_path, stream=True))
        events = await _collect(agent, "make a widget")

        kinds = _kinds(events)
        # Deltas surface between step_start and step_done via the queue drain.
        assert kinds.index("step_start") < kinds.index("text_delta") < kinds.index("step_done")
        deltas = [e for e in events if e.kind == "text_delta"]
        assert "".join(e.content for e in deltas) == "STEP_DONE: widget made"
        assert all(e.data.get("step") == "s1" for e in deltas)
        assert kinds[-1] == "done"

    async def test_controller_stream_failure_falls_back_once_per_step(self, tmp_path):
        client = FakeStreamClient([STREAM_FAIL, _text_stream("STEP_DONE: two")])
        client.queue(_plan_response(
            {"id": "s1", "goal": "one"},
            {"id": "s2", "goal": "two"},
        ))
        client.queue(make_assistant_text("STEP_DONE: one"))  # s1 blocking fallback
        agent = Agent(client, StubRegistry(), _controller_config(tmp_path, stream=True))
        events = await _collect(agent, "two streamed steps")

        kinds = _kinds(events)
        assert kinds.count("step_done") == 2
        assert kinds[-1] == "done"
        # s1: stream failed → blocking fallback; s2: streaming tried again.
        assert client.stream_calls == 2
        assert len(client.calls) == 2  # plan + s1 fallback
        # Only s2's text arrived as deltas.
        assert all(e.data.get("step") == "s2" for e in events if e.kind == "text_delta")
