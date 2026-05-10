"""
planner.py — Pre-execution planning phase.

Before the executor's tool-calling loop starts, ask the model to
produce a numbered, high-level plan for the task.  The plan is then
prepended to the executor's context as a `[PLAN]` block so every
subsequent decision has the full trajectory in mind instead of
re-deriving "what was I trying to accomplish?" from the previous
tool result.

Pattern is the same one UFO and AppAgent use — a HostAgent that
plans, an AppAgent that executes — but compressed into a single
extra LLM call rather than a second long-running session.

Cost: +1 chat call per task.  Benefit: fewer wasted iterations
and noticeably more coherent multi-step traces in practice.

Two planner modes
-----------------
``Planner.plan`` returns the legacy free-text numbered plan (used when
the typed controller is disabled).  ``Planner.plan_typed`` asks for a
JSON document that maps onto ``controller.Plan`` and falls back to the
free-text path on parse failure so an old/non-JSON-friendly model still
yields a working plan.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)


_PLANNER_SYSTEM = (
    "You are a desktop automation planner. Given a user task plus a brief "
    "snapshot of the current environment, produce a concise numbered plan of "
    "3 to 8 high-level steps that will accomplish the task.\n\n"
    "Rules:\n"
    "  * Describe goals (\"open Edge\", \"navigate to weather page\", "
    "    \"copy the forecast text\"), not specific clicks or coordinates.\n"
    "  * Each step should map to one observable outcome the executor can "
    "    verify (a window opening, a page loading, a value appearing).\n"
    "  * Do NOT call tools. Output is plain text only.\n"
    "  * Prefer the strongest available tool family for each step:\n"
    "      browser_*  for any web-based subgoal (when allowed_browser=true)\n"
    "      desktop_click_element  for native UI elements with visible labels\n"
    "      desktop_click_text / desktop_click_mark  for unlabelled items\n"
    "      desktop_click(x,y)  only as a last resort.\n"
    "  * If the task is trivial (one tool call), output a single-line plan.\n"
    "  * If the task is ambiguous, state the assumption you'll make in step 1."
)


_TYPED_PLANNER_SYSTEM = (
    "You are a desktop automation planner producing a structured plan.\n"
    "Output a single JSON object — no prose, no code fences, no commentary.\n\n"
    "Schema:\n"
    "  {\n"
    "    \"steps\": [\n"
    "      {\n"
    "        \"id\": \"s1\",                  // short stable id\n"
    "        \"goal\": \"...\",                // one-line goal statement\n"
    "        \"expected\": \"...\",            // post-condition the executor can verify\n"
    "        \"tools_hint\": [\"browser_navigate\"],  // optional\n"
    "        \"depends_on\": []              // ids of steps that must complete first\n"
    "      }, ...\n"
    "    ]\n"
    "  }\n\n"
    "Rules:\n"
    "  * 3 to 8 steps; each maps to ONE observable post-condition.\n"
    "  * `expected` must be checkable (a window title, a page URL, a file existing,\n"
    "    a piece of text being on screen).\n"
    "  * Use ids like s1, s2, s3 unless logical grouping makes another scheme clearer.\n"
    "  * Prefer browser_* for web tasks, desktop_click_element for labelled native\n"
    "    UI, desktop_click_text / desktop_click_mark for unlabelled, desktop_click as\n"
    "    a last resort.\n"
    "  * Do NOT call tools.  Output is JSON only."
)


_TYPED_PLANNER_USER = (
    "TASK\n----\n{task}\n\n"
    "ENVIRONMENT\n-----------\n"
    "OS: {os_label}\n"
    "Vision-capable: {vision}\n"
    "Browser tools available: {browser_available}\n"
    "Accessibility (a11y) clicking available: {a11y_available}\n\n"
    "DESKTOP STATE\n-------------\n{windows}\n\n"
    "Produce the typed plan JSON now."
)


_PLANNER_USER = (
    "TASK\n----\n{task}\n\n"
    "ENVIRONMENT\n-----------\n"
    "OS: {os_label}\n"
    "Vision-capable: {vision}\n"
    "Browser tools available: {browser_available}\n"
    "Accessibility (a11y) clicking available: {a11y_available}\n\n"
    "DESKTOP STATE\n-------------\n{windows}\n\n"
    "Produce the plan now."
)


class Planner:
    """One-shot planner.  Stateless aside from its client reference."""

    def __init__(self, client, system_prompt: str | None = None):
        self._client = client
        self._system_prompt = system_prompt or _PLANNER_SYSTEM

    async def plan(
        self,
        task: str,
        os_label: str = "",
        vision: bool = False,
        browser_available: bool = False,
        a11y_available: bool = False,
        windows_summary: str = "",
    ) -> str:
        """Return the plan as plain text, or an empty string on failure."""
        if not task or not task.strip():
            return ""

        user_msg = _PLANNER_USER.format(
            task=task.strip(),
            os_label=os_label or "unknown",
            vision="yes" if vision else "no",
            browser_available="yes" if browser_available else "no",
            a11y_available="yes" if a11y_available else "no",
            windows=(windows_summary or "(unavailable)")[:1500],
        )
        try:
            response = await self._client.chat(
                messages=[
                    {"role": "system", "content": self._system_prompt},
                    {"role": "user", "content": user_msg},
                ],
                tools=None,
            )
            message = self._client.extract_message(response)
            text = (self._client.extract_text(message) or "").strip()
        except Exception as e:
            logger.warning("[planner] plan call failed: %s", e)
            return ""

        # Strip markdown code fences the model might wrap around the plan.
        text = re.sub(r"^```[a-z]*\n?", "", text).rstrip("`").strip()
        return text

    async def plan_typed(
        self,
        task: str,
        os_label: str = "",
        vision: bool = False,
        browser_available: bool = False,
        a11y_available: bool = False,
        windows_summary: str = "",
    ) -> str:
        """
        Ask the planner for a JSON-typed plan; falls back to the legacy
        free-text path on any failure so the controller can still run.

        Returns the raw model output (caller passes it through
        ``controller.parse_plan``).
        """
        if not task or not task.strip():
            return ""

        user_msg = _TYPED_PLANNER_USER.format(
            task=task.strip(),
            os_label=os_label or "unknown",
            vision="yes" if vision else "no",
            browser_available="yes" if browser_available else "no",
            a11y_available="yes" if a11y_available else "no",
            windows=(windows_summary or "(unavailable)")[:1500],
        )
        try:
            response = await self._client.chat(
                messages=[
                    {"role": "system", "content": _TYPED_PLANNER_SYSTEM},
                    {"role": "user", "content": user_msg},
                ],
                tools=None,
            )
            message = self._client.extract_message(response)
            text = (self._client.extract_text(message) or "").strip()
        except Exception as e:
            logger.warning("[planner] typed plan call failed: %s", e)
            return await self.plan(
                task=task, os_label=os_label, vision=vision,
                browser_available=browser_available,
                a11y_available=a11y_available,
                windows_summary=windows_summary,
            )

        text = re.sub(r"^```[a-z]*\n?", "", text).rstrip("`").strip()
        if text.startswith("{") or text.startswith("["):
            return text
        # The model ignored the JSON instruction; fall back so we still
        # get a usable (numbered-list) plan.
        return text
