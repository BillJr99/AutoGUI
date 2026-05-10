"""
budget.py — Per-task cost telemetry + hard ceilings.

Tracks token usage, tool calls, and wall time for one user task.
Surfaces the totals as ``budget`` events so a TUI / log can show
"73% of token budget used" in real time, and trips a configurable
hard ceiling that converts the in-flight task into a
``budget_exceeded`` failure rather than letting it run forever.

Token counts come from the LLM response when the provider includes
``usage`` in its OpenAI-compatible payload; otherwise we fall back
to a coarse character/4 heuristic so the meter still moves.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class BudgetSnapshot:
    elapsed_seconds: float
    tool_calls: int
    chat_calls: int
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    fraction_used: float
    exceeded: bool
    note: str = ""


@dataclass
class BudgetTracker:
    """
    Holds counters for one task.  Construct a fresh tracker per task.

    Limits set to 0 mean "no ceiling" for that dimension — only the
    others can trip the exceeded flag.
    """
    max_tool_calls: int = 0
    max_chat_calls: int = 0
    max_total_tokens: int = 0
    max_seconds: float = 0.0
    started: float = field(default_factory=time.monotonic)

    tool_calls: int = 0
    chat_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0

    def record_chat(self, response: Any) -> None:
        """
        Increment chat-call counter and pull token counts from an
        OpenAI-style response if usage is present.
        """
        self.chat_calls += 1
        usage = None
        if isinstance(response, dict):
            usage = response.get("usage")
        if isinstance(usage, dict):
            self.prompt_tokens += int(usage.get("prompt_tokens") or 0)
            self.completion_tokens += int(usage.get("completion_tokens") or 0)

    def record_text_fallback(self, prompt_chars: int, completion_chars: int) -> None:
        """Approximate token counter when the provider didn't return usage."""
        self.prompt_tokens += max(1, prompt_chars // 4)
        self.completion_tokens += max(0, completion_chars // 4)

    def record_tool(self) -> None:
        self.tool_calls += 1

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started

    def snapshot(self, *, note: str = "") -> BudgetSnapshot:
        fractions = []
        if self.max_tool_calls:
            fractions.append(self.tool_calls / self.max_tool_calls)
        if self.max_chat_calls:
            fractions.append(self.chat_calls / self.max_chat_calls)
        if self.max_total_tokens:
            fractions.append(self.total_tokens / self.max_total_tokens)
        if self.max_seconds:
            fractions.append(self.elapsed / self.max_seconds)
        frac = max(fractions) if fractions else 0.0
        return BudgetSnapshot(
            elapsed_seconds=round(self.elapsed, 2),
            tool_calls=self.tool_calls,
            chat_calls=self.chat_calls,
            prompt_tokens=self.prompt_tokens,
            completion_tokens=self.completion_tokens,
            total_tokens=self.total_tokens,
            fraction_used=round(frac, 3),
            exceeded=self.exceeded,
            note=note,
        )

    @property
    def exceeded(self) -> bool:
        if self.max_tool_calls and self.tool_calls > self.max_tool_calls:
            return True
        if self.max_chat_calls and self.chat_calls > self.max_chat_calls:
            return True
        if self.max_total_tokens and self.total_tokens > self.max_total_tokens:
            return True
        if self.max_seconds and self.elapsed > self.max_seconds:
            return True
        return False

    def reason(self) -> str:
        parts = []
        if self.max_tool_calls and self.tool_calls > self.max_tool_calls:
            parts.append(f"tool_calls={self.tool_calls}>{self.max_tool_calls}")
        if self.max_chat_calls and self.chat_calls > self.max_chat_calls:
            parts.append(f"chat_calls={self.chat_calls}>{self.max_chat_calls}")
        if self.max_total_tokens and self.total_tokens > self.max_total_tokens:
            parts.append(f"total_tokens={self.total_tokens}>{self.max_total_tokens}")
        if self.max_seconds and self.elapsed > self.max_seconds:
            parts.append(f"elapsed={self.elapsed:.1f}s>{self.max_seconds}s")
        return "; ".join(parts) or "within budget"


__all__ = ["BudgetTracker", "BudgetSnapshot"]
