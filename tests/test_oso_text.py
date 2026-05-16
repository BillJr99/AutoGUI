"""
test_oso_text.py — Unit tests for the OSO text-observation helpers and the
strict no-OSO-in-context gating.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from oso_text import build_text_bundle, flatten_tree, trim_tree


def _make_tree(branching: int, depth: int, label: str = "n") -> dict:
    """Build a synthetic OSO-shaped tree with given branching/depth."""
    def rec(d: int, path: str) -> dict:
        node = {
            "role": "ButtonControl",
            "name": f"{label}-{path}",
            "bounds": {"x": 0, "y": 0, "width": 10, "height": 10},
            "children": [],
        }
        if d > 0:
            for i in range(branching):
                node["children"].append(rec(d - 1, f"{path}.{i}"))
        return node
    return rec(depth, "0")


def test_flatten_tree_respects_depth_limit():
    tree = _make_tree(2, 4)
    shallow = flatten_tree(tree, 1)
    deep = flatten_tree(tree, 3)
    assert shallow.count("\n") < deep.count("\n")
    # Indentation grows by 2 spaces per depth.
    assert "    " in deep  # depth 2 lines have 4 spaces of indent
    assert "      " not in shallow  # no depth-3 lines


def test_trim_tree_shrinks_depth_until_under_budget():
    tree = _make_tree(3, 5)  # 3^5 ≈ 243 leaves — long output
    full = flatten_tree(tree, 5)
    assert len(full) > 500  # sanity: tree is large

    res = trim_tree(tree, start_depth=5, min_depth=1, max_chars=500)
    assert len(res["text"]) <= 500
    assert res["depth_used"] < 5
    assert res["depth_used"] >= 1


def test_trim_tree_truncates_when_even_min_depth_too_big():
    # Force a tree whose depth-1 serialisation alone exceeds the budget.
    tree = _make_tree(50, 1)
    res = trim_tree(tree, start_depth=1, min_depth=1, max_chars=100)
    assert res["truncated"] is True
    assert len(res["text"]) <= 200  # roughly within budget + truncation marker


def test_trim_tree_handles_empty():
    res = trim_tree(None, start_depth=5, min_depth=1, max_chars=100)
    assert res == {"text": "", "depth_used": 0, "truncated": False}


class _FakeOso:
    def __init__(self, *, structure: dict | None = None, sketch: str | None = None,
                 description: str | None = None, enabled: bool = True):
        self.enabled = enabled
        self._structure = structure
        self._sketch = sketch
        self._description = description

    async def get_structure(self, window_index: int | None = None) -> dict | None:
        return {"tree": self._structure} if self._structure is not None else None

    async def get_sketch(self, window_index: int | None = None) -> dict | None:
        return {"sketch": self._sketch} if self._sketch is not None else None

    async def get_description(self, window_index: int | None = None) -> dict | None:
        return {"description": self._description} if self._description is not None else None


def test_build_text_bundle_combined_cap_drops_tree_first():
    tree = _make_tree(2, 5)
    long_sketch = "S" * 200
    long_desc = "D" * 200
    oso = _FakeOso(structure=tree, sketch=long_sketch, description=long_desc)
    bundle = asyncio.run(build_text_bundle(
        oso,
        window_index=None,
        include_sketch=True,
        include_tree=True,
        tree_start_depth=5,
        tree_min_depth=1,
        tree_max_chars=2000,
        max_chars=600,
    ))
    assert bundle is not None
    total = len(bundle["description"]) + len(bundle["sketch"]) + len(bundle["tree_text"])
    assert total <= 600 + 50  # +slack for truncation marker
    assert bundle["description"] == long_desc  # description preserved
    assert bundle["sketch"] == long_sketch     # sketch preserved
    # Tree was the bulk; it must have been cut.
    assert "truncated" in bundle["tree_text"].lower() or len(bundle["tree_text"]) < len(flatten_tree(tree, 5))


def test_build_text_bundle_returns_none_when_disabled():
    oso = _FakeOso(enabled=False)
    bundle = asyncio.run(build_text_bundle(
        oso, window_index=None, include_sketch=True, include_tree=True,
        tree_start_depth=3, tree_min_depth=1, tree_max_chars=1000, max_chars=2000,
    ))
    assert bundle is None


def test_build_text_bundle_scope_label():
    tree = _make_tree(1, 1)
    oso = _FakeOso(structure=tree, sketch="x", description="y")
    win_bundle = asyncio.run(build_text_bundle(
        oso, window_index=2, include_sketch=True, include_tree=True,
        tree_start_depth=3, tree_min_depth=1, tree_max_chars=1000, max_chars=2000,
    ))
    screen_bundle = asyncio.run(build_text_bundle(
        oso, window_index=None, include_sketch=True, include_tree=True,
        tree_start_depth=3, tree_min_depth=1, tree_max_chars=1000, max_chars=2000,
    ))
    assert win_bundle["scope"] == "active_window"
    assert screen_bundle["scope"] == "screen"


# ---------------------------------------------------------------------------
# Strict gating: when screen_observer.enabled=false, no OSO text in context.
# ---------------------------------------------------------------------------

# Strings that must never appear in LLM-visible context when OSO is off.
# Use distinctive phrases — "OSO" alone matches noise like "those", "Microsoft".
_FORBIDDEN_WHEN_OFF = (
    "OSScreenObserver",
    "screen observer",
    "screen_observer",
    "describe_screen",
    "OS Screen Observer",
    "desktop_describe_screen",
)


class _StubRegistry:
    def __init__(self, caps: dict[str, Any]):
        self._backend_caps = caps
        self._backend = None
        self.schemas: list[dict] = []
        self._tools: dict[str, Any] = {}
    def add_tool(self, schema, handler):
        try:
            self._tools[schema["function"]["name"]] = handler
        except Exception:
            pass
    def list_tools(self) -> list[str]:
        return list(self._tools)
    def dispatch(self, *_a, **_k):
        raise NotImplementedError


class _StubClient:
    pass


def _build_system_prompt_via_agent(screen_observer_caps: dict[str, Any]) -> tuple[str, set[str]]:
    """Construct an Agent purely to inspect its prompt + tool registry."""
    from agent import Agent
    cfg = {
        "agent": {"controller": {"enabled": True}},
        "prompts_dir": "prompts",
        "screen_observer": {"text_observation": {"enabled": True}},
    }
    agent = Agent(_StubClient(), _StubRegistry(screen_observer_caps), cfg)
    return agent._system_prompt, set()


def test_system_prompt_omits_oso_when_disabled():
    prompt, _tools = _build_system_prompt_via_agent({})
    for needle in _FORBIDDEN_WHEN_OFF:
        assert needle.lower() not in prompt.lower(), (
            f"system prompt leaked OSO reference {needle!r} when OSO is disabled"
        )


def test_system_prompt_includes_oso_fragment_when_enabled():
    prompt, _tools = _build_system_prompt_via_agent({"screen_observer": True})
    # Fragment is loaded — should mention text observation guidance.
    assert "text observation" in prompt.lower() or "desktop_describe_screen" in prompt.lower()


def test_oso_text_enabled_flag_requires_both_capability_and_config():
    from agent import Agent

    # config off, cap on -> disabled
    a1 = Agent(_StubClient(), _StubRegistry({"screen_observer": True}),
               {"agent": {}, "prompts_dir": "prompts",
                "screen_observer": {"text_observation": {"enabled": False}}})
    assert a1._oso_text_enabled is False
    # config on, cap off -> disabled (no OSO knowledge leaks)
    a2 = Agent(_StubClient(), _StubRegistry({}),
               {"agent": {}, "prompts_dir": "prompts",
                "screen_observer": {"text_observation": {"enabled": True}}})
    assert a2._oso_text_enabled is False
    # both on -> enabled
    a3 = Agent(_StubClient(), _StubRegistry({"screen_observer": True}),
               {"agent": {}, "prompts_dir": "prompts",
                "screen_observer": {"text_observation": {"enabled": True}}})
    assert a3._oso_text_enabled is True
