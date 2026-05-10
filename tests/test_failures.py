"""Failure classifier coverage."""

from __future__ import annotations

from failures import FailureClass, RecoveryAction, classify, escalate_action


def test_permission_message():
    v = classify(tool_name="shell_run", error_message="permission denied")
    assert v.cls == FailureClass.PERMISSION
    assert v.action == RecoveryAction.ESCALATE


def test_missing_element_message():
    v = classify(tool_name="desktop_click_element",
                 error_message="No element matching 'Save' found")
    assert v.cls == FailureClass.MISSING_ELEMENT
    assert v.action == RecoveryAction.REPLAN


def test_predicate_failed_short_circuits():
    v = classify(tool_name="(none)", error_message="ok",
                 predicate_failed=True)
    assert v.cls == FailureClass.PREDICATE_NOT_MET
    assert v.action == RecoveryAction.REPLAN


def test_timeout_treated_as_app_not_ready():
    v = classify(tool_name="desktop_click", error_message="operation timed out")
    assert v.cls == FailureClass.APP_NOT_READY


def test_shell_nonzero_exit_when_no_pattern_match():
    v = classify(
        tool_name="shell_run", error_message="quux",
        result={"exit_code": 2},
    )
    assert v.cls == FailureClass.TRANSIENT_IO


def test_unknown_falls_back_to_retry():
    v = classify(tool_name="foo", error_message="something else")
    assert v.cls == FailureClass.UNKNOWN
    assert v.action == RecoveryAction.RETRY


def test_escalate_promotes_after_retries():
    v = classify(tool_name="foo", error_message="something else")
    promoted = escalate_action(v, retry_count=3, max_retries=2)
    assert promoted == RecoveryAction.REPLAN


def test_escalate_keeps_escalate_unchanged():
    v = classify(tool_name="shell_run", error_message="permission denied")
    assert escalate_action(v, retry_count=0, max_retries=5) == RecoveryAction.ESCALATE
