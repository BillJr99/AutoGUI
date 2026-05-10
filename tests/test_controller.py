"""
test_controller.py — Plan parsing, status transitions, replan merge.

These tests exercise the controller helpers without an LLM or backend.
"""

from __future__ import annotations

from controller import (
    Plan,
    PlanStep,
    StepStatus,
    StepVerdict,
    merge_revised_plan,
    parse_plan,
    parse_step_outcome,
)


def test_parse_typed_plan_round_trip():
    raw = (
        '{"steps":[{"id":"s1","goal":"open editor","expected":"editor visible",'
        '"predicate":{"kind":"window_title_contains","value":"Notepad"},'
        '"tools_hint":["desktop_launch"],"depends_on":[],"risks":["app slow"]},'
        '{"id":"s2","goal":"type text","depends_on":["s1"]}]}'
    )
    plan = parse_plan(raw)
    assert len(plan.steps) == 2
    assert plan.steps[0].predicate["kind"] == "window_title_contains"
    assert plan.steps[0].risks == ["app slow"]
    assert plan.steps[0].tools_hint == ["desktop_launch"]


def test_parse_plan_falls_back_to_numbered_list():
    plan = parse_plan("1. step one\n2. step two\n")
    assert len(plan.steps) == 2
    assert plan.steps[0].id == "s1"
    assert plan.steps[1].goal == "step two"


def test_next_runnable_respects_dependencies():
    plan = Plan(steps=[
        PlanStep(id="a", goal=""),
        PlanStep(id="b", goal="", depends_on=["a"]),
    ])
    first = plan.next_runnable()
    assert first.id == "a"
    first.status = StepStatus.DONE
    assert plan.next_runnable().id == "b"


def test_next_runnable_returns_none_when_blocked():
    plan = Plan(steps=[
        PlanStep(id="a", goal="", status=StepStatus.FAILED),
        PlanStep(id="b", goal="", depends_on=["a"]),
    ])
    assert plan.next_runnable() is None


def test_merge_revised_preserves_done_steps():
    current = Plan(steps=[
        PlanStep(id="s1", goal="", status=StepStatus.DONE),
        PlanStep(id="s2", goal="", status=StepStatus.PENDING),
    ])
    revised = Plan(steps=[
        PlanStep(id="s1", goal="re-attempt"),
        PlanStep(id="s3", goal="new step"),
    ])
    merged = merge_revised_plan(current, revised)
    assert merged.revision == 1
    statuses = {s.id: s.status for s in merged.steps}
    assert statuses["s1"] == StepStatus.DONE   # preserved
    assert "s3" in statuses
    # s2 was dropped from revised but wasn't DONE so it's not preserved.
    assert "s2" not in statuses


def test_merge_revised_carries_preflight():
    current = Plan(
        steps=[PlanStep(id="s1", goal="")],
        preflight=[{"kind": "app", "target": "vim"}],
    )
    # Revised plan without preflight: current's checks must survive.
    revised = Plan(steps=[PlanStep(id="s1", goal="re-attempt")])
    merged = merge_revised_plan(current, revised)
    assert merged.preflight == [{"kind": "app", "target": "vim"}]

    # Revised plan with its own preflight: revised wins.
    revised2 = Plan(
        steps=[PlanStep(id="s1", goal="x")],
        preflight=[{"kind": "url", "target": "https://example.com"}],
    )
    merged2 = merge_revised_plan(current, revised2)
    assert merged2.preflight == [{"kind": "url", "target": "https://example.com"}]


def test_merge_revised_does_not_preserve_failed_steps():
    """The whole point of a replan is to re-attempt FAILED steps with a new
    approach; FAILED status must NOT be carried over from current."""
    current = Plan(steps=[
        PlanStep(id="s1", goal="old", status=StepStatus.FAILED, last_error="oops"),
    ])
    revised = Plan(steps=[PlanStep(id="s1", goal="new approach")])
    merged = merge_revised_plan(current, revised)
    only = merged.steps[0]
    assert only.status == StepStatus.PENDING
    assert only.goal == "new approach"
    assert only.last_error == ""


def test_step_outcome_parses_done_marker():
    verdict, reason = parse_step_outcome("Some prose\nSTEP_DONE: editor open")
    assert verdict == StepVerdict.DONE
    assert reason == "editor open"


def test_step_outcome_parses_blocked_marker():
    verdict, reason = parse_step_outcome("STEP_BLOCKED: captcha appeared")
    assert verdict == StepVerdict.BLOCKED
    assert "captcha" in reason


def test_step_outcome_no_marker_returns_blocked():
    """A final assistant message with NO STEP_DONE / STEP_BLOCKED marker
    is a protocol violation — the model narrated instead of acting.  The
    parser must NOT treat this as implicit success: that lets the model
    coast through every step without invoking tools.  BLOCKED routes
    through the retry / replan path which re-prompts with the protocol
    reminder so the next attempt actually executes."""
    verdict, reason = parse_step_outcome(
        "I'll now execute the plan step by step.",
    )
    assert verdict == StepVerdict.BLOCKED
    # The narrative text is preserved in the reason so the controller
    # can show the user what the model said instead of acting.
    assert "no STEP_DONE" in reason
    assert "execute the plan" in reason


def test_step_outcome_empty_text_returns_failed():
    verdict, reason = parse_step_outcome("")
    assert verdict == StepVerdict.FAILED
    assert reason


def test_to_dict_round_trip_preserves_predicate_and_risks():
    plan = Plan(steps=[PlanStep(
        id="s1", goal="g", expected="e",
        predicate={"kind": "file_exists", "path": "/tmp/x"},
        risks=["one", "two"],
    )])
    plan.preflight = [{"kind": "app", "target": "vim"}]
    data = plan.to_dict()
    plan2 = Plan.from_dict(data)
    assert plan2.steps[0].predicate["path"] == "/tmp/x"
    assert plan2.steps[0].risks == ["one", "two"]
    assert plan2.preflight == [{"kind": "app", "target": "vim"}]


def test_to_public_drops_empty_predicate():
    """Steps without a predicate must NOT serialize {predicate: {}};
    that's noise on the wire and is truthy on the TS side, where it
    would cause spurious renderForPrompt output and unnecessary
    check_predicate calls from the model."""
    step = PlanStep(id="s1", goal="g")  # default predicate is {}
    serialized = step.to_public()
    assert "predicate" not in serialized

    step_with = PlanStep(
        id="s2", goal="g",
        predicate={"kind": "file_exists", "path": "/tmp/x"},
    )
    assert step_with.to_public()["predicate"] == {"kind": "file_exists", "path": "/tmp/x"}
