"""
watchdog.py — No-progress detector for the controller loop.

The iteration ceiling stops a runaway loop eventually, but doesn't
notice when an agent is *spinning*: same window, same active window,
same tool args, every iteration.  The watchdog hashes a compact
``(state + action)`` signature each iteration and flags ``stuck=True``
once the same signature recurs ``stall_threshold`` times in a row.

The controller treats stuck steps as ``predicate_failed`` so they
trigger replan / escalate via the standard failure-classification
path — no special-case logic.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections import deque
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class WatchdogStatus:
    iteration: int
    signature: str
    repeats: int
    stuck: bool
    note: str = ""


@dataclass
class Watchdog:
    """
    Tracks repeated state+action signatures.

    ``stall_threshold`` controls when the watchdog flags ``stuck=True``.
    A value of 3 means: the same signature observed three iterations
    in a row.  Set 0 to disable.
    """
    stall_threshold: int = 3
    history_size: int = 8
    _history: deque = field(default_factory=lambda: deque(maxlen=8))
    _iteration: int = 0

    def __post_init__(self):
        self._history = deque(maxlen=max(self.stall_threshold + 2, self.history_size))

    @staticmethod
    def signature(*, windows: Any, active_window: Any,
                  pending_tool: str, pending_args: Any) -> str:
        """
        Canonical signature for one iteration.

        Window list is reduced to a sorted (id, title, app) tuple so
        cosmetic ordering doesn't affect equality.  The pending tool +
        normalised args go into the signature so a model that keeps
        proposing the *same* action against unchanged state is stuck,
        but a model that varies its approach against unchanged state
        is not.
        """
        win_seq = []
        if isinstance(windows, list):
            for w in windows:
                if not isinstance(w, dict):
                    continue
                win_seq.append((str(w.get("id", "")),
                                str(w.get("title", ""))[:80],
                                str(w.get("app", ""))))
        win_seq.sort()
        active = ""
        if isinstance(active_window, dict):
            w = active_window.get("window") or active_window
            if isinstance(w, dict):
                active = (str(w.get("title", ""))[:80] + "|"
                          + str(w.get("app", "")))
        # Hash the FULL canonical args — truncating before hashing means
        # two calls that differ only past the cutoff (long selectors,
        # file paths, prompts) collapse to the same signature and the
        # watchdog falsely reports the step as stuck.  The cap below is
        # generous enough to bound memory while still distinguishing
        # realistic argument payloads.
        try:
            args_str = json.dumps(pending_args or {}, sort_keys=True, default=str)
        except (TypeError, ValueError):
            args_str = repr(pending_args)
        # Defensive ceiling: cap at ~64 KiB so a pathological payload
        # can't blow up sha1 work without bound.  Real tool args are
        # orders of magnitude smaller; for those this is a no-op.
        if len(args_str) > 65536:
            args_str = args_str[:65536]
        payload = json.dumps({
            "w": win_seq[:50],
            "a": active,
            "t": pending_tool,
            "g": args_str,
        }, sort_keys=True)
        return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]

    def observe(self, sig: str) -> WatchdogStatus:
        self._iteration += 1
        self._history.append(sig)
        # How many consecutive trailing entries match this signature?
        repeats = 0
        for s in reversed(self._history):
            if s == sig:
                repeats += 1
            else:
                break
        stuck = self.stall_threshold > 0 and repeats >= self.stall_threshold
        note = ""
        if stuck:
            note = (f"signature {sig} repeated {repeats} iteration(s); "
                    "controller will treat this step as blocked.")
            logger.warning("[watchdog] %s", note)
        return WatchdogStatus(
            iteration=self._iteration,
            signature=sig,
            repeats=repeats,
            stuck=stuck,
            note=note,
        )

    def reset(self) -> None:
        self._history.clear()
        self._iteration = 0


__all__ = ["Watchdog", "WatchdogStatus"]
