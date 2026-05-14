"""
dry_run.py — DryRunAgent for the AutoGUI REST API.

Provides a drop-in replacement for Agent that emits canned events
without touching the real desktop, filesystem, or shell.  Useful for:

  - Testing the REST API without a display or OpenWebUI instance.
  - CI environments where desktop automation is not available.
  - Demonstrating the SSE event stream format without side effects.

Usage::

    from dry_run import DryRunAgent
    agent = DryRunAgent()
    async for event in agent.run("Open a browser"):
        print(event.kind, event.content)
"""

import asyncio
from dataclasses import dataclass, field


@dataclass
class AgentEvent:
    """
    Mirrors the AgentEvent dataclass from agent.py so DryRunAgent yields
    the same type as the real Agent without importing the full agent module
    (which has many heavy dependencies).
    """
    kind: str
    content: str
    data: dict = field(default_factory=dict)


class DryRunAgent:
    """
    A mock agent that simulates the Agent.run() interface.

    Yields a fixed sequence of AgentEvent objects representing a plausible
    agentic trace without actually invoking any tools.  The sequence is:

      plan        → high-level plan
      text        → reasoning step
      tool_call   → observe_screen (read-only, no side effects)
      tool_result → mock screen state
      done        → task complete

    All events are tagged with ``[DRY RUN]`` in their content so callers
    can distinguish simulated output from real output.
    """

    async def run(self, command: str):
        """
        Async generator mirroring Agent.run(command).

        Parameters
        ----------
        command : str
            The task description (used verbatim in the plan event).

        Yields
        ------
        AgentEvent
            Canned events that exercise all event kinds used by the real agent.
        """
        await asyncio.sleep(0)  # yield control so callers can await freely

        yield AgentEvent(
            kind="plan",
            content=f"[DRY RUN] Plan to execute: {command}",
            data={"steps": ["Observe screen", "Simulate action", "Confirm result"]},
        )

        yield AgentEvent(
            kind="text",
            content="[DRY RUN] Simulating task execution without touching the desktop.",
            data={},
        )

        yield AgentEvent(
            kind="tool_call",
            content="observe_screen()",
            data={"tool": "observe_screen", "args": {}},
        )

        await asyncio.sleep(0.05)  # brief pause to simulate tool dispatch latency

        yield AgentEvent(
            kind="tool_result",
            content="[DRY RUN] Mock screen state: desktop visible, no windows focused.",
            data={"result": "mock", "dry_run": True},
        )

        yield AgentEvent(
            kind="done",
            content="[DRY RUN] Task complete (simulated)",
            data={"iterations": 1, "finish_reason": "dry_run"},
        )
