"""
controller.py — Typed plan + sub-executor controller.

Replaces the old "flat numbered string plan" with a structured ``Plan``
that survives the entire task lifecycle: each step has an id, goal text,
expected post-condition, optional tools hint, dependencies, and a status
the executor mutates as it works.

The ``Controller`` owns the plan and dispatches step-scoped executor
runs via the existing agent loop.  It does NOT subclass Agent; instead
it composes one — calling ``Agent.run()`` repeatedly with carefully
scoped per-step prompts so each step keeps a small focused history
rather than dragging the whole task into a single ever-growing
conversation.

The controller is intentionally optional: when ``agent.controller.enabled``
is false the agent runs in legacy single-loop mode unchanged.

Public surface
--------------

    Plan                — typed plan object (json-serialisable)
    PlanStep            — one step with status + expected outcome
    StepStatus          — pending | running | done | failed | skipped | blocked
    Controller          — orchestrates step-by-step execution

The plan is produced by ``Planner.plan_typed`` (a thin wrapper over the
existing planner that asks for JSON output) and persisted via the
``ProgressStore`` so a crash or context reset can resume mid-task.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


class StepStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"
    BLOCKED = "blocked"


@dataclass
class PlanStep:
    id: str
    goal: str
    expected: str = ""               # human-readable post-condition
    predicate: dict = field(default_factory=dict)   # typed predicate (predicates.py)
    tools_hint: list[str] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)   # pre-mortem risk notes
    status: StepStatus = StepStatus.PENDING
    attempts: int = 0
    last_error: str = ""
    artifacts: list[str] = field(default_factory=list)  # produced artifact ids
    notes: str = ""

    def to_public(self) -> dict[str, Any]:
        d = asdict(self)
        d["status"] = self.status.value
        return d


@dataclass
class Plan:
    steps: list[PlanStep] = field(default_factory=list)
    created: float = field(default_factory=time.time)
    revision: int = 0
    # Explicit preflight checks the planner / critique pass attached.  The
    # controller passes these to ``preflight.run_preflight`` before the
    # first step executes; failures abort the task with a structured report.
    preflight: list[dict] = field(default_factory=list)

    # ------------------------------------------------------------------
    # Lookup helpers
    # ------------------------------------------------------------------

    def by_id(self, step_id: str) -> PlanStep | None:
        for s in self.steps:
            if s.id == step_id:
                return s
        return None

    def next_runnable(self) -> PlanStep | None:
        """Return the first PENDING step whose dependencies are all DONE."""
        done_ids = {s.id for s in self.steps if s.status == StepStatus.DONE}
        for s in self.steps:
            if s.status != StepStatus.PENDING:
                continue
            if all(d in done_ids for d in s.depends_on):
                return s
        return None

    def all_terminal(self) -> bool:
        return all(s.status in (StepStatus.DONE, StepStatus.SKIPPED, StepStatus.FAILED, StepStatus.BLOCKED)
                   for s in self.steps)

    def progress_summary(self) -> str:
        counts: dict[str, int] = {}
        for s in self.steps:
            counts[s.status.value] = counts.get(s.status.value, 0) + 1
        return ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))

    # ------------------------------------------------------------------
    # Serialisation
    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "steps": [s.to_public() for s in self.steps],
            "created": self.created,
            "revision": self.revision,
            "preflight": list(self.preflight),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Plan":
        steps_data = data.get("steps") or []
        steps = []
        for sd in steps_data:
            try:
                status = StepStatus(sd.get("status", "pending"))
            except ValueError:
                status = StepStatus.PENDING
            pred = sd.get("predicate")
            if not isinstance(pred, dict):
                pred = {}
            steps.append(PlanStep(
                id=str(sd.get("id", "")),
                goal=str(sd.get("goal", "")),
                expected=str(sd.get("expected", "")),
                predicate=dict(pred),
                tools_hint=list(sd.get("tools_hint") or []),
                depends_on=list(sd.get("depends_on") or []),
                risks=[str(r) for r in (sd.get("risks") or [])],
                status=status,
                attempts=int(sd.get("attempts", 0)),
                last_error=str(sd.get("last_error", "")),
                artifacts=list(sd.get("artifacts") or []),
                notes=str(sd.get("notes", "")),
            ))
        preflight_raw = data.get("preflight") or []
        preflight = [p for p in preflight_raw if isinstance(p, dict)]
        return cls(
            steps=steps,
            created=float(data.get("created") or time.time()),
            revision=int(data.get("revision") or 0),
            preflight=preflight,
        )

    def render_for_prompt(self) -> str:
        """Render the plan as a human-readable block for the LLM context."""
        lines = []
        for i, s in enumerate(self.steps, 1):
            marker = {
                StepStatus.DONE: "[x]",
                StepStatus.RUNNING: "[~]",
                StepStatus.FAILED: "[!]",
                StepStatus.SKIPPED: "[-]",
                StepStatus.BLOCKED: "[#]",
                StepStatus.PENDING: "[ ]",
            }[s.status]
            head = f"{marker} {i}. ({s.id}) {s.goal}"
            extras = []
            if s.expected:
                extras.append(f"expected: {s.expected}")
            if s.predicate:
                from predicates import render as _render_pred
                extras.append(f"predicate: {_render_pred(s.predicate)}")
            if s.tools_hint:
                extras.append(f"tools: {', '.join(s.tools_hint)}")
            if s.depends_on:
                extras.append(f"depends: {', '.join(s.depends_on)}")
            if s.risks:
                extras.append("risks: " + "; ".join(s.risks[:3]))
            if s.last_error and s.status == StepStatus.FAILED:
                extras.append(f"last_error: {s.last_error[:80]}")
            if extras:
                head += "\n     " + "; ".join(extras)
            lines.append(head)
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Plan parsing — accepts either a JSON object or the legacy numbered string.
# ---------------------------------------------------------------------------

def parse_plan(raw: str) -> Plan:
    """
    Parse a plan from either:
      * A JSON document with a top-level ``steps`` array, OR
      * A numbered list (legacy planner output) that we promote to typed
        steps with auto-generated ids.

    The fallback path means an old planner that ignores the typed-plan
    instructions still produces a working plan.
    """
    raw = (raw or "").strip()
    if not raw:
        return Plan()

    # Strip code fences if present.
    raw = re.sub(r"^```[a-z]*\n?", "", raw).rstrip("`").strip()

    # Try JSON first.
    try:
        data = json.loads(raw)
        if isinstance(data, dict) and "steps" in data:
            return Plan.from_dict(data)
        if isinstance(data, list):
            return Plan.from_dict({"steps": data})
    except json.JSONDecodeError:
        pass

    # Legacy numbered-list fallback.
    steps: list[PlanStep] = []
    for i, line in enumerate(raw.splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        m = re.match(r"^\s*(?:\d+[.)]|[-*])\s+(.*)$", line)
        text = m.group(1) if m else line
        steps.append(PlanStep(id=f"s{i}", goal=text))
    return Plan(steps=steps)


# ---------------------------------------------------------------------------
# Step prompt construction
# ---------------------------------------------------------------------------

def build_step_prompt(
    *,
    user_input: str,
    plan: Plan,
    step: PlanStep,
    artifact_index_summary: str = "",
    completed_actions_summary: str = "",
) -> str:
    """
    Compose the per-step prompt the controller sends to the executor.

    The executor sees the full plan (so it can adapt) but is told to
    focus on the current step.  When it succeeds it emits a final
    assistant message containing the post-condition observation; the
    controller then marks the step DONE.
    """
    parts = [
        f"USER TASK\n=========\n{user_input}",
        "",
        "PLAN STATUS\n===========",
        plan.render_for_prompt(),
        "",
        f"CURRENT STEP\n============\n"
        f"id: {step.id}\n"
        f"goal: {step.goal}",
    ]
    if step.expected:
        parts.append(f"expected outcome: {step.expected}")
    if step.tools_hint:
        parts.append(f"tools to consider: {', '.join(step.tools_hint)}")
    if step.depends_on:
        parts.append(f"depends on completed steps: {', '.join(step.depends_on)}")
    if completed_actions_summary:
        parts.append("")
        parts.append("ALREADY DONE\n============")
        parts.append(completed_actions_summary)
    if artifact_index_summary:
        parts.append("")
        parts.append("AVAILABLE ARTIFACTS\n===================")
        parts.append(artifact_index_summary)

    parts += [
        "",
        "INSTRUCTIONS",
        "============",
        "Work on the CURRENT STEP only — earlier DONE steps are settled, later",
        "PENDING steps are not your concern this turn.  When the step's",
        "expected outcome holds, finish with a final assistant message that",
        "starts with `STEP_DONE: <one-line proof of post-condition>`.  If you",
        "cannot complete the step, finish with `STEP_BLOCKED: <reason>` so the",
        "controller can replan.  Do not narrate intent — call tools.",
    ]
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Controller verdict — what came out of one step run.
# ---------------------------------------------------------------------------

class StepVerdict(str, Enum):
    DONE = "done"
    FAILED = "failed"
    BLOCKED = "blocked"
    EXHAUSTED = "exhausted"


_DONE_RE = re.compile(r"^\s*STEP_DONE\s*:\s*(.+)$", re.IGNORECASE | re.MULTILINE)
_BLOCKED_RE = re.compile(r"^\s*STEP_BLOCKED\s*:\s*(.+)$", re.IGNORECASE | re.MULTILINE)


def parse_step_outcome(text: str) -> tuple[StepVerdict, str]:
    """
    Inspect the executor's final assistant text and return the implied
    verdict plus the proof / reason string.
    """
    if not text:
        return StepVerdict.FAILED, ""
    m = _DONE_RE.search(text)
    if m:
        return StepVerdict.DONE, m.group(1).strip()
    m = _BLOCKED_RE.search(text)
    if m:
        return StepVerdict.BLOCKED, m.group(1).strip()
    # No explicit marker — accept the run as ``done`` only if the executor
    # produced ANY non-error final text.  The caller is expected to have
    # already checked that no tool failures occurred this turn.
    return StepVerdict.DONE, text.strip()[:200]


# ---------------------------------------------------------------------------
# Replan helper
# ---------------------------------------------------------------------------

def merge_revised_plan(current: Plan, revised: Plan) -> Plan:
    """
    Combine an existing plan with a revised one returned by the planner.

    Preserves DONE / SKIPPED step states from ``current`` when the same
    id appears in ``revised``.  FAILED steps are NOT preserved on
    purpose: the planner's whole job in a replan is to propose a new
    approach to a step that just failed, so we accept the revised
    step as-is.  New ids from ``revised`` are appended.  The revision
    counter increments so the executor can tell plans apart in the
    trace.

    Plan-level preflight checks are carried forward — the revised
    plan's preflight wins when it supplies one, otherwise the current
    plan's checks are kept so a replan that doesn't restate them
    doesn't silently lose them.  Mirrors the TS ``mergeRevisedPlan``.
    """
    by_id = {s.id: s for s in current.steps}
    merged_steps: list[PlanStep] = []
    seen_ids: set[str] = set()

    for new_step in revised.steps:
        seen_ids.add(new_step.id)
        prev = by_id.get(new_step.id)
        if prev and prev.status in (StepStatus.DONE, StepStatus.SKIPPED):
            merged_steps.append(prev)
        else:
            merged_steps.append(new_step)

    # Carry over any DONE step the new plan dropped (don't lose history).
    for old in current.steps:
        if old.id not in seen_ids and old.status == StepStatus.DONE:
            merged_steps.append(old)

    preflight = list(revised.preflight) if revised.preflight else list(current.preflight)
    return Plan(
        steps=merged_steps,
        created=current.created,
        revision=current.revision + 1,
        preflight=preflight,
    )
