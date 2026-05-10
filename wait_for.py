"""
wait_for.py — `desktop_wait_for` polling primitive.

Cold-start UI flake is the single biggest source of wasted iterations:
the executor calls ``desktop_launch`` and immediately tries to click
something that hasn't been drawn yet.  ``desktop_wait_for`` lets it ask
the system "wake me when X is true" instead, so a slow Word load or a
half-loaded browser tab doesn't burn three iterations of failed clicks.

Targets supported
-----------------
* ``window_title=<substring>``  — any visible window matches
* ``element_name=<name>``       — a11y element resolves
* ``text=<visible label>``       — OCR / a11y text match (no click)
* ``window_id=<id>``            — specific window handle exists

Polling cadence is fixed at 0.5 s (cheap calls) with a configurable
timeout (default 15 s).  Returns ``{found, target, elapsed, ...}`` so
the model can branch on success.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any


async def wait_for(
    *,
    backend,
    window_title: str = "",
    element_name: str = "",
    text: str = "",
    window_id: str = "",
    timeout: float = 15.0,
    poll_interval: float = 0.5,
) -> dict[str, Any]:
    """
    Poll until any of the supplied targets is observable, or the timeout
    elapses.  At least one target MUST be supplied.

    Each target is checked in a forgiving way: errors during a poll
    (e.g. AT-SPI hiccups) are swallowed and the loop tries again.
    """
    targets = {
        "window_title": window_title,
        "element_name": element_name,
        "text": text,
        "window_id": window_id,
    }
    targets = {k: v for k, v in targets.items() if v}
    if not targets:
        return {
            "error": "wait_for requires at least one of "
                     "window_title, element_name, text, window_id.",
            "found": False,
        }

    # Normalize timeout once so the loop-exit comparison and the sleep
    # math both use the same effective ceiling.  A caller-supplied
    # timeout below the floor is silently raised so we still poll at
    # least once before giving up.
    timeout = max(0.5, float(timeout))
    poll_interval = max(0.1, float(poll_interval))
    start = time.monotonic()
    deadline = start + timeout

    last_observation: dict[str, Any] = {}

    while True:
        elapsed = time.monotonic() - start

        try:
            if window_title or window_id:
                listing = await backend.list_windows()
                wins = listing.get("windows") if isinstance(listing, dict) else listing
                wins = wins or []
                for w in wins:
                    title = str(w.get("title", "")) or ""
                    wid = str(w.get("id", "")) or ""
                    if window_title and window_title.lower() in title.lower():
                        return _ok(start, "window_title", w)
                    if window_id and window_id == wid:
                        return _ok(start, "window_id", w)
                last_observation["windows"] = len(wins)
        except Exception as e:
            last_observation["window_error"] = str(e)

        if element_name:
            try:
                if hasattr(backend, "find_element"):
                    el = await backend.find_element(name=element_name)
                    if el and not el.get("error") and el.get("rect"):
                        return _ok(start, "element_name", el)
            except Exception as e:
                last_observation["element_error"] = str(e)

        if text:
            try:
                if hasattr(backend, "find_text_on_screen"):
                    found = await backend.find_text_on_screen(text, 0)
                    if found and found.get("found"):
                        return _ok(start, "text", found)
            except Exception as e:
                last_observation["text_error"] = str(e)

        if elapsed >= timeout:
            return {
                "found": False,
                "target": None,
                "elapsed": round(elapsed, 3),
                "timeout": timeout,
                "targets": targets,
                "last_observation": last_observation,
            }
        # Sleep until next poll, but never past the deadline.
        await asyncio.sleep(min(poll_interval, max(0.05, deadline - time.monotonic())))


def _ok(start: float, kind: str, observation: Any) -> dict[str, Any]:
    return {
        "found": True,
        "target": kind,
        "elapsed": round(time.monotonic() - start, 3),
        "observation": observation,
    }
