"""
conftest.py — shared pytest fixtures.

Adds the project root to sys.path so the tests can ``import agent``,
``import controller``, etc. without a package install.  Provides a
mocked OpenWebUI client and a stub ToolRegistry for tests that need to
exercise the controller logic without a live model or backend.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ---------------------------------------------------------------------------
# Stub OpenWebUI client
# ---------------------------------------------------------------------------

class StubClient:
    """
    Replays a scripted sequence of chat responses.

    Construct with a list of dicts in the same shape ``OpenWebUIClient.chat``
    would normally return.  Each ``await chat()`` pops the next response;
    when the list is empty the stub returns a benign final message so a
    runaway test doesn't hang.
    """

    def __init__(self, responses: list[dict] | None = None):
        self._responses = list(responses or [])
        self.calls: list[list[dict]] = []
        self.model = "stub-model"
        self.base_url = "http://stub"

    def queue(self, response: dict) -> None:
        self._responses.append(response)

    async def chat(self, messages, tools=None, temperature=None):
        self.calls.append([dict(m) for m in messages])
        if self._responses:
            return self._responses.pop(0)
        return {
            "choices": [{
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": "STEP_DONE: stub default"},
            }],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }

    @staticmethod
    def extract_message(response: dict) -> dict:
        return response["choices"][0]["message"]

    @staticmethod
    def extract_text(message: dict) -> str:
        c = message.get("content")
        if isinstance(c, str):
            return c
        if isinstance(c, list):
            return "\n".join(p.get("text", "") for p in c if isinstance(p, dict))
        return ""

    @staticmethod
    def extract_tool_calls(message: dict) -> list:
        return message.get("tool_calls") or []


def make_assistant_text(text: str) -> dict:
    """Convenience: scripted assistant final message."""
    return {
        "choices": [{
            "finish_reason": "stop",
            "message": {"role": "assistant", "content": text},
        }],
        "usage": {"prompt_tokens": 12, "completion_tokens": 6},
    }


def make_tool_call(name: str, args: dict, *, call_id: str = "call_1") -> dict:
    """Convenience: scripted assistant turn that issues one tool call."""
    return {
        "choices": [{
            "finish_reason": "tool_calls",
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [{
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": name,
                        "arguments": json.dumps(args),
                    },
                }],
            },
        }],
        "usage": {"prompt_tokens": 11, "completion_tokens": 5},
    }


# ---------------------------------------------------------------------------
# Stub ToolRegistry
# ---------------------------------------------------------------------------

class StubRegistry:
    """
    Tiny ToolRegistry compatible with controller.py / agent.py: returns
    empty schemas, has a list-based dispatch table, and records every
    dispatch call so tests can assert on the sequence.
    """

    def __init__(self):
        self._handlers: dict = {}
        self._schemas: list[dict] = []
        self.calls: list[tuple[str, dict]] = []

    @property
    def schemas(self) -> list[dict]:
        return list(self._schemas)

    def add_tool(self, schema: dict, fn) -> None:
        self._schemas.append(schema)
        self._handlers[schema["function"]["name"]] = fn

    def add_handler(self, name: str, fn) -> None:
        """Register a callable returning either a dict or JSON string."""
        self._handlers[name] = fn
        self._schemas.append({
            "type": "function",
            "function": {"name": name, "parameters": {"type": "object", "properties": {}}},
        })

    async def dispatch(self, name: str, args: dict) -> str:
        self.calls.append((name, dict(args)))
        fn = self._handlers.get(name)
        if fn is None:
            return json.dumps({"error": f"Unknown tool: {name}"})
        result = fn(**args) if not _is_async(fn) else await fn(**args)
        if isinstance(result, str):
            return result
        return json.dumps(result, default=str)

    def list_tools(self) -> list[str]:
        return sorted(self._handlers.keys())


def _is_async(fn) -> bool:
    import inspect
    return inspect.iscoroutinefunction(fn)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def stub_client():
    return StubClient()


@pytest.fixture
def stub_registry():
    return StubRegistry()


@pytest.fixture
def temp_dirs(tmp_path):
    """Common runtime directories under a tmp dir."""
    return {
        "skills": tmp_path / "skills" / "skills.jsonl",
        "trace": tmp_path / "traces",
        "artifacts": tmp_path / "artifacts",
        "progress": tmp_path / "progress",
        "memory": tmp_path / "memory",
        "screenshots": tmp_path / "screenshots",
    }
