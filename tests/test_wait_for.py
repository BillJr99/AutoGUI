"""Targeted async tests for wait_for() — pin the contract for each
target type plus the timeout/error edge cases without requiring a
real desktop backend."""

from __future__ import annotations

import asyncio
import time

import pytest

from wait_for import wait_for


class StubBackend:
    """Minimal backend stand-in that wait_for can poll.

    ``windows_seq`` is a list of window-list snapshots returned in
    order: each call to list_windows() pops the next entry (or
    re-uses the last one when the list is exhausted).  Same idea for
    element / text lookups.
    """

    def __init__(
        self,
        *,
        windows_seq=None,
        element_seq=None,
        text_seq=None,
        list_windows_error=None,
    ):
        self._windows = list(windows_seq or [[]])
        self._element = list(element_seq or [])
        self._text = list(text_seq or [])
        self._list_error = list_windows_error
        self.list_windows_calls = 0
        self.find_element_calls = 0
        self.find_text_calls = 0

    async def list_windows(self):
        self.list_windows_calls += 1
        if self._list_error:
            raise RuntimeError(self._list_error)
        if not self._windows:
            return {"windows": []}
        # Pop until last; sticky behaviour mimics steady state once a
        # window has appeared.
        wins = self._windows.pop(0) if len(self._windows) > 1 else self._windows[0]
        return {"windows": wins}

    async def find_element(self, *, name=None, control_type=None,
                           window_title=None, index=0):
        self.find_element_calls += 1
        if not self._element:
            return {"error": "not found"}
        return self._element.pop(0) if len(self._element) > 1 else self._element[0]

    async def find_text_on_screen(self, text, occurrence=0):
        self.find_text_calls += 1
        if not self._text:
            return {"found": False}
        return self._text.pop(0) if len(self._text) > 1 else self._text[0]


@pytest.mark.asyncio
async def test_wait_for_requires_at_least_one_target():
    backend = StubBackend()
    result = await wait_for(backend=backend, timeout=0.5)
    assert result["found"] is False
    # The empty-targets early return must include the same shape as
    # the timeout/success paths so callers can branch on `target`,
    # `elapsed`, etc. without a KeyError.
    assert result["target"] is None
    assert result["elapsed"] == 0.0
    assert "wait_for requires" in result["error"]


@pytest.mark.asyncio
async def test_wait_for_window_title_match_on_first_poll():
    backend = StubBackend(windows_seq=[[
        {"id": "1", "title": "Notepad - foo.txt", "app": "notepad"},
    ]])
    result = await wait_for(
        backend=backend, window_title="Notepad", timeout=2.0,
    )
    assert result["found"] is True
    assert result["target"] == "window_title"
    assert result["observation"]["title"].startswith("Notepad")


@pytest.mark.asyncio
async def test_wait_for_window_title_appears_after_a_poll():
    """The window list returns empty first, then the target on the
    second poll.  wait_for must keep polling until it appears."""
    backend = StubBackend(windows_seq=[
        [],
        [{"id": "9", "title": "Slow App"}],
    ])
    result = await wait_for(
        backend=backend, window_title="Slow App",
        timeout=3.0, poll_interval=0.2,
    )
    assert result["found"] is True
    assert result["target"] == "window_title"
    assert backend.list_windows_calls >= 2


@pytest.mark.asyncio
async def test_wait_for_window_id_exact_match():
    backend = StubBackend(windows_seq=[[
        {"id": "abc", "title": "x"},
        {"id": "xyz", "title": "y"},
    ]])
    result = await wait_for(backend=backend, window_id="xyz", timeout=1.0)
    assert result["found"] is True
    assert result["target"] == "window_id"
    assert result["observation"]["id"] == "xyz"


@pytest.mark.asyncio
async def test_wait_for_element_match():
    backend = StubBackend(element_seq=[
        {"name": "Save", "rect": {"x": 1, "y": 2, "width": 3, "height": 4}},
    ])
    result = await wait_for(backend=backend, element_name="Save", timeout=1.0)
    assert result["found"] is True
    assert result["target"] == "element_name"


@pytest.mark.asyncio
async def test_wait_for_timeout_returns_full_shape():
    backend = StubBackend(windows_seq=[[]])
    start = time.monotonic()
    result = await wait_for(
        backend=backend, window_title="never",
        timeout=0.6, poll_interval=0.2,
    )
    elapsed = time.monotonic() - start
    assert result["found"] is False
    assert result["target"] is None
    assert "elapsed" in result and result["elapsed"] >= 0.5
    assert result["timeout"] == 0.6
    assert result["targets"] == {"window_title": "never"}
    assert "windows" in result["last_observation"]
    # Sanity check the wall-time clamp works — should NOT have run a
    # full second past the deadline.
    assert elapsed < 1.5


@pytest.mark.asyncio
async def test_wait_for_clamps_below_floor_to_minimum_poll():
    """timeout below the 0.5s floor still allows at least one poll."""
    backend = StubBackend(windows_seq=[[]])
    result = await wait_for(
        backend=backend, window_title="x", timeout=0.0,
    )
    # Still returns the timeout shape; wall-time roughly equals floor.
    assert result["found"] is False
    assert result.get("timeout") == 0.5
    assert backend.list_windows_calls >= 1


@pytest.mark.asyncio
async def test_wait_for_swallows_backend_errors():
    """A raising list_windows must not propagate; wait_for keeps
    polling and records the error in last_observation."""
    backend = StubBackend(list_windows_error="atspi crashed")
    result = await wait_for(
        backend=backend, window_title="x",
        timeout=0.6, poll_interval=0.2,
    )
    assert result["found"] is False
    assert "window_error" in result["last_observation"]
    assert "atspi crashed" in result["last_observation"]["window_error"]
