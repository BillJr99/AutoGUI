"""Watchdog signature + stuck detection."""

from __future__ import annotations

from watchdog import Watchdog


def test_repeated_signature_flags_stuck():
    wd = Watchdog(stall_threshold=3)
    sig = wd.signature(windows=[], active_window={}, pending_tool="x", pending_args={})
    assert wd.observe(sig).stuck is False
    assert wd.observe(sig).stuck is False
    assert wd.observe(sig).stuck is True


def test_changing_signature_resets_repeats():
    wd = Watchdog(stall_threshold=3)
    a = wd.signature(windows=[], active_window={}, pending_tool="x", pending_args={})
    b = wd.signature(windows=[], active_window={}, pending_tool="y", pending_args={})
    wd.observe(a)
    wd.observe(a)
    status = wd.observe(b)  # different
    assert status.stuck is False
    assert status.repeats == 1


def test_threshold_zero_disables():
    wd = Watchdog(stall_threshold=0)
    sig = wd.signature(windows=[], active_window={}, pending_tool="x", pending_args={})
    for _ in range(5):
        assert wd.observe(sig).stuck is False


def test_signature_is_stable_for_equivalent_state():
    wd = Watchdog(stall_threshold=3)
    a = wd.signature(
        windows=[{"id": "1", "title": "A", "app": "edge"},
                 {"id": "2", "title": "B", "app": "vscode"}],
        active_window={"window": {"app": "edge", "title": "A"}},
        pending_tool="desktop_click_element",
        pending_args={"name": "Save"},
    )
    b = wd.signature(
        windows=[{"id": "2", "title": "B", "app": "vscode"},
                 {"id": "1", "title": "A", "app": "edge"}],   # reordered
        active_window={"window": {"app": "edge", "title": "A"}},
        pending_tool="desktop_click_element",
        pending_args={"name": "Save"},
    )
    assert a == b


def test_reset_clears_history():
    wd = Watchdog(stall_threshold=2)
    sig = wd.signature(windows=[], active_window={}, pending_tool="x", pending_args={})
    wd.observe(sig)
    wd.observe(sig)
    wd.reset()
    assert wd.observe(sig).stuck is False
