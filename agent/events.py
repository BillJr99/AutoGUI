"""
agent.events — AgentEvent, the event dataclass yielded by the agent loop.

AgentEvent kinds
----------------
  "text"       — A text segment from the assistant.
  "text_delta" — An incremental text fragment streamed from the assistant
                 (only when openwebui.stream is enabled); the complete text
                 still follows as a normal "text" event.
  "tool_call"  — The model is about to invoke a tool (name + args).
  "tool_result" — The result of a tool call.
  "error"      — An error occurred (message included).
  "done"       — Loop has ended; includes finish_reason and iteration count.
"""

from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Event data classes
# ---------------------------------------------------------------------------

@dataclass
class AgentEvent:
    """
    A single event emitted by the agent loop.

    Fields
    ------
    kind : str
        One of "text", "text_delta", "tool_call", "tool_result", "error",
        "done".
    content : str
        Human-readable content string appropriate to the kind.
    data : dict
        Structured payload (tool name/args, result dict, iteration count, etc.).
    """
    kind: str
    content: str
    data: dict = field(default_factory=dict)
