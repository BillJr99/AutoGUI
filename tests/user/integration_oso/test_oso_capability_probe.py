"""
Integration: spawn OSScreenObserver (submodule) and probe it from
AutoGUI's ScreenObserverClient. Verifies the wire format, capability
negotiation, and the cooldown fallback.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.user, pytest.mark.integration]

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))


@pytest.mark.asyncio
async def test_is_available_returns_true_when_oso_running(oso_server):
    from screen_observer_client import ScreenObserverClient
    cli = ScreenObserverClient({"enabled": True,
                                "base_url": oso_server["base_url"],
                                "timeout_seconds": 5.0})
    ok = await cli.is_available()
    assert ok is True


@pytest.mark.asyncio
async def test_capabilities_dict_populated(oso_server):
    from screen_observer_client import ScreenObserverClient
    cli = ScreenObserverClient({"enabled": True,
                                "base_url": oso_server["base_url"],
                                "timeout_seconds": 5.0})
    await cli.is_available()
    caps = cli.oso_capabilities
    assert isinstance(caps, dict)
    # accessibility_tree is True on the mock adapter.
    assert caps.get("accessibility_tree") is True


@pytest.mark.asyncio
async def test_cooldown_after_unreachable_server():
    from screen_observer_client import ScreenObserverClient
    cli = ScreenObserverClient({"enabled": True,
                                "base_url": "http://127.0.0.1:1",
                                "timeout_seconds": 0.2})
    # First call should fail and engage cooldown.
    ok = await cli.is_available()
    assert ok is False
    # Subsequent call returns immediately without retrying.
    ok2 = await cli.is_available()
    assert ok2 is False
