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
    "        \"id\": \"s1\",\n"
    "        \"goal\": \"...\",                       // one-line goal statement\n"
    "        \"expected\": \"...\",                   // human-readable post-condition\n"
    "        \"predicate\": {                         // OPTIONAL typed verifier\n"
    "          \"kind\": \"window_title_contains\"|\"window_active_app\"|\"file_exists\"|\n"
    "                  \"file_absent\"|\"file_contains\"|\"url_contains\"|\"text_visible\"|\n"
    "                  \"process_running\"|\"shell_returns\",\n"
    "          \"value\": \"...\",                     // for *_contains/*_visible/process_running\n"
    "          \"path\": \"...\",                      // for file_*\n"
    "          \"command\": \"...\",                   // for shell_returns\n"
    "          \"stdout_contains\": \"...\"            // optional, shell_returns only\n"
    "        },\n"
    "        \"tools_hint\": [\"browser_navigate\"],\n"
    "        \"depends_on\": [],                      // ids that must complete first\n"
    "        \"risks\": [\"login wall on first visit\", \"pages load slowly\"]\n"
    "      }, ...\n"
    "    ],\n"
    "    \"preflight\": [                              // OPTIONAL resource gates\n"
    "      {\"kind\": \"app\"|\"file\"|\"url\"|\"tool\"|\"command\", \"target\": \"...\"}\n"
    "      // app:     executable resolves on PATH (e.g. \"notepad\", \"chrome\",\n"
    "      //          \"git\").  Use this for ANY GUI app you'll launch with\n"
    "      //          desktop_launch — never use command for that.\n"
    "      // file:    a file exists at the given path.\n"
    "      // url:     host:port of the URL is TCP-reachable.\n"
    "      // tool:    a desktop_/browser_/etc. tool name is registered.\n"
    "      // command: a CLI command runs and exits 0 (e.g. \"git --version\",\n"
    "      //          \"python --version\").  Do NOT pass GUI apps here:\n"
    "      //          launching notepad/excel/chrome through a shell either\n"
    "      //          hangs the preflight or returns exit -1, failing the\n"
    "      //          check even though the GUI app is perfectly available.\n"
    "    ]\n"
    "  }\n\n"
    "Rules:\n"
    "  * 3 to 8 steps; each maps to ONE observable post-condition.\n"
    "  * Always set `expected`.  Set `predicate` whenever the post-condition is\n"
    "    deterministically checkable — that lets the controller verify without\n"
    "    asking another LLM.\n"
    "  * Add `risks` for non-trivial steps so the executor treats common failure\n"
    "    modes as expected branches rather than surprises.\n"
    "  * Prefer browser_* for web tasks, desktop_click_element for labelled native\n"
    "    UI, desktop_click_text / desktop_click_mark for unlabelled, desktop_click as\n"
    "    a last resort.\n"
    "  * Predicate-selection guidance:\n"
    "      - GUI app launched           → window_title_contains (use the window\n"
    "        title that the app shows, e.g. \"Notepad\" / \"Excel\"), OR\n"
    "        process_running with the executable name (e.g. value=\"notepad.exe\").\n"
    "        Do NOT use shell_returns here — GUI apps print nothing to stdout,\n"
    "        so any stdout_contains predicate is guaranteed to fail even when\n"
    "        the launch succeeded.\n"
    "      - File created               → file_exists with the absolute path.\n"
    "      - File contents updated      → file_contains with path + value.\n"
    "      - Web page navigated         → url_contains with the URL substring.\n"
    "      - Visible text on screen, OR text typed into a window      → text_visible\n"
    "        with the typed substring.  Do NOT invent kinds like text_input_equals,\n"
    "        text_input_verified, element_focused, window_has_focus_element, or any\n"
    "        other unlisted name — only the kinds enumerated above are recognised;\n"
    "        anything else is treated as 'no predicate' and the controller falls\n"
    "        back to the model's STEP_DONE proof.\n"
    "      - Shell command exit/output  → shell_returns ONLY for actual CLI\n"
    "        commands (git status, ls, where.exe), never for GUI apps.\n"
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
    "{registered_tools}"
    "{exemplars}"
    "{memory_hints}"
    "Produce the typed plan JSON now."
)


_CRITIQUE_SYSTEM = (
    "You are a critique reviewer for a desktop-automation plan.  Read the plan\n"
    "and the task, then reply with a single JSON object:\n"
    "  {\n"
    "    \"approve\": true | false,\n"
    "    \"issues\": [ \"missing step X\", \"step s2 expected outcome is unverifiable\", ... ],\n"
    "    \"revised_plan\": { ...same schema as the planner output... } | null\n"
    "  }\n\n"
    "Approve plans that are correct and complete.  Reject and return a revised\n"
    "plan when steps are missing, when an `expected` post-condition is too vague\n"
    "to verify, when dependencies are wrong, or when an obvious failure mode\n"
    "(login wall, modal dialog, app not installed) is unmentioned.  Output JSON\n"
    "only — no prose, no code fences."
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
        exemplars: list[dict] | None = None,
        memory_hints: list[str] | None = None,
        registered_tools: list[str] | None = None,
    ) -> str:
        """
        Ask the planner for a JSON-typed plan; falls back to the legacy
        free-text path on any failure so the controller can still run.

        Optional ``exemplars`` is a list of skill records (``{name,
        keywords, steps}``) the planner can use as few-shot examples
        of "what worked before" for similar tasks.  Optional
        ``memory_hints`` is a list of free-form strings (one per app)
        from the AppMemory store warning about known quirks.

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
            registered_tools=_format_registered_tools(registered_tools),
            exemplars=_format_exemplars(exemplars),
            memory_hints=_format_memory_hints(memory_hints),
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

    async def critique(self, *, task: str, plan_json: str) -> dict:
        """
        Run a critique pass over a typed plan.

        Returns a dict with three keys: ``approve`` (bool), ``issues``
        (list[str]), ``revised_plan_json`` (str | None).  When the
        critique fails or returns nothing parseable, ``approve`` is
        True and ``issues`` is empty so the controller proceeds with
        the original plan.
        """
        if not plan_json or not plan_json.strip():
            return {"approve": True, "issues": [], "revised_plan_json": None}
        user_msg = (
            "TASK\n----\n" + (task or "").strip() + "\n\n"
            "PLAN\n----\n" + plan_json.strip()
        )
        try:
            response = await self._client.chat(
                messages=[
                    {"role": "system", "content": _CRITIQUE_SYSTEM},
                    {"role": "user", "content": user_msg},
                ],
                tools=None,
            )
            message = self._client.extract_message(response)
            text = (self._client.extract_text(message) or "").strip()
        except Exception as e:
            logger.warning("[planner] critique call failed: %s", e)
            return {"approve": True, "issues": [], "revised_plan_json": None}

        text = re.sub(r"^```[a-z]*\n?", "", text).rstrip("`").strip()
        try:
            import json
            data = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            logger.info("[planner] critique unparsable; proceeding with original plan")
            return {"approve": True, "issues": [], "revised_plan_json": None}

        approve = bool(data.get("approve", True))
        issues = [str(x) for x in (data.get("issues") or [])][:8]
        revised = data.get("revised_plan")
        revised_json = None
        if isinstance(revised, dict):
            try:
                import json
                revised_json = json.dumps(revised)
            except (TypeError, ValueError):
                revised_json = None
        return {"approve": approve, "issues": issues,
                "revised_plan_json": revised_json}


def _format_registered_tools(tools: list[str] | None) -> str:
    if not tools:
        return ""
    return (
        "REGISTERED TOOLS\n"
        "----------------\n"
        "Only reference these exact names in preflight tool checks and tools_hint:\n"
        + "\n".join(f"  {t}" for t in sorted(tools))
        + "\n\n"
    )


def _format_exemplars(exemplars: list[dict] | None) -> str:
    if not exemplars:
        return ""
    lines = ["FEW-SHOT EXEMPLARS (skills that worked for similar tasks)\n"
             "----------------------------------------------------------"]
    for s in exemplars[:3]:
        name = s.get("name") or "?"
        kw = ", ".join((s.get("keywords") or [])[:5])
        steps = s.get("steps") or []
        body = " → ".join(
            f"{step.get('tool')}({_short_args(step.get('args') or {})})"
            for step in steps[:8]
        )
        lines.append(f"- {name} (keywords: {kw}): {body}")
    lines.append("")
    return "\n".join(lines) + "\n"


def _short_args(args: dict) -> str:
    if not args:
        return ""
    pairs = []
    for k, v in list(args.items())[:3]:
        s = repr(v)
        if len(s) > 40:
            s = s[:37] + "..."
        pairs.append(f"{k}={s}")
    return ", ".join(pairs)


def _format_memory_hints(hints: list[str] | None) -> str:
    if not hints:
        return ""
    body = "\n".join(h for h in hints if h)
    if not body:
        return ""
    return "APP MEMORY HINTS\n----------------\n" + body + "\n\n"
