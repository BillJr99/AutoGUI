"""
App-memory store round-trip — the memory_get / memory_note tools.

`memoryEnabled` controls *creation only*: memory_get is always
registered (so existing notes stay readable), memory_note is gated.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.user]

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def _cfg(tmp_path, memory_enabled: bool):
    return {
        "agent": {
            "max_iterations": 4,
            "controller": {"enabled": False},
            "artifacts": {"dir": str(tmp_path / "art")},
            "progress": {"dir": str(tmp_path / "prog")},
            "memory": {"enabled": memory_enabled,
                       "dir": str(tmp_path / "memory")},
            "subagent": {"enabled": False},
            "screen_record": {"enabled": False, "out_dir": str(tmp_path / "fail")},
            "planner": {"enabled": False},
            "bon": {"enabled": False},
            "drift_anchor": {"enabled": False},
            "skills_enabled": False,
            "suggest_skills": False,
            "skills_path": str(tmp_path / "s.jsonl"),
            "trace_dir": str(tmp_path / "tr"),
            "vision_screenshots": False,
            "record_trace": False,
            "budget": {},
        },
        "tools": {"allowed_desktop": False, "allowed_shell": False,
                  "allowed_browser": False, "allowed_filesystem": False},
        "safety": {},
    }


def test_memory_note_writes_record_when_enabled(tmp_path):
    from agent import Agent
    sys.path.insert(0, str(ROOT / "tests"))
    from conftest import StubClient, StubRegistry

    agent = Agent(StubClient(), StubRegistry(),
                   _cfg(tmp_path, memory_enabled=True))
    result = asyncio.run(agent._registry.dispatch(
        "memory_note",
        {"app": "notepad", "text": "needs admin to save to C:\\Windows",
         "tag": "permission"},
    ))
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except json.JSONDecodeError:
            pass
    # Directory was created.
    assert (tmp_path / "memory").exists()
    # Notes can be read back.
    get = asyncio.run(agent._registry.dispatch("memory_get", {"app": "notepad"}))
    if isinstance(get, str):
        try:
            get = json.loads(get)
        except json.JSONDecodeError:
            pass
    body = json.dumps(get)
    assert "admin" in body or "needs" in body


def test_memory_note_absent_when_disabled(tmp_path):
    from agent import Agent
    sys.path.insert(0, str(ROOT / "tests"))
    from conftest import StubClient, StubRegistry

    agent = Agent(StubClient(), StubRegistry(),
                   _cfg(tmp_path, memory_enabled=False))
    names = set(agent._registry.list_tools())
    assert "memory_note" not in names
    assert "memory_get" in names
    assert not (tmp_path / "memory").exists()
