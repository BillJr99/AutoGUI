"""
Cooldown behaviour: when OSO becomes unreachable mid-session, the client
must back off, mark itself unavailable, and not hammer the dead server.
Once OSO comes back, the next probe re-enables it.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.user, pytest.mark.integration]

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))


@pytest.mark.asyncio
async def test_cooldown_engages_on_unreachable(oso_server_factory):
    """Start OSO, drop it, then verify the next call back-offs."""
    from screen_observer_client import ScreenObserverClient
    srv = oso_server_factory()
    cli = ScreenObserverClient({"enabled": True,
                                "base_url": srv["base_url"],
                                "timeout_seconds": 1.0})
    assert (await cli.is_available()) is True
    # Kill OSO.
    from tests.user.conftest import _kill_proc
    _kill_proc(srv["proc"])
    # Next call must return False without throwing.
    ok = await cli.is_available()
    assert ok is False
    # And again — should be cheap (cooldown).
    ok2 = await cli.is_available()
    assert ok2 is False


@pytest.mark.asyncio
async def test_disabled_flag_short_circuits():
    """When enabled=False, the client must never make any HTTP calls."""
    from screen_observer_client import ScreenObserverClient
    cli = ScreenObserverClient({"enabled": False, "base_url": "http://x"})
    assert (await cli.is_available()) is False
    # No exceptions, no network — short-circuit must be silent.
