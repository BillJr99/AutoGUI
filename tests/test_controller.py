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


def test_step_outcome_parses_done_marker():
    verdict, reason = parse_step_outcome("Some prose\nSTEP_DONE: editor open")
    assert verdict == StepVerdict.DONE
    assert reason == "editor open"


def test_step_outcome_parses_blocked_marker():
    verdict, reason = parse_step_outcome("STEP_BLOCKED: captcha appeared")
    assert verdict == StepVerdict.BLOCKED
    assert "captcha" in reason


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
