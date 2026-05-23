"""
Integration: AutoGUI ↔ OSScreenObserver via REST.

Drives the OSO submodule from AutoGUI's ScreenObserverClient and
exercises:
  - find_element fallback path
  - get_window_tree retrieval
  - describe_screen call (OSO-only tool)
  - oso_text bundle assembly (depth shrinking, scope, combined cap)
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.user, pytest.mark.integration]

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))


@pytest.fixture
def soc(oso_server):
    from screen_observer_client import ScreenObserverClient
    cli = ScreenObserverClient({"enabled": True,
                                "base_url": oso_server["base_url"],
                                "timeout_seconds": 5.0})
    return cli


@pytest.mark.asyncio
async def test_get_windows_returns_list(soc):
    r = await soc.get_windows()
    assert r is not None
    assert r["count"] >= 1


@pytest.mark.asyncio
async def test_get_structure_returns_tree(soc):
    r = await soc.get_structure(window_index=0)
    assert r is not None
    assert "tree" in r


@pytest.mark.asyncio
async def test_get_description_round_trips(soc):
    r = await soc.get_description(window_index=0)
    # Mock adapter may return either a populated dict or a graceful
    # "no description" envelope.
    assert r is not None


@pytest.mark.asyncio
async def test_get_sketch_returns_ascii(soc):
    r = await soc.get_sketch(window_index=0)
    assert r is not None
    assert r["sketch"]
    assert r["grid_width"] > 0


@pytest.mark.asyncio
async def test_find_element_in_tree_match(soc):
    """The mock has an Edit MenuItem inside MenuBar — OSO should find it."""
    m = await soc.find_element_in_tree(
        name="Edit", control_type="MenuItem", window_index=0,
    )
    assert m is not None
    assert m["method"] == "screen_observer"
    assert "rect" in m


@pytest.mark.asyncio
async def test_oso_text_bundle_assembles(soc):
    from oso_text import build_text_bundle
    b = await build_text_bundle(
        soc, window_index=0,
        include_sketch=True, include_tree=True,
        tree_start_depth=6, tree_min_depth=1,
        tree_max_chars=1000, max_chars=4000,
    )
    assert b is not None
    assert "description" in b or "sketch" in b or "tree_text" in b


@pytest.mark.asyncio
async def test_oso_text_bundle_returns_none_when_disabled():
    """Bundle helper short-circuits when oso.enabled=False."""
    from oso_text import build_text_bundle
    from screen_observer_client import ScreenObserverClient
    cli = ScreenObserverClient({"enabled": False, "base_url": "x"})
    b = await build_text_bundle(
        cli, window_index=0,
        include_sketch=True, include_tree=True,
        tree_start_depth=6, tree_min_depth=1,
        tree_max_chars=1000, max_chars=4000,
    )
    assert b is None
