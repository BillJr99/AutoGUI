"""
agent.py — Core agentic loop.

The agent implements a ReAct-style (Reason + Act) loop:

  1. Append the user message (or initial task) to the conversation history.
  2. Send the full history plus the tool catalog to the LLM via the client.
  3. Examine finish_reason:
       "stop"       → model produced a final text reply; yield it and exit.
       "tool_calls" → model issued one or more tool calls; dispatch each,
                      append results as role="tool" messages, and loop.
       "length"     → context length exceeded; yield a warning and exit.
  4. Guard against infinite loops with a configurable max_iterations ceiling.

The loop yields AgentEvent objects rather than returning a single final value,
so that the TUI (tui.py) and the CLI one-shot path (main.py) can both consume
a streaming event sequence and render incrementally.  This is a generator-based
approach using "yield" rather than async generators with "async yield", keeping
the event delivery compatible with asyncio without requiring AsyncGenerator
plumbing in the callers.

AgentEvent types
----------------
  "text"       — A text segment from the assistant.
  "tool_call"  — The model is about to invoke a tool (name + args).
  "tool_result" — The result of a tool call.
  "error"      — An error occurred (message included).
  "done"       — Loop has ended; includes finish_reason and iteration count.
"""

import asyncio
import json
import logging
import platform as _platform
import re
import traceback
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable

from app_memory import AppMemory, _normalize_app
from artifacts import ArtifactStore
from budget import BudgetTracker
from client import OpenWebUIClient
from controller import (
    Plan,
    PlanStep,
    StepStatus,
    StepVerdict,
    build_step_prompt,
    merge_revised_plan,
    parse_plan,
    parse_step_outcome,
)
from failures import FailureClass, RecoveryAction, classify, escalate_action
import predicates
from planner import Planner
import preflight
from progress import ProgressStore
from prompt_loader import PromptLoader
from screen_record import ScreenRecorder
from skills import SkillStore
from subagent import Subagent
from tools import ToolRegistry
from trace import TraceWriter
import visual_diff
from watchdog import Watchdog

logger = logging.getLogger(__name__)

PLACEHOLDER_REST_OF_FILE