"""
agent.history — conversation history and progress persistence.

History access, progress-store persistence and plan-state resume, split out
of the original agent.py as a mixin of ``Agent``.
"""

import logging
from typing import TYPE_CHECKING

from controller import StepStatus

if TYPE_CHECKING:
    from controller import Plan
    from progress import ProgressStore, TaskProgress

logger = logging.getLogger(__name__)


class HistoryMixin:
    """History / progress persistence methods of ``Agent``."""

    if TYPE_CHECKING:
        # Typing-only declarations of Agent state used by these methods.
        _history: list[dict]
        _plan: "Plan | None"
        _progress: "ProgressStore | None"
        _task_progress: "TaskProgress | None"

    def _persist_progress(self):
        """Snapshot the live plan into the progress store (no-op if disabled)."""
        if self._progress is None or self._task_progress is None or self._plan is None:
            return
        try:
            self._progress.update_plan_snapshot(self._task_progress, self._plan.to_dict())
        except Exception as e:
            logger.debug("[agent] progress snapshot failed: %s", e)

    def _resume_plan_state(self) -> None:
        """Apply persisted completed_step_ids onto the freshly parsed plan,
        BUT only when the persisted plan's shape (step id + goal pairs)
        actually matches the new one.

        Without the shape check we'd inherit completed_step_ids whenever
        the new plan happened to use the same auto-generated ids
        (s1, s2, ...) as the previous run — which is essentially every
        run, since both planners number sequentially.  Result: the
        agent silently skipped step 1 of a fresh task because a prior
        run with the same task text had completed an *unrelated* s1.
        """
        if self._plan is None or self._task_progress is None:
            return

        prior_snapshot = self._task_progress.plan_snapshot or {}
        prior_steps = prior_snapshot.get("steps") if isinstance(prior_snapshot, dict) else None
        if not isinstance(prior_steps, list) or not prior_steps:
            # No persisted shape to compare against — treat as a fresh
            # plan rather than blindly inheriting completed ids.
            logger.info(
                "[agent] no persisted plan_snapshot to resume against; "
                "starting fresh.  task_id=%s",
                self._task_progress.task_id,
            )
            return

        prior_shape = {
            str(s.get("id", "")): str(s.get("goal", ""))
            for s in prior_steps
            if isinstance(s, dict)
        }
        new_shape = {step.id: step.goal for step in self._plan.steps}

        # Compare ONLY the steps that actually carry persisted state —
        # if the prior plan had {s1: "open notepad"} and the new plan
        # has {s1: "launch chrome"}, we must NOT mark s1 done.  But if
        # the new plan added new steps (s4, s5) that's fine; we just
        # don't have state for them, which is correct.
        compatible_ids: set[str] = set()
        for sid, prior_goal in prior_shape.items():
            new_goal = new_shape.get(sid)
            if new_goal is not None and new_goal == prior_goal:
                compatible_ids.add(sid)

        # If NONE of the prior step ids match by goal, the persisted
        # state is for a different plan — log the divergence and
        # don't apply it.  This is the common case when the user
        # re-runs the same /autogui prompt with the model producing a
        # fresh plan.
        if not compatible_ids:
            logger.info(
                "[agent] persisted plan_snapshot has no shape overlap with the "
                "fresh plan (likely a different planning pass); starting fresh.  "
                "task_id=%s prior_step_ids=%s new_step_ids=%s",
                self._task_progress.task_id,
                list(prior_shape.keys()),
                list(new_shape.keys()),
            )
            return

        for sid in self._task_progress.completed_step_ids:
            if sid not in compatible_ids:
                continue
            step = self._plan.by_id(sid)
            if step is not None and step.status == StepStatus.PENDING:
                step.status = StepStatus.DONE
        for sid in self._task_progress.failed_step_ids:
            if sid not in compatible_ids:
                continue
            step = self._plan.by_id(sid)
            if step is not None and step.status == StepStatus.PENDING:
                step.status = StepStatus.FAILED


    @property
    def history(self) -> list[dict]:
        """Return a read-only view of the current message history."""
        return list(self._history)


    def _brief_history_summary(self) -> str:
        """Compact recent-history blurb for verifier prompts."""
        out = []
        for msg in self._history[-6:]:
            role = msg.get("role", "?")
            content = msg.get("content", "")
            if isinstance(content, list):
                content = " ".join(
                    p.get("text", "") for p in content if isinstance(p, dict)
                )
            content = (content or "")[:200].replace("\n", " ")
            out.append(f"{role}: {content}")
        return "\n".join(out)
