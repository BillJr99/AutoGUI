"""
agent — core agentic loop, decomposed from the original single-module agent.py.

The public import surface is unchanged: ``from agent import Agent, AgentEvent``
keeps working exactly as before.  The implementation now lives in:

  events.py      AgentEvent dataclass (+ event-kind documentation)
  loop.py        the Agent class core: run / _run_inner / _run_step /
                 _run_with_controller / _replan and the streaming helper
  dispatch.py    skill/meta tool registration + result summarization
  history.py     history access, progress persistence, plan-state resume
  guards.py      narration / give-up / leaked-tool-call / coherence guards
  bon.py         best-of-N action sampling
  recovery.py    watchdog snapshots, drift anchors, recovery probes, verifier
  oso_bundle.py  OSO text observation bundle assembly
  prompts.py     OS detection + OS-specific system-prompt assembly

``Agent`` remains a single class; the split modules contribute mixins.
"""

from .events import AgentEvent
from .loop import Agent

__all__ = ["Agent", "AgentEvent"]
