"""
failures.py — Structured failure classification.

Tool failures aren't all alike: a transient I/O blip wants a retry-with-wait,
a missing UI element wants a different lookup strategy, a permission denial
should escalate to the user, and a predicate-not-met after a successful
action means the model's mental model of the screen is wrong and the plan
should be revisited.

This module centralises the regex/keyword classifier the agent loop uses
to map an error message + tool name + result dict to one of a small set of
``FailureClass`` enum values, plus a recommended ``Action`` (retry / wait /
replan / escalate / abort).

The classifier is deliberately conservative — when nothing matches it
returns ``UNKNOWN`` so the existing retry-injection path runs unchanged.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Any


class FailureClass(str, Enum):
    """High-level error category used to choose a recovery strategy."""

    TRANSIENT_IO = "transient_io"       # network blip, EBUSY, temporary lock
    APP_NOT_READY = "app_not_ready"     # window not yet drawn, control not visible
    MISSING_ELEMENT = "missing_element" # selector / a11y name didn't resolve
    PERMISSION = "permission"           # OS or app refused the action
    PREDICATE_NOT_MET = "predicate_not_met"  # action fired but expected outcome absent
    USER_INPUT_NEEDED = "user_input_needed"  # captcha, 2FA, license dialog, etc.
    UNKNOWN = "unknown"


class RecoveryAction(str, Enum):
    """What the controller should do next given a classified failure."""

    RETRY = "retry"            # immediate retry; same args
    WAIT_AND_RETRY = "wait_and_retry"  # short sleep, then retry
    REPLAN = "replan"          # ask the planner to revise the plan
    ESCALATE = "escalate"      # checkpoint to the user
    ABORT = "abort"            # the task cannot proceed


@dataclass
class FailureVerdict:
    cls: FailureClass
    action: RecoveryAction
    reason: str
    wait_seconds: float = 0.0


# ---------------------------------------------------------------------------
# Pattern tables
# ---------------------------------------------------------------------------

# Order matters — earlier patterns win.  Keep specific patterns above
# generic ones.  Patterns are matched against the lowercased error message.
_PATTERNS: list[tuple[FailureClass, str]] = [
    # Permission / refused — surface to user.
    (FailureClass.PERMISSION,
     r"\b(permission|access)\s+(denied|refused)\b|\beacces\b|\beperm\b"
     r"|\boperation not permitted\b|\brun as administrator\b"
     r"|\bauthorization (?:required|failed)\b"),

    # Captcha / 2FA / consent — needs the human.
    (FailureClass.USER_INPUT_NEEDED,
     r"\b(captcha|recaptcha|two[ -]?factor|2fa|verification code"
     r"|sign in|signin|login required|consent|terms of service)\b"),

    # Missing element / selector failures.
    (FailureClass.MISSING_ELEMENT,
     r"\b(no (?:such )?element|selector .* (?:not found|did not match)|element not found"
     r"|no marks? with id|find_element .* (?:no|none)|atspi.* not found"
     r"|no visible text matching|no window matching|no candidates found)\b"),

    # App-not-ready / timing.  Window appeared but isn't ready, control
    # not yet drawn, page still loading.
    (FailureClass.APP_NOT_READY,
     r"\b(?:not\s+(?:yet\s+)?(?:visible|loaded|ready|interactable)"
     r"|stale element|element is detached|target closed"
     r"|loading|waiting for .* to be ready|navigation interrupted)\b"),

    # Transient I/O / network / temporary OS errors.
    (FailureClass.TRANSIENT_IO,
     r"\b(econnreset|econnrefused|etimedout|ehostunreach|enetunreach"
     r"|temporarily unavailable|resource busy|ebusy|connection (?:reset|aborted|refused)"
     r"|broken pipe|read timed? out|gateway timeout|503|502|504)\b"),
]


# Recovery policy per class — controllers may tune wait times via config.
_DEFAULT_POLICY: dict[FailureClass, tuple[RecoveryAction, float]] = {
    FailureClass.TRANSIENT_IO: (RecoveryAction.WAIT_AND_RETRY, 2.0),
    FailureClass.APP_NOT_READY: (RecoveryAction.WAIT_AND_RETRY, 1.5),
    FailureClass.MISSING_ELEMENT: (RecoveryAction.REPLAN, 0.0),
    FailureClass.PERMISSION: (RecoveryAction.ESCALATE, 0.0),
    FailureClass.USER_INPUT_NEEDED: (RecoveryAction.ESCALATE, 0.0),
    FailureClass.PREDICATE_NOT_MET: (RecoveryAction.REPLAN, 0.0),
    FailureClass.UNKNOWN: (RecoveryAction.RETRY, 0.0),
}


def classify(
    *,
    tool_name: str,
    error_message: str,
    result: dict[str, Any] | None = None,
    predicate_failed: bool = False,
) -> FailureVerdict:
    """
    Map a tool failure to a FailureClass + RecoveryAction.

    Parameters
    ----------
    tool_name : str
        The tool that failed.
    error_message : str
        Free-text error from the tool result (``result["error"]``,
        ``stderr``, etc.).
    result : dict, optional
        The full tool result so the classifier can also key off
        ``exit_code`` / ``timed_out`` / ``found`` flags.
    predicate_failed : bool
        Set to True when the tool reported success but a post-condition
        check (e.g. expected window title) didn't hold.  Bypasses pattern
        matching.
    """
    if predicate_failed:
        action, wait = _DEFAULT_POLICY[FailureClass.PREDICATE_NOT_MET]
        return FailureVerdict(
            cls=FailureClass.PREDICATE_NOT_MET,
            action=action,
            reason="post-condition predicate did not hold",
            wait_seconds=wait,
        )

    msg = (error_message or "").lower()

    # Timeouts: always treat as app-not-ready (best heuristic) regardless
    # of which tool emitted them.
    if (result and result.get("timed_out")) or "timed out" in msg or "timeout" in msg:
        action, wait = _DEFAULT_POLICY[FailureClass.APP_NOT_READY]
        return FailureVerdict(
            cls=FailureClass.APP_NOT_READY,
            action=action,
            reason="tool reported timeout",
            wait_seconds=wait,
        )

    for cls, pat in _PATTERNS:
        if re.search(pat, msg):
            action, wait = _DEFAULT_POLICY[cls]
            return FailureVerdict(
                cls=cls, action=action,
                reason=f"matched {cls.value} pattern",
                wait_seconds=wait,
            )

    # Generic shell exit-code failure with no useful stderr — treat as
    # transient on first encounter, replan on repeat (the controller is
    # expected to track per-step retry counts).
    if tool_name == "shell_run" and result and result.get("exit_code") not in (None, 0):
        action, wait = _DEFAULT_POLICY[FailureClass.TRANSIENT_IO]
        return FailureVerdict(
            cls=FailureClass.TRANSIENT_IO,
            action=action,
            reason=f"non-zero exit code ({result.get('exit_code')})",
            wait_seconds=wait,
        )

    action, wait = _DEFAULT_POLICY[FailureClass.UNKNOWN]
    return FailureVerdict(
        cls=FailureClass.UNKNOWN, action=action,
        reason="no pattern matched",
        wait_seconds=wait,
    )


def escalate_action(verdict: FailureVerdict, *, retry_count: int, max_retries: int) -> RecoveryAction:
    """
    Promote a verdict's action when retries are exhausted.

    The controller calls this after each failed retry: a tool that keeps
    failing transiently for ``max_retries`` rounds should not retry forever
    — promote to REPLAN, then to ESCALATE.
    """
    if verdict.action in (RecoveryAction.ESCALATE, RecoveryAction.ABORT):
        return verdict.action
    if retry_count < max_retries:
        return verdict.action
    if verdict.action in (RecoveryAction.RETRY, RecoveryAction.WAIT_AND_RETRY):
        return RecoveryAction.REPLAN
    return RecoveryAction.ESCALATE
