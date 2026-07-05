"""
agent.loop — Core agentic loop (the ``Agent`` class).

The agent implements a ReAct-style (Reason + Act) loop:

  1. Append the user message (or initial task) to the conversation history.
  2. Send the full history plus the tool catalog to the LLM via the client.
  3. Examine finish_reason:
       "stop"       → model produced a final text reply; yield it and exit.
       "tool_calls" → model issued one or more tool calls; dispatch each,
                      append results as role="tool" messages, and loop.
       "length"     → context length exceeded; yield a warning and exit.
  4. Guard against infinite loops with a configurable max_iterations ceiling.

The loop yields AgentEvent objects rather than returning a single final value,
so that the TUI (tui.py) and the CLI one-shot path (main.py) can both consume
a streaming event sequence and render incrementally.  This is a generator-based
approach using "yield" rather than async generators with "async yield", keeping
the event delivery compatible with asyncio without requiring AsyncGenerator
plumbing in the callers.

AgentEvent types
----------------
  "text"       — A text segment from the assistant.
  "text_delta" — An incremental text fragment streamed from the assistant
                 (only when openwebui.stream is enabled); the complete text
                 still follows as a normal "text" event.
  "tool_call"  — The model is about to invoke a tool (name + args).
  "tool_result" — The result of a tool call.
  "error"      — An error occurred (message included).
  "done"       — Loop has ended; includes finish_reason and iteration count.
"""

import asyncio
import json
import logging
import platform as _platform
import re
import traceback
from trace import TraceWriter
from typing import Any, AsyncIterator, Callable

import predicates
import preflight
from app_memory import AppMemory
from artifacts import ArtifactStore
from budget import BudgetTracker
from client import OpenWebUIClient, StreamAggregator
from controller import (
    Plan,
    PlanStep,
    StepStatus,
    StepVerdict,
    build_step_prompt,
    merge_revised_plan,
    parse_plan,
    parse_step_outcome,
)
from failures import RecoveryAction, classify, escalate_action
from planner import Planner
from progress import ProgressStore, TaskProgress
from prompt_loader import PromptLoader
from screen_record import ScreenRecorder
from skills import SkillStore
from subagent import Subagent
from tools import ToolRegistry
from watchdog import Watchdog

from .bon import BonMixin
from .dispatch import DispatchMixin
from .events import AgentEvent
from .guards import GuardsMixin
from .history import HistoryMixin
from .oso_bundle import OsoBundleMixin
from .prompts import _build_os_instructions
from .recovery import RecoveryMixin

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------

class Agent(
    DispatchMixin,
    HistoryMixin,
    GuardsMixin,
    BonMixin,
    RecoveryMixin,
    OsoBundleMixin,
):
    """
    Stateful agent that wraps a conversation history and drives the agentic loop.

    Parameters
    ----------
    client : OpenWebUIClient
        Initialized API client.
    registry : ToolRegistry
        Initialized tool registry.
    cfg : dict
        Full configuration dict (agent section used for system prompt, etc.).
    """

    def __init__(
        self,
        client: OpenWebUIClient,
        registry: ToolRegistry,
        cfg: dict,
        event_sink: Callable[["AgentEvent"], None] | None = None,
    ):
        self._client = client
        self._registry = registry
        self._cfg = cfg
        self._agent_cfg = cfg.get("agent", {})
        self._max_iterations: int = self._agent_cfg.get("max_iterations", 30)
        self._event_sink = event_sink

        # Load prompt files; fall back to config system_prompt for the base if missing.
        self._prompts = PromptLoader(cfg.get("prompts_dir", "prompts"))
        base = self._prompts.text("system_base") or self._agent_cfg.get(
            "system_prompt", "You are a helpful agent."
        )
        # Controller protocol is appended to the system prompt only when
        # the controller is enabled — otherwise the legacy "free-form
        # assistant" guidance applies and the STEP_DONE markers would be
        # noise.  Read the SAME default that the runtime branch below
        # uses (True) so we don't accidentally take the controller path
        # while leaving the system prompt without the step protocol —
        # that combination caused the model to receive a typed plan
        # with no STEP_DONE guidance and respond by hallucinating "I've
        # completed the plan" in text without invoking tools.
        controller_protocol = ""
        if (self._agent_cfg.get("controller", {}) or {}).get("enabled", True):
            controller_protocol = self._prompts.text("controller_step_protocol")

        # OSO text observation: opt-in feature that pairs the auto-screenshot
        # with a textual perception bundle (description + sketch + depth-
        # trimmed a11y tree).  Hard-gated on screen_observer.enabled AND the
        # backend actually reporting screen_observer capability — otherwise
        # the model must see no trace of OSO in its context.
        _oso_text_cfg = (
            (cfg.get("screen_observer", {}) or {}).get("text_observation", {}) or {}
        )
        _backend_caps_for_oso = getattr(self._registry, "_backend_caps", {}) or {}
        self._oso_text_enabled: bool = bool(
            _oso_text_cfg.get("enabled", False)
            and _backend_caps_for_oso.get("screen_observer", False)
        )
        self._oso_text_cfg = {
            "include_sketch": bool(_oso_text_cfg.get("include_sketch", True)),
            "include_tree": bool(_oso_text_cfg.get("include_tree", True)),
            "scope": str(_oso_text_cfg.get("scope", "active_window")),
            "max_chars": int(_oso_text_cfg.get("max_chars", 6000)),
            "tree_start_depth": int(_oso_text_cfg.get("tree_start_depth", 6)),
            "tree_min_depth": int(_oso_text_cfg.get("tree_min_depth", 1)),
            "tree_max_chars": int(_oso_text_cfg.get("tree_max_chars", 4000)),
        }
        # OSO-specific prompt fragment is loaded ONLY when OSO is reachable.
        # When OSO is disabled the file isn't read, so the strings inside
        # never enter the system prompt or any other LLM-visible context.
        oso_prompt_fragment = ""
        if _backend_caps_for_oso.get("screen_observer", False):
            oso_prompt_fragment = self._prompts.text("system_desktop_rules_oso")

        self._system_prompt: str = "\n\n".join(filter(None, [
            base,
            _build_os_instructions(self._prompts),
            self._prompts.text("system_desktop_rules"),
            self._prompts.text("system_browser_rules"),
            self._prompts.text("system_tool_analysis"),
            oso_prompt_fragment,
            controller_protocol,
        ]))

        # Seconds to wait before dispatching each tool call (0 = immediate).
        self._confirm_delay: int = int(cfg.get("safety", {}).get("command_confirm_delay_seconds", 0))
        # When True, screenshot base64 is delivered as an image_url vision message
        # so vision-capable models can actually see it.  Set False for text-only models.
        self._vision_screenshots: bool = bool(self._agent_cfg.get("vision_screenshots", True))
        logger.info("[agent] vision_screenshots=%s", self._vision_screenshots)

        # Streaming chat (openwebui.stream, default false): when enabled the
        # chat call sites iterate client.chat_stream and emit a "text_delta"
        # AgentEvent per text fragment before the usual final "text" event.
        # Gated on the client actually exposing chat_stream so stub clients
        # without it keep working unchanged.
        self._stream_enabled: bool = bool(
            (cfg.get("openwebui") or {}).get("stream", False)
        ) and hasattr(client, "chat_stream")
        logger.info("[agent] stream=%s", self._stream_enabled)

        # Pre-create the screenshots directory at startup so it's always
        # there for the user to inspect, even if a backend's screenshot()
        # later fails (PIL missing, ImageGrab raises, PowerShell hiccup,
        # etc.).  Backends would otherwise create it lazily INSIDE the
        # capture try-block — which never runs when the capture itself
        # raises before reaching the mkdir call.
        screenshots_dir = self._agent_cfg.get("screenshots_dir", "screenshots")
        try:
            from pathlib import Path as _P
            _P(screenshots_dir).mkdir(parents=True, exist_ok=True)
        except OSError as e:
            logger.warning("[agent] could not pre-create %s: %s", screenshots_dir, e)

        # The message history is the single source of truth for the conversation.
        # It persists across multiple calls to run(), allowing multi-turn dialogue.
        self._history: list[dict] = [
            {"role": "system", "content": self._system_prompt}
        ]

        # Running record of successful tool calls — passed to the coherence
        # checker so it can flag duplicates (e.g. launching the same app twice).
        self._completed_actions: list[str] = []

        # Full-fidelity record of successful tool dispatches for the current
        # session.  Used by skill_save to snapshot the recipe of "what worked"
        # and by the trace writer for post-hoc inspection.
        self._session_steps: list[dict] = []

        # Best-of-N (Phase 7) configuration + uncertainty trackers.  When
        # bon.enabled is true and any of the trigger conditions match, the
        # next action is selected by sampling N completions and asking the
        # fast/verifier model to pick the best.  All defaults off — this
        # multiplies token spend on uncertain steps.
        bon_cfg = self._agent_cfg.get("bon", {}) or {}
        self._bon_enabled: bool = bool(bon_cfg.get("enabled", False))
        self._bon_n: int = max(2, int(bon_cfg.get("n", 3)))
        self._bon_temperature: float = float(bon_cfg.get("temperature", 0.7))
        self._bon_trigger_on_failure: bool = bool(
            bon_cfg.get("trigger_on_recent_failure", True)
        )
        self._bon_trigger_on_validator: bool = bool(
            bon_cfg.get("trigger_on_validator_disagreement", True)
        )
        self._last_iteration_had_failure: bool = False
        self._last_validator_verdict: str | None = None

        # Trace + skill store wiring (Phase 2).
        trace_dir = self._agent_cfg.get("trace_dir", "logs/traces")
        skills_path = self._agent_cfg.get("skills_path", "skills/skills.jsonl")
        # Skills are CREATION-OPT-IN: when skills_enabled is false (the
        # default) existing skills are still readable / replayable
        # (skill_list, skill_run, candidate suggestion at task start),
        # but skill_save is NOT registered so the agent cannot create
        # new skill records.  The SkillStore is constructed lazily —
        # its constructor does not touch the disk, so nothing is
        # written until skill_save fires for the first time.  Set
        # skills_enabled=true to allow creation.
        self._skills_enabled: bool = bool(self._agent_cfg.get("skills_enabled", False))
        self._suggest_skills: bool = bool(self._agent_cfg.get("suggest_skills", True))
        self._record_trace: bool = bool(self._agent_cfg.get("record_trace", True))
        # Drift anchors are the post-action snapshots (window titles + an
        # optional perceptual screen hash) attached to each session step
        # so `replay --drift-check` can flag steps whose live world state
        # has shifted vs. the recording.  Anchoring is on by default
        # because window titles are essentially free; the perceptual
        # screenshot hash is gated separately because it requires a
        # screenshot per step.
        drift_cfg = self._agent_cfg.get("drift_anchor", {}) or {}
        self._drift_anchor_enabled: bool = bool(drift_cfg.get("enabled", True))
        self._drift_anchor_phash: bool = bool(drift_cfg.get("capture_phash", False))

        try:
            self._trace = TraceWriter(trace_dir) if self._record_trace else None
        except Exception as e:
            logger.warning("[agent] TraceWriter init failed: %s", e)
            self._trace = None

        self._skill_store: SkillStore | None = None
        try:
            # Constructed unconditionally — lazy disk semantics mean an
            # empty / missing skills file produces no side effects.
            self._skill_store = SkillStore(skills_path)
        except Exception as e:
            logger.warning("[agent] SkillStore init failed: %s", e)
            self._skill_store = None

        if self._trace:
            try:
                self._trace.write_meta(
                    event="session_start",
                    model=(cfg.get("openwebui") or {}).get("model") or "?",
                    vision=self._vision_screenshots,
                )
            except Exception as e:
                logger.debug("[agent] trace session_start meta write failed: %s", e)

        if self._skill_store is not None:
            self._register_skill_tools()

        # Planner (Phase 12).  When enabled, the agent issues one extra
        # LLM call BEFORE the action loop to produce a numbered plan, then
        # injects the plan into history so the executor sees the full
        # trajectory it's working towards.  Disabled = legacy behaviour.
        planner_cfg = self._agent_cfg.get("planner", {}) or {}
        self._planner_enabled: bool = bool(planner_cfg.get("enabled", True))
        self._planner: Planner | None = None
        if self._planner_enabled:
            try:
                self._planner = Planner(self._client)
            except Exception as e:
                logger.warning("[agent] Planner init failed: %s", e)
                self._planner = None

        # ---- Controller / artifact / progress / subagent (Phase 13) -----
        # The controller decomposes a task into typed plan steps and runs
        # the executor loop step-by-step with scoped per-step prompts.
        # Artifact store gives observations stable IDs so big bodies stay
        # out of history.  Progress store survives crashes/aborts and lets
        # the user resume.  Subagent answers read-only questions without
        # bloating the main conversation.
        controller_cfg = self._agent_cfg.get("controller", {}) or {}
        # Controller is ON by default — the typed-plan + step-scoped
        # executor is the recommended path.  Set enabled=false to fall
        # back to the legacy single-loop executor.
        self._controller_enabled: bool = bool(controller_cfg.get("enabled", True))
        self._step_max_iterations: int = max(1, int(controller_cfg.get("step_max_iterations", 8)))
        self._step_max_retries: int = max(0, int(controller_cfg.get("step_max_retries", 2)))
        self._auto_resume: bool = bool(controller_cfg.get("auto_resume", True))
        self._replan_on_block: bool = bool(controller_cfg.get("replan_on_block", True))
        self._critique_enabled: bool = bool(controller_cfg.get("critique_enabled", True))
        self._preflight_enabled: bool = bool(controller_cfg.get("preflight_enabled", True))
        self._predicate_check_enabled: bool = bool(
            controller_cfg.get("predicate_check_enabled", True)
        )
        # Predicate retries: GUI state (process_running, text_visible,
        # window_title_contains) is racy — Notepad takes a moment to
        # show up in tasklist after desktop_launch returns, OCR may
        # fail to read text on a screen that hasn't repainted yet.
        # Retry the check a few times with a short gap so a transient
        # miss doesn't tank a step that actually succeeded.  Set
        # predicate_retry_count=0 to disable.
        self._predicate_retry_count: int = max(
            0, int(controller_cfg.get("predicate_retry_count", 3))
        )
        self._predicate_retry_delay: float = max(
            0.0, float(controller_cfg.get("predicate_retry_delay_seconds", 0.5))
        )
        # Recovery probe: when a predicate or step fails, capture a
        # perception bundle (screenshot + window list + OSO observe) so
        # the model can re-target unattended on the next turn.  Bounded
        # by ``recovery_probe_max_per_step`` to keep a hopeless step from
        # looping on screenshots.  Symmetric with the pi-extension.
        self._recovery_probe_enabled: bool = bool(
            controller_cfg.get("recovery_probe_enabled", True)
        )
        self._recovery_probe_max_per_step: int = max(
            0, int(controller_cfg.get("recovery_probe_max_per_step", 2))
        )
        # Per-step counter; reset at the top of each step loop iteration.
        self._recovery_probe_count: dict[str, int] = {}
        self._visual_diff_enabled: bool = bool(
            controller_cfg.get("visual_diff_enabled", True)
        )
        self._watchdog_threshold: int = int(controller_cfg.get("watchdog_stall_threshold", 3))

        artifact_cfg = self._agent_cfg.get("artifacts", {}) or {}
        self._artifacts: ArtifactStore | None = None
        try:
            self._artifacts = ArtifactStore(artifact_cfg.get("dir", "logs/artifacts"))
        except Exception as e:
            logger.warning("[agent] ArtifactStore init failed: %s", e)
            self._artifacts = None

        progress_cfg = self._agent_cfg.get("progress", {}) or {}
        self._progress: ProgressStore | None = None
        try:
            self._progress = ProgressStore(progress_cfg.get("dir", "logs/progress"))
        except Exception as e:
            logger.warning("[agent] ProgressStore init failed: %s", e)
            self._progress = None

        subagent_cfg = self._agent_cfg.get("subagent", {}) or {}
        self._subagent_enabled: bool = bool(subagent_cfg.get("enabled", True))
        self._subagent_max_calls: int = max(1, int(subagent_cfg.get("max_tool_calls", 4)))
        self._subagent: Subagent | None = None
        if self._subagent_enabled:
            try:
                self._subagent = Subagent(
                    self._client, self._registry,
                    max_tool_calls=self._subagent_max_calls,
                    artifact_store=self._artifacts,
                )
            except Exception as e:
                logger.warning("[agent] Subagent init failed: %s", e)
                self._subagent = None

        # Per-task plan + state (populated by run()).
        self._plan: Plan | None = None
        self._task_progress: TaskProgress | None = None
        self._step_retry_counts: dict[str, int] = {}
        self._budget: BudgetTracker | None = None
        self._watchdog: Watchdog | None = None
        self._last_screenshot_hash: bytes | None = None

        # Per-app memory store (failure histograms, success counts, notes).
        # Lives under <agent.memory.dir> (default ./memory) and survives
        # across sessions so the planner can warn about app quirks the
        # next time the user touches the same app.
        #
        # ``memory.enabled`` controls CREATION only.  When false (the
        # default) existing records remain readable via memory_get and
        # the planner still consumes hints from on-disk memory, but
        # record_failure / record_success / add_note become silent
        # no-ops and memory_note is not registered as a tool.  This
        # mirrors the skills.skills_enabled gate and keeps the agent
        # from writing a memory/ directory unless the user opts in.
        memory_cfg = self._agent_cfg.get("memory", {}) or {}
        self._memory_enabled: bool = bool(memory_cfg.get("enabled", False))
        self._memory: AppMemory | None = None
        try:
            self._memory = AppMemory(
                memory_cfg.get("dir", "memory"),
                allow_writes=self._memory_enabled,
            )
        except Exception as e:
            logger.warning("[agent] AppMemory init failed: %s", e)
            self._memory = None

        # Budget ceilings.  All defaults zero = no ceiling — the user
        # opts in to hard limits explicitly.
        budget_cfg = self._agent_cfg.get("budget", {}) or {}
        self._budget_max_tools: int = int(budget_cfg.get("max_tool_calls", 0))
        self._budget_max_chats: int = int(budget_cfg.get("max_chat_calls", 0))
        self._budget_max_tokens: int = int(budget_cfg.get("max_total_tokens", 0))
        self._budget_max_seconds: float = float(budget_cfg.get("max_seconds", 0.0))

        # Register the artifact / plan / checkpoint / subagent meta-tools
        # so the model can interact with the new stores explicitly.
        self._register_meta_tools()

        # Rolling screen buffer (Phase 11).  When a tool fails, the buffer
        # is flushed to an animated GIF so the user can see exactly how
        # the agent got into trouble.
        rec_cfg = self._agent_cfg.get("screen_record", {}) or {}
        self._record_enabled: bool = bool(rec_cfg.get("enabled", True))
        self._recorder: ScreenRecorder | None = None
        if self._record_enabled:
            try:
                self._recorder = ScreenRecorder(
                    out_dir=rec_cfg.get("out_dir", "screenshots/failures"),
                    fps=int(rec_cfg.get("fps", 5)),
                    buffer_seconds=float(rec_cfg.get("buffer_seconds", 5.0)),
                    max_width=int(rec_cfg.get("max_width", 960)),
                )
                self._recorder.start()
            except Exception as e:
                logger.warning("[agent] ScreenRecorder init failed: %s", e)
                self._recorder = None

    # ------------------------------------------------------------------
    # Streaming chat helper
    # ------------------------------------------------------------------

    async def _chat_streamed(
        self,
        messages: list[dict],
        tools: list[dict],
        on_text_delta: Callable[[str], None] | None = None,
    ) -> dict:
        """
        One streaming chat call: iterate ``client.chat_stream``, forward each
        text fragment to ``on_text_delta`` as it arrives, and return the
        aggregated response (shape-identical to ``client.chat()``).

        Raises on any stream failure — the caller decides the fallback
        (one non-streaming retry per step).
        """
        aggregator = StreamAggregator()
        async for delta in self._client.chat_stream(messages=messages, tools=tools):
            aggregator.add(delta)
            if (
                on_text_delta is not None
                and delta.get("type") == "text_delta"
                and delta.get("text")
            ):
                on_text_delta(delta["text"])
        return aggregator.response()

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    async def run(self, user_input: str) -> AsyncIterator[AgentEvent]:
        """
        Public entry point.  Tees every yielded event through the trace
        writer and the optional event_sink before passing it to the caller,
        so observability / replay logging can be added without modifying
        each yield site individually.

        Routes to the controller-driven loop when ``agent.controller.enabled``
        is true; otherwise runs the legacy single-loop executor.
        """
        if self._controller_enabled and self._planner is not None:
            inner = self._run_with_controller(user_input)
        else:
            inner = self._run_inner(user_input)

        async for event in inner:
            if self._trace is not None:
                try:
                    self._trace.write_event(event)
                except Exception as e:
                    logger.debug("[agent] trace write_event failed: %s", e)
            if self._event_sink is not None:
                try:
                    self._event_sink(event)
                except Exception as e:
                    logger.debug("[agent] event sink callback failed: %s", e)
            yield event

    # ------------------------------------------------------------------
    # Controller-driven path
    # ------------------------------------------------------------------

    async def _run_with_controller(self, user_input: str) -> AsyncIterator[AgentEvent]:
        """
        Plan -> critique -> preflight -> step-by-step execution.

        For each PENDING step the controller composes a scoped prompt
        (see controller.build_step_prompt), drives the executor loop in a
        per-step history, and observes the step's STEP_DONE / STEP_BLOCKED
        marker plus any tool failures.  Plan progress is persisted after
        every step.  Cost telemetry, watchdog, predicate verification,
        and visual diff are wired into the per-step loop.
        """
        # run() only dispatches here when a planner was constructed; the
        # assert re-establishes that invariant for the type checker.
        assert self._planner is not None
        # Fresh per-task budget tracker + watchdog so concurrent runs
        # don't share state.
        self._budget = BudgetTracker(
            max_tool_calls=self._budget_max_tools,
            max_chat_calls=self._budget_max_chats,
            max_total_tokens=self._budget_max_tokens,
            max_seconds=self._budget_max_seconds,
        )
        self._watchdog = (
            Watchdog(stall_threshold=self._watchdog_threshold)
            if self._watchdog_threshold > 0 else None
        )
        self._last_screenshot_hash = None

        available_tools = set(self._registry.list_tools())
        windows_json = ""
        if "desktop_list_windows" in available_tools:
            try:
                windows_json = await self._registry.dispatch("desktop_list_windows", {})
            except Exception as e:
                failure_verdict = classify(tool_name="desktop_list_windows", error_message=str(e))
                logger.debug(
                    "[agent] planner windows probe failed (%s): %s",
                    failure_verdict.cls.value, e,
                )

        # --- Few-shot exemplars + memory hints ---------------------------
        # Surface up to 3 successful skills with overlapping keywords as
        # planning exemplars, plus per-app memory hints for any apps
        # currently visible on the desktop.  Both are best-effort.
        exemplars: list[dict] = []
        if self._suggest_skills and self._skill_store is not None:
            try:
                exemplars = self._skill_store.search(user_input, limit=3)
            except Exception as e:
                logger.debug("[agent] skill exemplar search failed: %s", e)
                exemplars = []

        memory_hints: list[str] = []
        if self._memory is not None and windows_json:
            seen: set[str] = set()
            try:
                wins = json.loads(windows_json).get("windows") or []
            except (json.JSONDecodeError, TypeError):
                wins = []
            for w in wins:
                app = (w.get("app") or "").strip()
                if not app or app in seen:
                    continue
                seen.add(app)
                hint = self._memory.hint_for_planner(app)
                if hint:
                    memory_hints.append(hint)
                if len(memory_hints) >= 4:
                    break

        # --- Plan acquisition: typed first, free-text fallback ----------
        os_label = _platform.system()
        browser_avail = any(t.startswith("browser_") for t in available_tools)
        a11y_avail = "desktop_click_element" in available_tools
        plan_text = ""
        try:
            plan_text = await self._planner.plan_typed(
                task=user_input,
                os_label=os_label,
                vision=self._vision_screenshots,
                browser_available=browser_avail,
                a11y_available=a11y_avail,
                windows_summary=windows_json,
                exemplars=exemplars,
                memory_hints=memory_hints,
                registered_tools=sorted(available_tools),
            )
            # Plan_typed wraps an internal client.chat call we can't
            # otherwise observe; count it for the budget tracker.
            if self._budget is not None:
                self._budget.chat_calls += 1
        except Exception as e:
            logger.warning("[agent] typed planner failed: %s", e)

        # --- Critique pass: one extra LLM call to review the plan ------
        if self._critique_enabled and plan_text and plan_text.strip().startswith("{"):
            try:
                critique_verdict = await self._planner.critique(task=user_input, plan_json=plan_text)
                if self._budget is not None:
                    self._budget.chat_calls += 1
            except Exception as e:
                logger.warning("[agent] critique failed: %s", e)
                critique_verdict = {"approve": True, "issues": [], "revised_plan_json": None}
            if critique_verdict.get("issues"):
                yield AgentEvent(
                    kind="plan_critique",
                    content="critique issues: " + " | ".join(critique_verdict["issues"][:5]),
                    data={"issues": critique_verdict["issues"], "approve": critique_verdict.get("approve")},
                )
            if not critique_verdict.get("approve") and critique_verdict.get("revised_plan_json"):
                plan_text = critique_verdict["revised_plan_json"]
                yield AgentEvent(
                    kind="plan_revised",
                    content="critique replaced plan",
                    data={"source": "critique"},
                )

        plan = parse_plan(plan_text)
        if not plan.steps:
            yield AgentEvent(
                kind="error",
                content="Planner produced no steps; falling back to legacy loop.",
                data={"raw": plan_text[:200]},
            )
            async for event in self._run_inner(user_input):
                yield event
            return

        self._plan = plan
        if self._progress is not None:
            self._task_progress = self._progress.open_task(user_input)
            if self._auto_resume:
                self._resume_plan_state()
            self._persist_progress()

        yield AgentEvent(
            kind="plan",
            content=plan.render_for_prompt(),
            data={"plan": plan.to_dict()},
        )

        # --- Preflight: resource gates BEFORE the executor touches UI -----
        if self._preflight_enabled:
            checks = preflight.infer_checks_from_plan(plan.to_dict(), registry=self._registry)
            if checks:
                report = await preflight.run_preflight(checks, registry=self._registry)
                yield AgentEvent(
                    kind="preflight",
                    content=("preflight passed" if report.all_passed
                             else f"preflight failed ({len(report.failures())} of {len(report.results)})"),
                    data=report.to_dict(),
                )
                if not report.all_passed:
                    # The preflight event above already announced the
                    # failure (with per-check details).  Skip the
                    # follow-up step_escalate yield — both rendered the
                    # same "preflight failed" line in the TUI, which
                    # made it look like the agent aborted twice for
                    # different reasons.  The done event below carries
                    # the same report under data["report"] for any
                    # consumer that wanted it.
                    if self._progress is not None and self._task_progress is not None:
                        self._progress.finalize(self._task_progress, status="failed")
                    yield AgentEvent(
                        kind="done",
                        content="Controller aborted (preflight)",
                        data={
                            "plan": plan.to_dict(),
                            "status": "preflight_failed",
                            # Surface a real iteration count even on early
                            # exits so the TUI's "done (?, ? iterations)"
                            # render shows 0 instead of "?", which made it
                            # look like the controller never ran at all.
                            "iterations": 0,
                            "finish_reason": "preflight_failed",
                            # Carried forward from the preflight event so
                            # programmatic consumers (replay, dashboards)
                            # can still see what the actual checks were.
                            "report": report.to_dict(),
                        },
                    )
                    return

        # --- Step loop ---------------------------------------------------
        global_iter = 0
        while True:
            step = plan.next_runnable()
            if step is None:
                break

            # Check the global iteration ceiling so a runaway plan can't
            # exceed the same overall budget the legacy loop enforces.
            if global_iter >= self._max_iterations:
                yield AgentEvent(
                    kind="error",
                    content=f"Global iteration ceiling ({self._max_iterations}) reached; "
                            "remaining plan steps abandoned.",
                    data={"plan": plan.to_dict()},
                )
                break

            # Pre-step budget check: bail BEFORE starting a new step when
            # the previous step blew through the ceiling.  Mirror check
            # at the bottom catches mid-step overflow as well.
            if self._budget is not None and self._budget.exceeded:
                yield AgentEvent(
                    kind="budget_exceeded",
                    content=f"Budget exceeded: {self._budget.reason()}",
                    data=self._budget.snapshot(note="exceeded").__dict__,
                )
                if self._progress is not None and self._task_progress is not None:
                    self._progress.finalize(self._task_progress, status="failed")
                yield AgentEvent(
                    kind="done",
                    content="Controller stopped (budget)",
                    data={
                        "plan": plan.to_dict(),
                        "status": "budget_exceeded",
                        "iterations": global_iter,
                        "finish_reason": "budget_exceeded",
                    },
                )
                return

            step.status = StepStatus.RUNNING
            step.attempts += 1
            self._step_retry_counts[step.id] = self._step_retry_counts.get(step.id, 0)
            self._persist_progress()
            yield AgentEvent(
                kind="step_start",
                content=f"→ ({step.id}) {step.goal}",
                data={"step": step.to_public(), "attempts": step.attempts},
            )

            if self._stream_enabled:
                # Streaming: run the step as a task and drain its event
                # queue concurrently so text_delta events reach the caller
                # while the step is still executing.
                step_queue: asyncio.Queue = asyncio.Queue()
                step_task = asyncio.create_task(self._run_step(
                    user_input=user_input,
                    step=step,
                    event_yield=step_queue.put_nowait,
                ))
                while True:
                    getter = asyncio.create_task(step_queue.get())
                    finished, _ = await asyncio.wait(
                        {getter, step_task},
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if getter in finished:
                        yield getter.result()
                        continue
                    getter.cancel()
                    break
                while not step_queue.empty():
                    yield step_queue.get_nowait()
                verdict, reason, used_iters, failure = step_task.result()
            else:
                verdict, reason, used_iters, failure = await self._run_step(
                    user_input=user_input,
                    step=step,
                    event_yield=None,  # no live event forwarding when not streaming
                )
            global_iter += used_iters

            # Predicate verification: when the step declared a typed
            # predicate, check it deterministically before accepting
            # STEP_DONE.  A miss demotes the verdict to BLOCKED and
            # routes through the standard failure-classification path.
            predicate_failure_detail = ""
            if (
                verdict == StepVerdict.DONE
                and self._predicate_check_enabled
                and step.predicate
            ):
                # Sanity-check the predicate kind BEFORE running it.  The
                # model occasionally invents kinds like
                # "window_exists_with_partial_match" or
                # "element_has_focus_and_is_editable" that aren't in
                # PREDICATE_KINDS.  predicates.normalize() returns None for
                # those, and check_predicate would then return ok=False
                # with "predicate normalised to None" — treating an
                # invalid kind as a verified failure, demoting STEP_DONE
                # to BLOCKED and forcing a replan.  But the model's action
                # may have actually succeeded!  Treat invalid-kind as
                # "skip the check, accept STEP_DONE", log a warning so
                # the planner authoring is visible, and move on.
                if predicates.normalize(step.predicate) is None:
                    logger.warning(
                        "[agent] step %s predicate kind %r is not a recognised "
                        "PREDICATE_KIND — skipping verification and accepting "
                        "STEP_DONE.  Tell the planner to use one of: %s",
                        step.id,
                        step.predicate.get("kind") or step.predicate.get("type"),
                        ", ".join(predicates.PREDICATE_KINDS),
                    )
                    p_result = predicates.PredicateResult(
                        ok=True, kind="(skipped)",
                        detail=(
                            "invalid predicate kind "
                            f"{step.predicate.get('kind') or step.predicate.get('type')!r}"
                            " — verification skipped (the model's STEP_DONE is authoritative)"
                        ),
                    )
                else:
                    # Race-tolerant predicate verification: process_running
                    # / text_visible / window_title_contains all probe GUI
                    # state that lags the action by hundreds of
                    # milliseconds (Notepad needs a tick to show up in
                    # tasklist; OCR can't read a half-painted window).
                    # Retry up to predicate_retry_count times with a small
                    # gap so the model's STEP_DONE doesn't get demoted to
                    # BLOCKED just because we checked too soon.  The
                    # FIRST attempt runs immediately; backoff only fires
                    # between retries, not before the initial check.
                    attempt_total = 1 + self._predicate_retry_count
                    p_result = predicates.PredicateResult(
                        ok=False, kind=str(step.predicate.get("kind", "?")),
                        detail="predicate not yet checked",
                    )
                    for attempt in range(1, attempt_total + 1):
                        try:
                            p_result = await predicates.check_predicate(
                                step.predicate, self._registry,
                            )
                        except Exception as e:
                            logger.warning("[agent] predicate check raised: %s", e)
                            p_result = predicates.PredicateResult(
                                ok=False, kind=str(step.predicate.get("kind", "?")),
                                detail=f"check raised: {e}",
                            )
                        if p_result.ok or attempt >= attempt_total:
                            break
                        await asyncio.sleep(self._predicate_retry_delay)
                yield AgentEvent(
                    kind="predicate",
                    content=("predicate ok: " if p_result.ok else "predicate FAILED: ")
                            + predicates.render(step.predicate),
                    data={
                        "step": step.id, "ok": p_result.ok,
                        "detail": p_result.detail,
                        "predicate": step.predicate,
                    },
                )
                if not p_result.ok:
                    verdict = StepVerdict.BLOCKED
                    predicate_failure_detail = p_result.detail
                    reason = (
                        f"predicate {predicates.render(step.predicate)} "
                        f"did not hold ({p_result.detail})"
                    )
                    # Recovery probe: attach a perception bundle so the next
                    # planner / executor turn can re-target unattended
                    # instead of escalating.  Symmetric with the
                    # pi-extension's check_predicate failure path.
                    probe = await self._emit_recovery_probe(step.id, reason)
                    if probe is not None:
                        yield AgentEvent(
                            kind="recovery_probe",
                            content=(
                                f"recovery probe attached for step {step.id}: "
                                f"predicate {predicates.render(step.predicate)} failed — "
                                "before reporting blocked, use the screenshot + window "
                                "list + (when present) oso_observe in this event's data "
                                "to call desktop_screenshot_marked / desktop_click_text / "
                                "desktop_click_element and re-attempt."
                            ),
                            data=probe,
                        )

            # Process the verdict.
            if verdict == StepVerdict.DONE:
                step.status = StepStatus.DONE
                step.last_error = ""
                # Record per-app success so the planner can prefer the
                # tools that worked next time.  Skipped when memory
                # creation is disabled — the call would no-op anyway,
                # but cheaper not to make it.
                if self._memory is not None and self._memory_enabled:
                    try:
                        active_app = await self._best_effort_active_app()
                        if active_app:
                            for tool in step.tools_hint or []:
                                self._memory.record_success(app=active_app, tool=tool)
                    except Exception as e:
                        logger.debug("[agent] app-memory success recording failed: %s", e)
                if self._progress is not None and self._task_progress is not None:
                    self._progress.mark_done(self._task_progress, step.id)
                self._persist_progress()
                yield AgentEvent(
                    kind="step_done",
                    content=f"✓ ({step.id}) {reason[:160]}",
                    data={"step": step.to_public(), "iterations": used_iters},
                )

                # Budget check — if the user set ceilings, surface usage
                # after each successful step and bail when exceeded.
                if self._budget is not None and self._budget.exceeded:
                    yield AgentEvent(
                        kind="budget_exceeded",
                        content=f"Budget exceeded: {self._budget.reason()}",
                        data=self._budget.snapshot(note="exceeded").__dict__,
                    )
                    if self._progress is not None and self._task_progress is not None:
                        self._progress.finalize(self._task_progress, status="failed")
                    yield AgentEvent(
                        kind="done",
                        content="Controller stopped (budget)",
                        data={
                            "plan": plan.to_dict(),
                            "status": "budget_exceeded",
                            "iterations": global_iter,
                            "finish_reason": "budget_exceeded",
                        },
                    )
                    return
                continue

            # Failed / blocked / exhausted — classify and decide.
            self._step_retry_counts[step.id] += 1
            retry_count = self._step_retry_counts[step.id]
            verdict_failure = failure or classify(
                tool_name="(none)",
                error_message=reason,
                predicate_failed=verdict == StepVerdict.BLOCKED,
            )

            # Record the failure against the active app's memory so the
            # next plan can warn about it.  Same gate as success-record
            # above: skipped entirely when memory creation is disabled.
            if self._memory is not None and self._memory_enabled:
                try:
                    active_app = await self._best_effort_active_app()
                    if active_app:
                        self._memory.record_failure(
                            app=active_app,
                            tool=", ".join(step.tools_hint) or "(none)",
                            failure_class=verdict_failure.cls.value,
                            reason=predicate_failure_detail or reason[:120],
                        )
                except Exception as e:
                    logger.debug("[agent] app-memory failure recording failed: %s", e)
            action = escalate_action(
                verdict_failure,
                retry_count=retry_count,
                max_retries=self._step_max_retries,
            )

            # Recovery probe before escalation: when the step is going to
            # retry or replan, the perception bundle lets the next attempt
            # / planner turn see the actual screen state, not just the
            # error string.  Skip on terminal verdicts (the user will be
            # taking over) and on RETRY+predicate paths where we already
            # emitted a probe immediately after the predicate failed.
            already_probed = (
                verdict == StepVerdict.BLOCKED
                and bool(step.predicate)
                and predicate_failure_detail
            )
            if (
                not already_probed
                and action in (RecoveryAction.RETRY, RecoveryAction.WAIT_AND_RETRY,
                               RecoveryAction.REPLAN)
            ):
                step_probe = await self._emit_recovery_probe(step.id, reason)
                if step_probe is not None:
                    yield AgentEvent(
                        kind="recovery_probe",
                        content=(
                            f"recovery probe attached for step {step.id}: {verdict.value} — "
                            "the screenshot + window list + (when present) oso_observe "
                            "in this event's data show the current screen; use them with "
                            "desktop_screenshot_marked / desktop_click_text / "
                            "desktop_click_element to re-target on the next attempt."
                        ),
                        data=step_probe,
                    )

            yield AgentEvent(
                kind="step_failure",
                content=f"✗ ({step.id}) {verdict.value}: {reason[:160]} → {action.value}",
                data={
                    "step": step.to_public(),
                    "verdict": verdict.value,
                    "reason": reason,
                    "failure_class": verdict_failure.cls.value,
                    "recovery_action": action.value,
                    "retry_count": retry_count,
                },
            )

            if action == RecoveryAction.WAIT_AND_RETRY and verdict_failure.wait_seconds > 0:
                await asyncio.sleep(verdict_failure.wait_seconds)
                step.status = StepStatus.PENDING  # try again next iteration
                step.last_error = reason[:200]
                self._persist_progress()
                continue

            if action == RecoveryAction.RETRY:
                step.status = StepStatus.PENDING
                step.last_error = reason[:200]
                self._persist_progress()
                continue

            if action == RecoveryAction.REPLAN and self._replan_on_block:
                # Mark this step blocked; ask the planner to revise.
                step.status = StepStatus.BLOCKED
                step.last_error = reason[:200]
                self._persist_progress()
                revised = await self._replan(user_input, plan, blocked_step=step)
                if revised is not None and revised.steps:
                    plan = merge_revised_plan(plan, revised)
                    self._plan = plan
                    self._persist_progress()
                    yield AgentEvent(
                        kind="plan_revised",
                        content=plan.render_for_prompt(),
                        data={"plan": plan.to_dict(), "revision": plan.revision},
                    )
                    continue
                # Replan failed — fall through to escalation.

            # ESCALATE / ABORT or replan-disabled.
            step.status = StepStatus.FAILED
            step.last_error = reason[:200]
            if self._progress is not None and self._task_progress is not None:
                self._progress.mark_failed(self._task_progress, step.id)
            self._persist_progress()
            yield AgentEvent(
                kind="step_escalate",
                content=f"Step ({step.id}) needs user attention: {reason[:160]}",
                data={
                    "step": step.to_public(),
                    "failure_class": verdict_failure.cls.value,
                    "recovery_action": action.value,
                },
            )
            break

        # --- Final summary ----------------------------------------------
        all_done = all(s.status == StepStatus.DONE for s in plan.steps)
        final_status = "done" if all_done else "failed"
        if self._progress is not None and self._task_progress is not None:
            self._progress.finalize(self._task_progress, status=final_status)
        yield AgentEvent(
            kind="done",
            content=f"Controller {final_status} — {plan.progress_summary()}",
            data={
                "plan": plan.to_dict(),
                "iterations": global_iter,
                "status": final_status,
                "finish_reason": final_status,
            },
        )

    async def _replan(
        self, user_input: str, plan: Plan, *, blocked_step: PlanStep
    ) -> Plan | None:
        """Ask the planner for a revised plan that takes the failure into account."""
        if self._planner is None:
            return None
        context = (
            f"PREVIOUS PLAN STATUS\n--------------------\n{plan.render_for_prompt()}\n\n"
            f"BLOCKED STEP\n------------\nid: {blocked_step.id}\n"
            f"goal: {blocked_step.goal}\nreason: {blocked_step.last_error}"
        )
        try:
            revised_text = await self._planner.plan_typed(
                task=user_input + "\n\n" + context,
                os_label=_platform.system(),
                vision=self._vision_screenshots,
                registered_tools=self._registry.list_tools(),
            )
        except Exception as e:
            logger.warning("[agent] replan failed: %s", e)
            return None
        return parse_plan(revised_text)

    async def _run_step(
        self,
        *,
        user_input: str,
        step: PlanStep,
        event_yield,
    ) -> tuple[StepVerdict, str, int, Any]:
        """
        Drive a scoped per-step executor loop.

        Returns ``(verdict, reason, iterations_used, failure_verdict_or_None)``.
        Per-step history is constructed fresh from the system prompt + the
        scoped step prompt — it does NOT touch ``self._history``, so steps
        don't contaminate each other's context.

        ``event_yield`` — optional callable receiving AgentEvent objects to
        forward live (used for "text_delta" events while streaming); pass
        None to drop intra-step events.
        """
        # Watchdog history is per-step: a "stuck on step X" signal must
        # not bleed into step Y, even when both legitimately repeat the
        # same set of preliminary read-only tools.
        if self._watchdog is not None:
            self._watchdog.reset()

        # Build artifact summary for the step prompt (one-line per artifact,
        # capped) so the executor knows what's already in the artifact store.
        artifact_summary = ""
        if self._artifacts is not None:
            recent = self._artifacts.list_recent(limit=8)
            if recent:
                artifact_summary = "\n".join(
                    f"  {a.id}  [{a.kind}] {a.summary[:100]}" for a in recent
                )
        completed_summary = ""
        if self._plan is not None:
            done_steps = [s for s in self._plan.steps if s.status == StepStatus.DONE]
            if done_steps:
                completed_summary = "\n".join(
                    f"  ({s.id}) {s.goal}" for s in done_steps[-6:]
                )

        # _run_with_controller assigns self._plan before dispatching steps.
        assert self._plan is not None
        step_prompt = build_step_prompt(
            user_input=user_input,
            plan=self._plan,
            step=step,
            artifact_index_summary=artifact_summary,
            completed_actions_summary=completed_summary,
        )

        local_history: list[dict] = [
            {"role": "system", "content": self._system_prompt},
            {"role": "user", "content": step_prompt},
        ]

        # Pre-step baseline screenshot — give the model accurate visual
        # ground truth before its first action so it doesn't assume
        # state from the prompt text alone.  Without this the model
        # tends to treat the step's `expected outcome` as already true
        # and skip straight to STEP_DONE, or duplicate work that's
        # already been done (extra Notepad windows on retry).  Skipped
        # silently when vision is off, the screenshot tool isn't
        # registered, or the capture fails — we never want a baseline
        # screenshot failure to abort the step.
        if (
            self._vision_screenshots
            and "desktop_screenshot" in self._registry.list_tools()
        ):
            try:
                baseline_json = await self._registry.dispatch("desktop_screenshot", {})
                baseline_obj = json.loads(baseline_json)
                baseline_b64 = baseline_obj.pop("base64_png", None)
                if baseline_b64 and not baseline_obj.get("error"):
                    local_history.append({
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": (
                                    f"[Baseline screenshot for step {step.id} — "
                                    "this is the WORLD STATE BEFORE you act.  "
                                    "Inspect the windows, layout, and any visible "
                                    "text first; only call tools whose effect is "
                                    "needed to satisfy the CURRENT STEP's expected "
                                    "outcome.  If the expected outcome already "
                                    "appears to hold here, finish with STEP_DONE "
                                    "WITHOUT taking redundant actions.]"
                                ),
                            },
                            {
                                "type": "image_url",
                                "image_url": {"url": "data:image/png;base64," + baseline_b64},
                            },
                        ],
                    })
                elif baseline_obj.get("error"):
                    logger.warning(
                        "[agent] step %s baseline screenshot failed: %s",
                        step.id, str(baseline_obj.get("error"))[:200],
                    )
            except Exception as e:
                logger.warning("[agent] step %s baseline screenshot raised: %s", step.id, e)

        last_text = ""
        last_failure = None
        iterations = 0
        # BLOCKED retry guard: once we've fed the model a "does this
        # screenshot change your mind?" reminder we don't want to do it
        # again every iteration — that becomes a loop where the model
        # alternates STEP_BLOCKED + screenshot reminder until iterations
        # exhaust.  One chance, then accept the second STEP_BLOCKED.
        blocked_screenshot_retry_used = False
        # Same idea for the Gemma "tool-call syntax leaked into text"
        # reminder — re-prompt once, then accept the next bad turn so a
        # genuinely-stuck model still escalates.
        _gemma_format_reminder_used = False

        # Streaming: try chat_stream while this stays True; on the first
        # stream failure fall back to non-streaming for the REST of the
        # step (one fallback per step, per the streaming contract).
        stream_active = self._stream_enabled

        def _emit_text_delta(fragment: str) -> None:
            if event_yield is not None:
                event_yield(AgentEvent(
                    kind="text_delta",
                    content=fragment,
                    data={"step": step.id},
                ))

        while iterations < self._step_max_iterations:
            iterations += 1

            # Hard ceiling: refuse to make further chat calls when the
            # task budget is already over.  The outer loop will translate
            # this into a budget_exceeded event.
            if self._budget is not None and self._budget.exceeded:
                last_text = f"budget exceeded: {self._budget.reason()}"
                last_failure = classify(
                    tool_name="(budget)", error_message=last_text,
                )
                break

            try:
                response = None
                if stream_active:
                    try:
                        response = await self._chat_streamed(
                            local_history,
                            self._registry.schemas,
                            on_text_delta=_emit_text_delta,
                        )
                    except Exception as stream_exc:
                        stream_active = False
                        logger.warning(
                            "[agent] step %s: chat_stream failed (%s); "
                            "falling back to non-streaming for the rest of "
                            "this step.",
                            step.id, stream_exc,
                        )
                if response is None:
                    response = await self._client.chat(
                        messages=local_history,
                        tools=self._registry.schemas,
                    )
                if self._budget is not None:
                    self._budget.record_chat(response)
            except Exception as e:
                last_text = f"chat call failed: {e}"
                last_failure = classify(tool_name="(chat)", error_message=str(e))
                break

            try:
                message = self._client.extract_message(response)
            except Exception as e:
                last_text = f"extract_message failed: {e}"
                last_failure = classify(tool_name="(extract)", error_message=str(e))
                break

            local_history.append(message)
            text = self._client.extract_text(message) or ""
            tool_calls = self._client.extract_tool_calls(message) or []
            if text:
                last_text = text

            if not tool_calls:
                # Detect Gemma 3/4-style tool-call syntax that leaked
                # into the assistant TEXT instead of being parsed as a
                # real tool_call (`action:tool_name{...}<tool_call|>` or
                # similar).  The model intended to invoke a tool but the
                # client's tool-call extractor missed it.  Treating this
                # as STEP_BLOCKED triggers a costly replan; instead,
                # re-prompt the model with a format reminder so it tries
                # the proper tool_call format on the next iteration.
                # Gated by a flag so we only do this once per step (a
                # second leak after the reminder genuinely IS a stuck
                # model and should escalate).
                if (
                    not _gemma_format_reminder_used
                    and self._looks_like_leaked_tool_call(text)
                ):
                    _gemma_format_reminder_used = True
                    logger.warning(
                        "[agent] step %s: model emitted tool-call syntax as text "
                        "instead of a real tool_call (%s); injecting format "
                        "reminder and continuing.",
                        step.id, text[:80],
                    )
                    local_history.append({
                        "role": "user",
                        "content": (
                            "[CONTROLLER] Your last response contained tool-call "
                            "syntax INSIDE the assistant message text instead of "
                            "emitting it as a proper tool_call.  The runtime "
                            "cannot dispatch tools written that way.  For your "
                            "next turn, invoke the tool through the assistant's "
                            "tool_calls mechanism (the API field), not in the "
                            "message body.  If you meant to finish the step, "
                            "use STEP_DONE: <proof> or STEP_BLOCKED: <reason>."
                        ),
                    })
                    continue

                # Final assistant message — parse the marker.
                verdict, reason = parse_step_outcome(text)
                # On the FIRST STEP_BLOCKED in a step, give the model
                # one more chance with a fresh screenshot.  Models
                # frequently issue STEP_BLOCKED prematurely — the action
                # actually succeeded, the OCR / window-list check just
                # didn't fire correctly, OR the screen genuinely shows
                # the post-condition holds and the model didn't notice.
                # Feed the current screen back as visual evidence and
                # ask "does this change your assessment?".  Only one
                # such retry per step; a second STEP_BLOCKED is honoured.
                if (
                    verdict == StepVerdict.BLOCKED
                    and not blocked_screenshot_retry_used
                    and self._vision_screenshots
                    and "desktop_screenshot" in self._registry.list_tools()
                ):
                    blocked_screenshot_retry_used = True
                    try:
                        review_json = await self._registry.dispatch("desktop_screenshot", {})
                        review_obj = json.loads(review_json)
                        review_b64 = review_obj.pop("base64_png", None)
                        if review_b64 and not review_obj.get("error"):
                            local_history.append({
                                "role": "user",
                                "content": [
                                    {
                                        "type": "text",
                                        "text": (
                                            f"[STEP_BLOCKED reconsider — before the "
                                            f"controller replans step {step.id}, look at "
                                            f"the CURRENT desktop state captured just "
                                            f"now.  Does this screenshot show the step's "
                                            f"expected outcome ({step.expected!r}) "
                                            f"already holding?  If YES, finish with "
                                            f"`STEP_DONE: <proof from this screenshot>`.  "
                                            f"If the post-condition genuinely doesn't "
                                            f"hold, repeat your `STEP_BLOCKED: <reason>` "
                                            f"so the controller knows to replan.]"
                                        ),
                                    },
                                    {
                                        "type": "image_url",
                                        "image_url": {"url": "data:image/png;base64," + review_b64},
                                    },
                                ],
                            })
                            # Don't return yet — let the loop run another
                            # iteration so the model can react to the
                            # screenshot.
                            continue
                    except Exception as e:
                        logger.warning(
                            "[agent] step %s STEP_BLOCKED screenshot retry raised: %s",
                            step.id, e,
                        )
                return verdict, reason or text[:200], iterations, last_failure

            # Watchdog: hash (state + first proposed tool/args) and bail
            # if the same signature recurs ``stall_threshold`` times.
            if self._watchdog is not None and tool_calls:
                first = tool_calls[0]
                first_name = (first.get("function") or {}).get("name") or ""
                try:
                    first_args = json.loads((first.get("function") or {}).get("arguments") or "{}")
                except json.JSONDecodeError:
                    first_args = {}
                wd_state = await self._snapshot_watchdog_state()
                sig = self._watchdog.signature(
                    windows=wd_state["windows"],
                    active_window=wd_state["active"],
                    pending_tool=first_name,
                    pending_args=first_args,
                )
                status = self._watchdog.observe(sig)
                if status.stuck:
                    last_failure = classify(
                        tool_name=first_name,
                        error_message="watchdog: no progress over "
                                      f"{status.repeats} iterations",
                        predicate_failed=True,
                    )
                    return (
                        StepVerdict.BLOCKED,
                        f"watchdog: same state+action signature {sig} "
                        f"observed {status.repeats}× — step appears stuck.",
                        iterations, last_failure,
                    )

            # Dispatch each tool call.
            step_failure_seen = False
            for tc in tool_calls:
                tool_name = (tc.get("function") or {}).get("name") or ""
                call_id = tc.get("id", "")
                try:
                    args = json.loads((tc.get("function") or {}).get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                result_json = await self._registry.dispatch(tool_name, args)
                if self._budget is not None:
                    self._budget.record_tool()

                # Default verifier: enrich the result with a structured
                # post-condition check so the model reads it before deciding
                # the action succeeded.
                result_json = await self._apply_default_verifier(
                    tool_name, args, result_json,
                )

                # Maybe store large results as artifacts to keep history small.
                history_content = self._maybe_store_artifact(tool_name, args, result_json)

                # Track session step for skill_save (only on success).
                if not self._result_is_error(result_json) and tool_name not in (
                    "skill_save", "skill_list", "skill_run",
                    "desktop_screenshot", "desktop_screenshot_marked",
                    "desktop_list_windows", "desktop_get_active_window",
                    "fs_read", "fs_list", "get_artifact", "list_artifacts",
                    "plan_get", "plan_update_step", "checkpoint", "ask_subagent",
                ):
                    step_record: dict = {"tool": tool_name, "args": dict(args)}
                    anchor = await self._capture_drift_anchor()
                    if anchor:
                        step_record["drift_anchor"] = anchor
                    self._session_steps.append(step_record)

                if self._result_is_error(result_json):
                    step_failure_seen = True
                    try:
                        result_obj = json.loads(result_json)
                        err_msg = result_obj.get("error") or result_obj.get("stderr") or ""
                    except json.JSONDecodeError:
                        result_obj = {}
                        err_msg = result_json[:200]
                    last_failure = classify(
                        tool_name=tool_name,
                        error_message=err_msg,
                        result=result_obj,
                    )
                    history_content = (
                        "[TOOL FAILED: " + tool_name + "] "
                        f"(class={last_failure.cls.value} action={last_failure.action.value})\n"
                        + history_content
                    )

                local_history.append({
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": history_content,
                })

            # Auto-screenshot after state-changing desktop actions —
            # mirrors the legacy _run_inner behaviour so the model can
            # SEE the consequences of its action before deciding the
            # step is done.  Skipped when the model already called
            # desktop_screenshot itself this iteration (the result is
            # already in local_history) and when none of the called
            # tools were state-changing (read-only steps don't need a
            # post-action screenshot).
            called_names = {(tc.get("function") or {}).get("name") or "" for tc in tool_calls}
            state_changing = {
                n for n in called_names
                if n.startswith("desktop_")
                and n not in (
                    "desktop_screenshot", "desktop_screenshot_marked",
                    "desktop_list_windows", "desktop_get_active_window",
                    "desktop_get_cursor_pos", "desktop_get_window_text",
                    "desktop_get_window_tree", "desktop_find_element",
                    "desktop_find_text",
                )
                or n.startswith("browser_") and n not in (
                    "browser_get_text", "browser_screenshot",
                )
            }
            if (
                state_changing
                and "desktop_screenshot" in self._registry.list_tools()
                and "desktop_screenshot" not in called_names
                and "desktop_screenshot_marked" not in called_names
            ):
                try:
                    shot_json = await self._registry.dispatch("desktop_screenshot", {})
                    shot_obj = json.loads(shot_json)
                    shot_b64 = shot_obj.pop("base64_png", None)
                    if shot_obj.get("error"):
                        # Make sure the user (and the model) sees why
                        # screenshots aren't being saved.  Three places
                        # surface the failure: the model's local_history
                        # (so the next turn can diagnose), a logger.warning
                        # (which the TUI's WARNING-level log bridge picks
                        # up and renders in the conversation pane), and
                        # the agent log file.
                        err_msg = str(shot_obj.get("error"))[:200]
                        logger.warning(
                            "[agent] step %s auto-screenshot after %s FAILED: %s",
                            step.id, ", ".join(sorted(state_changing)), err_msg,
                        )
                        local_history.append({
                            "role": "user",
                            "content": (
                                f"[Auto-screenshot after {', '.join(sorted(state_changing))} "
                                f"FAILED: {err_msg}]"
                            ),
                        })
                    elif self._vision_screenshots and shot_b64:
                        local_history.append({
                            "role": "user",
                            "content": [
                                {
                                    "type": "text",
                                    "text": (
                                        f"[Auto-screenshot AFTER "
                                        f"{', '.join(sorted(state_changing))} — "
                                        f"compare this to the step's expected "
                                        f"outcome ({step.expected!r}).  Issue "
                                        f"STEP_DONE only if the screen confirms "
                                        f"the action took effect; if you can't "
                                        f"tell from the image, call "
                                        f"desktop_find_text or desktop_get_window_text "
                                        f"to verify before finishing.  Do NOT issue "
                                        f"STEP_DONE on assumed success.]"
                                    ),
                                },
                                {
                                    "type": "image_url",
                                    "image_url": {"url": "data:image/png;base64," + shot_b64},
                                },
                            ],
                        })
                    else:
                        # Vision off OR no base64: still surface the
                        # path so the user has a saved file to inspect
                        # and the model knows the screenshot ran.
                        path_str = shot_obj.get("path", "?")
                        dims = f"{shot_obj.get('width')}×{shot_obj.get('height')}"
                        local_history.append({
                            "role": "user",
                            "content": f"[Auto-screenshot saved: {path_str} ({dims})]",
                        })
                except Exception as e:
                    logger.warning("[agent] controller auto-screenshot failed: %s", e)

                # OSO text observation: paired with the screenshot when enabled
                # and the backend has the screen_observer capability.  Hard-
                # gated so when OSO is off no "[OSO ...]" string ever lands
                # in local_history.
                if self._oso_text_enabled:
                    try:
                        bundle = await self._build_oso_text_bundle()
                        if bundle is not None:
                            tools_str = ", ".join(sorted(state_changing))
                            parts = [
                                f"[Text observation AFTER {tools_str} "
                                f"(scope={bundle['scope']}, tree_depth={bundle['depth_used']}"
                                f"{', truncated' if bundle['truncated'] else ''})]"
                            ]
                            if bundle["description"]:
                                parts.append(bundle["description"])
                            if bundle["sketch"]:
                                parts.append("SKETCH:\n" + bundle["sketch"])
                            if bundle["tree_text"]:
                                parts.append("TREE:\n" + bundle["tree_text"])
                            local_history.append({
                                "role": "user",
                                "content": "\n\n".join(parts),
                            })
                    except Exception as e:
                        logger.warning("[agent] OSO text observation failed: %s", e)

            if step_failure_seen:
                local_history.append({
                    "role": "user",
                    "content": (
                        "[CONTROLLER] At least one tool failed in this iteration. "
                        "Diagnose the failure, choose a different approach, and try "
                        "again — do not give up after one failed attempt. When the "
                        "step's expected outcome is satisfied, finish with "
                        "STEP_DONE: <one-line proof>. If you cannot proceed, finish "
                        "with STEP_BLOCKED: <reason>."
                    ),
                })

        # Iteration ceiling reached without a marker.
        return StepVerdict.EXHAUSTED, last_text[:200] or "step iteration ceiling reached", iterations, last_failure

    async def _run_inner(self, user_input: str) -> AsyncIterator[AgentEvent]:
        """
        Append user_input to the history and drive the agentic loop until
        the model produces a final response or the iteration ceiling is reached.

        This is an async generator; callers must iterate it with "async for".

        Parameters
        ----------
        user_input : str
            The user's message or task description.

        Yields
        ------
        AgentEvent
            Events in emission order: text segments, tool_call, tool_result,
            error, and finally done.
        """
        # --- Snapshot initial desktop state --------------------------------
        # Append current window list to the user message so the model knows
        # what is already open before it starts.  If vision is on, also attach
        # a screenshot so the model can see the baseline screen state.
        available_tools = set(self._registry.list_tools())

        # Inject the current vision state so the model always knows whether
        # it will receive images — important when the user toggles mid-session.
        if self._vision_screenshots:
            initial_suffix = "\n" + self._prompts.text("runtime_vision_enabled")
        else:
            initial_suffix = "\n" + self._prompts.text("runtime_vision_disabled")

        windows_json = ""
        if "desktop_list_windows" in available_tools:
            try:
                windows_json = await self._registry.dispatch("desktop_list_windows", {})
                initial_suffix += f"\n\n[Desktop state at task start: {windows_json}]"
            except Exception as e:
                failure_verdict = classify(tool_name="desktop_list_windows", error_message=str(e))
                logger.debug(
                    "[agent] task-start desktop state probe failed (%s): %s",
                    failure_verdict.cls.value, e,
                )

        # ---- Planner pass (Phase 12) -------------------------------------
        # One extra LLM call BEFORE the executor loop produces a numbered
        # plan; the plan is injected as a [PLAN] block so every subsequent
        # tool decision has the full trajectory in mind.  Falls back to a
        # plan-less run on any failure so this can never make things worse.
        if self._planner is not None:
            browser_avail = any(t.startswith("browser_") for t in available_tools)
            a11y_avail = "desktop_click_element" in available_tools
            os_label = _platform.system()
            plan_text = ""
            try:
                plan_text = await self._planner.plan(
                    task=user_input,
                    os_label=os_label,
                    vision=self._vision_screenshots,
                    browser_available=browser_avail,
                    a11y_available=a11y_avail,
                    windows_summary=windows_json,
                )
            except Exception as e:
                logger.warning("[agent] planner failed: %s", e)
            if plan_text:
                logger.info("[agent] plan:\n%s", plan_text)
                yield AgentEvent(
                    kind="plan",
                    content=plan_text,
                    data={"plan": plan_text},
                )
                initial_suffix += (
                    "\n\n[PLAN — follow this trajectory, but adapt if the "
                    "screen state diverges from what a step expects:]\n"
                    + plan_text
                )

        # Skill retrieval — show the model up to 3 saved procedures whose
        # keywords overlap with the user's request, so it can opt to skill_run
        # instead of re-deriving from scratch.
        if self._suggest_skills and self._skill_store is not None and "skill_run" in available_tools:
            try:
                candidates = self._skill_store.search(user_input, limit=3)
            except Exception as e:
                logger.debug("[agent] skill candidate search failed: %s", e)
                candidates = []
            if candidates:
                lines = ["[Candidate saved skills (call skill_run if one matches):]"]
                for s in candidates:
                    lines.append(
                        f"  - {s.get('name')!r} (app={s.get('app','?')}, "
                        f"steps={len(s.get('steps', []))}, "
                        f"successes={s.get('success_count', 0)}, "
                        f"keywords={s.get('keywords', [])[:5]})"
                    )
                initial_suffix += "\n\n" + "\n".join(lines)

        # Pick the best screenshot tool: marked (Set-of-Mark) when available,
        # else plain.  The marked variant draws numbered boxes over UI elements
        # so the model can refer to them by id.
        shot_tool = (
            "desktop_screenshot_marked"
            if "desktop_screenshot_marked" in available_tools
            else "desktop_screenshot"
        )
        if self._vision_screenshots and shot_tool in available_tools:
            try:
                shot_json = await self._registry.dispatch(shot_tool, {})
                result_obj = json.loads(shot_json)
                b64 = result_obj.pop("base64_png", None)
                marks = result_obj.get("marks") or []
                if b64:
                    text_parts = [user_input + initial_suffix]
                    if marks:
                        text_parts.append(
                            f"[Set-of-Mark active — {len(marks)} numbered boxes drawn. "
                            "Use desktop_click_mark(id) to click any of them.]"
                        )
                    self._history.append({
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "\n".join(text_parts)},
                            {"type": "image_url",
                             "image_url": {"url": "data:image/png;base64," + b64}},
                        ],
                    })
                else:
                    self._history.append({"role": "user",
                                          "content": user_input + initial_suffix})
            except Exception as e:
                logger.debug(
                    "[agent] vision first-turn screenshot failed; using text-only turn: %s", e
                )
                self._history.append({"role": "user",
                                      "content": user_input + initial_suffix})
        else:
            self._history.append({"role": "user", "content": user_input + initial_suffix})

        iteration = 0
        # Streaming: try chat_stream while this stays True; the first stream
        # failure falls back to non-streaming for the rest of the run.
        stream_active = self._stream_enabled

        while iteration < self._max_iterations:
            iteration += 1
            logger.info("[agent.py:run] Iteration %d / %d", iteration, self._max_iterations)

            # ---- Call the LLM ----------------------------------------
            # Best-of-N branch when uncertainty triggers fired on the
            # previous iteration; otherwise greedy single sample.
            try:
                if self._bon_should_trigger():
                    response, bon_rationale = await self._bon_sample()
                    yield AgentEvent(
                        kind="validation",
                        content=f"BoN: {bon_rationale}",
                        data={
                            "bon_rationale": bon_rationale,
                            "iteration": iteration,
                        },
                    )
                else:
                    response = None
                    if stream_active:
                        try:
                            aggregator = StreamAggregator()
                            async for delta in self._client.chat_stream(
                                messages=self._history,
                                tools=self._registry.schemas,
                            ):
                                aggregator.add(delta)
                                if delta.get("type") == "text_delta" and delta.get("text"):
                                    yield AgentEvent(
                                        kind="text_delta",
                                        content=delta["text"],
                                        data={"iteration": iteration},
                                    )
                            response = aggregator.response()
                        except Exception as stream_exc:
                            stream_active = False
                            logger.warning(
                                "[agent.py:run] chat_stream failed on iteration "
                                "%d (%s); falling back to non-streaming.",
                                iteration, stream_exc,
                            )
                    if response is None:
                        response = await self._client.chat(
                            messages=self._history,
                            tools=self._registry.schemas,
                        )
            except Exception as e:
                print(f"[agent.py:run] API call failed on iteration {iteration}: {e}")
                traceback.print_exc()
                event = AgentEvent(
                    kind="error",
                    content=f"API error on iteration {iteration}: {e}",
                    data={"iteration": iteration},
                )
                yield event
                break

            # ---- Extract message and finish_reason -------------------
            assert response is not None  # all fall-through paths above assign it
            try:
                message = self._client.extract_message(response)
            except ValueError as e:
                yield AgentEvent(kind="error", content=str(e), data={"iteration": iteration})
                break

            finish_reason = response.get("choices", [{}])[0].get("finish_reason", "stop")
            text_content = self._client.extract_text(message)
            tool_calls = self._client.extract_tool_calls(message)

            # Append the assistant turn to history before processing.
            # The message object from the API already has the correct structure.
            self._history.append(message)

            # ---- Emit any text content --------------------------------
            if text_content:
                yield AgentEvent(
                    kind="text",
                    content=text_content,
                    data={"iteration": iteration, "finish_reason": finish_reason},
                )

            # ---- Handle finish_reason ---------------------------------
            if finish_reason == "stop" or (not tool_calls):
                # Before accepting "done", check for two failure modes:
                #
                # 1. Narrated actions: the model described what it would do instead
                #    of actually calling the tools.
                # 2. Premature give-up: the model said it cannot help or assist even
                #    though it has tools available to try.
                if text_content and self._text_implies_skipped_actions(text_content, self._vision_screenshots):
                    self._history.append({
                        "role": "user",
                        "content": self._prompts.render(
                            "runtime_narration_correction",
                            user_input=repr(user_input),
                        ),
                    })
                    logger.warning(
                        "[agent.py:run] Model narrated actions without tool calls on "
                        "iteration %d — injecting correction and continuing.",
                        iteration,
                    )
                    continue

                if text_content and self._text_implies_giving_up(text_content):
                    self._history.append({
                        "role": "user",
                        "content": self._prompts.render(
                            "runtime_give_up",
                            user_input=repr(user_input),
                        ),
                    })
                    logger.warning(
                        "[agent.py:run] Model appeared to give up on iteration %d — "
                        "injecting retry directive and continuing.",
                        iteration,
                    )
                    continue

                # Genuine stop — model produced a final answer.
                yield AgentEvent(
                    kind="done",
                    content="Agent completed.",
                    data={
                        "finish_reason": finish_reason,
                        "iterations": iteration,
                        "history_length": len(self._history),
                    },
                )
                return

            if finish_reason == "length":
                yield AgentEvent(
                    kind="error",
                    content="Context length limit reached; response may be incomplete.",
                    data={"iteration": iteration},
                )
                yield AgentEvent(kind="done", content="Agent stopped (length).", data={"iterations": iteration})
                return

            # ---- Dispatch tool calls ---------------------------------
            # Each tool_call is dispatched, its result appended as a role="tool"
            # message, and then the loop continues so the model can reason about
            # the results.  Multiple tool_calls in one assistant turn are all
            # dispatched before the next LLM invocation (parallel dispatch).
            #
            # Screenshot handling: the base64 payload is stripped from the tool
            # result text (to save context tokens) and re-delivered as a vision
            # message (role="user" with image_url) so vision-capable models can
            # actually SEE the screenshot rather than read raw base64.

            tool_result_messages: list[dict] = []
            vision_messages: list[dict] = []       # appended after tool results
            failed_tools:  list[str]  = []         # track failures for retry directive
            iteration_validator_verdict: str | None = None  # most recent validator verdict

            # ---- Capture pre-action window state for the diff ------------
            # Only relevant when at least one of the upcoming tool calls is a
            # desktop action.  Skipped otherwise to avoid wasting a wmctrl call.
            pre_windows = None
            tool_names_pending = {
                (tc.get("function") or {}).get("name") or "" for tc in tool_calls
            }
            if (
                any(n.startswith("desktop_") and n not in (
                    "desktop_list_windows", "desktop_screenshot",
                    "desktop_screenshot_marked", "desktop_get_active_window",
                    "desktop_get_cursor_pos", "desktop_get_window_text",
                    "desktop_get_window_tree", "desktop_find_element",
                    "desktop_find_text",
                ) for n in tool_names_pending)
                and "desktop_list_windows" in available_tools
            ):
                try:
                    pre_json = await self._registry.dispatch("desktop_list_windows", {})
                    pre_windows = json.loads(pre_json).get("windows", [])
                except Exception as e:
                    logger.debug("[agent] pre-action window snapshot failed: %s", e)
                    pre_windows = None

            for tc in tool_calls:
                tool_name = (tc.get("function") or {}).get("name") or "unknown"
                call_id = tc.get("id", "")
                raw_args = (tc.get("function") or {}).get("arguments") or "{}"

                # Parse the arguments JSON string emitted by the model.
                try:
                    args = json.loads(raw_args)
                except json.JSONDecodeError as e:
                    args = {}
                    logger.warning("[agent.py:run] Failed to parse tool args for %s: %s", tool_name, e)

                yield AgentEvent(
                    kind="tool_call",
                    content=f"→ {tool_name}({', '.join(f'{k}={repr(v)[:60]}' for k, v in args.items())})",
                    data={
                        "tool_name": tool_name,
                        "args": args,
                        "call_id": call_id,
                        "iteration": iteration,
                        # Phase 6b: stitch the model's preceding rationale onto
                        # every tool_call event so observability tools can show
                        # "why did the model do this?" without re-walking history.
                        "rationale": (text_content or "")[:400],
                    },
                )

                # Safety countdown: yield one event per second so consumers can
                # display a live timer, then dispatch only after the delay elapses.
                for remaining in range(self._confirm_delay, 0, -1):
                    yield AgentEvent(
                        kind="confirm_countdown",
                        content=f"Executing {tool_name} in {remaining}s…",
                        data={
                            "remaining": remaining,
                            "total": self._confirm_delay,
                            "tool_name": tool_name,
                            "call_id": call_id,
                        },
                    )
                    await asyncio.sleep(1)

                # Coherence check before executing shell or launch commands.
                # Calls the LLM with no tools to validate the command is appropriate
                # for the current task before actually running it.
                result_json = None
                if tool_name in ("shell_run", "desktop_launch"):
                    proceed, args, verdict = await self._check_command_coherence(
                        tool_name, args, user_input
                    )
                    iteration_validator_verdict = verdict
                    yield AgentEvent(
                        kind="validation",
                        content=f"Coherence [{tool_name}]: {verdict}",
                        data={
                            "tool_name": tool_name,
                            "verdict": verdict,
                            "iteration": iteration,
                        },
                    )
                    if not proceed:
                        result_json = json.dumps({
                            "error": (
                                f"Command blocked by coherence check: {verdict}. "
                                "Please choose a different command appropriate for the task."
                            )
                        })

                # Execute the tool (skip if blocked by coherence check).
                if result_json is None:
                    result_json = await self._registry.dispatch(tool_name, args)

                yield AgentEvent(
                    kind="tool_result",
                    content=self._summarize_result(tool_name, result_json),
                    data={
                        "tool_name": tool_name,
                        "call_id": call_id,
                        "result_json": result_json,
                        "iteration": iteration,
                    },
                )

                # ---- Build the history content for this tool result --------
                #
                # Screenshots: the backend always returns base64_png in the result.
                # We ALWAYS strip it before putting the result into history to avoid
                # sending thousands of base64 chars to the model (which causes models
                # like Gemma to choke or return "not in a format I can process").
                # When vision is on, the image is re-delivered as a vision message.
                # When vision is off, only the file metadata (path, size) goes to history.
                history_content = result_json
                if tool_name == "desktop_screenshot":
                    try:
                        result_obj = json.loads(result_json)
                        b64 = result_obj.pop("base64_png", None)
                        if b64:
                            if self._vision_screenshots:
                                result_obj["image_note"] = (
                                    "Image delivered as vision message — "
                                    "examine it carefully to verify screen state."
                                )
                                vision_messages.append({
                                    "role": "user",
                                    "content": [
                                        {
                                            "type": "text",
                                            "text": self._prompts.text("runtime_screenshot_vision"),
                                        },
                                        {
                                            "type": "image_url",
                                            "image_url": {
                                                "url": "data:image/png;base64," + b64
                                            },
                                        },
                                    ],
                                })
                            else:
                                result_obj["image_note"] = (
                                    "Vision is disabled — no image data is available to the model. "
                                    "Use desktop_list_windows to verify which applications are open."
                                )
                            history_content = json.dumps(result_obj)
                    except Exception as e:
                        # fall back to original history_content
                        logger.debug("[agent] screenshot result annotation failed: %s", e)

                # ---- fs_read: annotate result so model must analyze it -----
                if tool_name == "fs_read" and not self._result_is_error(result_json):
                    try:
                        r = json.loads(result_json)
                        if r.get("content"):
                            history_content = (
                                self._prompts.text("runtime_fs_read_annotation") + "\n"
                                + history_content
                            )
                    except Exception as e:
                        logger.debug("[agent] fs_read annotation failed: %s", e)

                # ---- Stderr warning for shell_run with exit_code 0 ---------
                # Some programs write real errors to stderr and still exit 0.
                # Flag it so the model reads it rather than skipping past it.
                if tool_name == "shell_run" and not self._result_is_error(result_json):
                    try:
                        r = json.loads(result_json)
                        stderr_text = r.get("stderr", "").strip()
                        if stderr_text:
                            history_content = (
                                self._prompts.text("runtime_stderr_warning") + "\n"
                                + history_content
                            )
                    except Exception as e:
                        logger.debug("[agent] shell stderr annotation failed: %s", e)

                # ---- Track successful actions for coherence / duplicate check --
                if not self._result_is_error(result_json):
                    if tool_name in ("shell_run", "desktop_launch", "desktop_click", "desktop_type"):
                        if tool_name == "shell_run":
                            detail = args.get("command", "")[:80]
                        elif tool_name == "desktop_launch":
                            detail = args.get("application", "")[:60]
                        else:
                            detail = json.dumps(args, ensure_ascii=False)[:60]
                        entry = f"{tool_name}({detail})"
                        # Don't record the same entry back-to-back
                        if not self._completed_actions or self._completed_actions[-1] != entry:
                            self._completed_actions.append(entry)
                        if len(self._completed_actions) > 20:
                            self._completed_actions = self._completed_actions[-20:]

                    # Full-fidelity record for skill snapshotting / trace export.
                    # Skip the meta tools — they're not meaningful to replay.
                    if tool_name not in (
                        "skill_save", "skill_list", "skill_run",
                        "desktop_screenshot", "desktop_screenshot_marked",
                        "desktop_list_windows", "desktop_get_active_window",
                        "desktop_get_cursor_pos", "desktop_get_window_text",
                        "desktop_get_window_tree", "desktop_find_element",
                        "desktop_find_text",
                        "fs_read", "fs_list",
                    ):
                        step_record: dict = {"tool": tool_name, "args": dict(args)}
                        anchor = await self._capture_drift_anchor()
                        if anchor:
                            step_record["drift_anchor"] = anchor
                        self._session_steps.append(step_record)

                # ---- Error injection: fail loud, not quiet -----------------
                # Prepend a header inside the tool result AND track the failure
                # so we can inject a strong user-role retry directive afterward.
                if self._result_is_error(result_json):
                    failed_tools.append(tool_name)
                    history_content = (
                        "[TOOL FAILED: " + tool_name + "]\n"
                        + history_content
                    )
                    # Phase 11: dump the rolling screen buffer so the user
                    # has a visual of how the agent got here.  Cheap when
                    # the buffer is small; silent if the recorder is off.
                    if self._recorder is not None:
                        gif_path = self._recorder.flush(
                            label=f"{tool_name}_iter{iteration}"
                        )
                        if gif_path:
                            yield AgentEvent(
                                kind="failure_recording",
                                content=f"Saved failure recording: {gif_path}",
                                data={
                                    "path": gif_path,
                                    "tool_name": tool_name,
                                    "iteration": iteration,
                                },
                            )

                tool_result_messages.append({
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": history_content,
                })

            # Append tool results first, then any vision messages.
            # Vision messages use role="user" which is valid after role="tool" messages.
            self._history.extend(tool_result_messages)
            if vision_messages:
                self._history.extend(vision_messages)

            # ---- Update BoN uncertainty trackers --------------------------
            # Persisted across iterations so that BoN triggers on the *next*
            # decision when this one fired off a failed tool or had a
            # disputed validator verdict.
            self._last_iteration_had_failure = bool(failed_tools)
            self._last_validator_verdict = iteration_validator_verdict

            # ---- Error retry directive -----------------------------------
            # If any tools failed, inject a role="user" message AFTER the tool
            # results.  The tool-result role gets skimmed by some models; a user
            # message right after forces a deliberate acknowledgement and retry.
            if failed_tools:
                self._history.append({
                    "role": "user",
                    "content": self._prompts.render(
                        "runtime_error_retry",
                        count=len(failed_tools),
                        tools=", ".join(failed_tools),
                    ),
                })
                logger.warning(
                    "[agent.py:run] %d tool(s) failed on iteration %d: %s — "
                    "injecting retry directive.",
                    len(failed_tools), iteration, failed_tools,
                )

            # ---- Auto-verify after desktop actions ----------------------
            # After any desktop action other than list_windows/screenshot,
            # automatically inject current window state so the model always
            # has ground-truth desktop state rather than having to hallucinate it.
            # If vision is on and the model didn't already take a screenshot this
            # batch, also inject a screenshot so it can SEE the result.
            # skill_run replays desktop actions internally so it also triggers
            # the verify pass — otherwise the model never sees whether typing
            # actually appeared in the target window.
            called_names = {(tc.get("function") or {}).get("name") or "" for tc in tool_calls}
            desktop_actions_taken = {
                n for n in called_names
                if n.startswith("desktop_")
                and n not in ("desktop_list_windows", "desktop_screenshot")
            }
            verify_label = (
                ", ".join(sorted(desktop_actions_taken))
                if desktop_actions_taken
                else "skill_run"
            )
            if desktop_actions_taken or "skill_run" in called_names:
                if "desktop_list_windows" in available_tools:
                    try:
                        windows_json = await self._registry.dispatch("desktop_list_windows", {})
                        # After desktop_launch specifically, remind the model to click
                        # in the new window before attempting to type anything.
                        launch_reminder = ""
                        if "desktop_launch" in desktop_actions_taken:
                            launch_reminder = " " + self._prompts.text("runtime_launch_reminder")

                        # ---- State-diff banner (Phases 4a/4b) -----------
                        # Compare pre/post window sets and surface "nothing
                        # happened" or "a modal popped up" as a one-line
                        # banner, so the model deliberately notices the
                        # change rather than glossing past it.
                        diff_banner = ""
                        modal_banner = ""
                        try:
                            post_windows = json.loads(windows_json).get("windows", [])
                        except Exception as e:
                            logger.debug("[agent] post-action window list parse failed: %s", e)
                            post_windows = []
                        if pre_windows is not None:
                            pre_ids = {(w.get("id"), w.get("title", "")) for w in pre_windows}
                            post_ids = {(w.get("id"), w.get("title", "")) for w in post_windows}
                            added = post_ids - pre_ids
                            removed = pre_ids - post_ids
                            yield AgentEvent(
                                kind="state_diff",
                                content=(
                                    f"Δ windows added={len(added)} removed={len(removed)} "
                                    f"total {len(pre_windows)}→{len(post_windows)}"
                                ),
                                data={
                                    "added": [t for _, t in added],
                                    "removed": [t for _, t in removed],
                                    "iteration": iteration,
                                },
                            )
                            if not added and not removed:
                                diff_banner = (
                                    " [State-diff: no window changes — the action "
                                    "may not have taken effect; verify carefully.]"
                                )
                            else:
                                diff_banner = (
                                    f" [State-diff: +{len(added)} -{len(removed)} windows.]"
                                )
                            modal_pat = re.compile(
                                r"\b(error|warning|sign[ -]?in|password|allow|"
                                r"permission|are you sure|confirm|update available)\b",
                                re.IGNORECASE,
                            )
                            modal_titles = [t for _, t in added if t and modal_pat.search(t)]
                            if modal_titles:
                                modal_banner = (
                                    " [UNEXPECTED MODAL: new window(s) "
                                    + ", ".join(repr(t) for t in modal_titles)
                                    + " — handle this dialog before continuing.]"
                                )
                        self._history.append({
                            "role": "user",
                            "content": (
                                f"[Auto-verify after {verify_label}] "
                                f"Current desktop state: {windows_json}"
                                + diff_banner
                                + modal_banner
                                + launch_reminder
                            ),
                        })
                        yield AgentEvent(
                            kind="tool_result",
                            content=f"Auto-verify windows: {windows_json[:200]}",
                            data={"tool_name": "desktop_list_windows", "iteration": iteration},
                        )
                    except Exception as e:
                        logger.debug("[agent] auto-verify window listing failed: %s", e)

                # Always take a screenshot after desktop actions so there is a
                # real file on disk the user can inspect.  For vision-on models
                # the image is also injected into history; for vision-off models
                # the file is saved silently (model does not see the base64).
                if (
                    "desktop_screenshot" in available_tools
                    and "desktop_screenshot" not in called_names
                ):
                    try:
                        shot_json = await self._registry.dispatch("desktop_screenshot", {})
                        result_obj = json.loads(shot_json)
                        b64 = result_obj.pop("base64_png", None)
                        path_str = result_obj.get("path", "?")
                        dims = f"{result_obj.get('width')}×{result_obj.get('height')}"

                        # Detect a screenshot-tool failure first: the base
                        # backend returns {"error": ...} (no path/base64)
                        # when PIL is missing or ImageGrab raises.  Without
                        # this branch, the message below would mislead with
                        # "saved" + path "?" + "None×None".
                        shot_error = result_obj.get("error")
                        if shot_error:
                            yield AgentEvent(
                                kind="tool_result",
                                content=f"Auto-screenshot FAILED: {str(shot_error)[:200]}",
                                data={"tool_name": "desktop_screenshot", "iteration": iteration, "error": shot_error},
                            )
                        elif self._vision_screenshots and b64:
                            self._history.append({
                                "role": "user",
                                "content": [
                                    {
                                        "type": "text",
                                        "text": (
                                            f"[Auto-verify screenshot after {verify_label}]\n"
                                            + self._prompts.text("runtime_screenshot_verify")
                                        ),
                                    },
                                    {
                                        "type": "image_url",
                                        "image_url": {"url": "data:image/png;base64," + b64},
                                    },
                                ],
                            })
                            yield AgentEvent(
                                kind="tool_result",
                                content=f"Auto-verify screenshot: {path_str} ({dims})",
                                data={"tool_name": "desktop_screenshot", "iteration": iteration},
                            )
                        else:
                            # Vision branch skipped: clarify which of the two
                            # possible reasons fired so the user isn't told
                            # "vision off" when their config has it on.
                            if not self._vision_screenshots:
                                why = "vision off in config (agent.vision_screenshots)"
                            elif not b64:
                                why = (
                                    "vision on but screenshot tool returned no "
                                    "base64_png (backend may have failed silently)"
                                )
                            else:
                                why = "skipped"
                            yield AgentEvent(
                                kind="tool_result",
                                content=f"Auto-screenshot saved ({why}): {path_str} ({dims})",
                                data={"tool_name": "desktop_screenshot", "iteration": iteration},
                            )
                    except Exception as e:
                        logger.debug("[agent] auto-screenshot after desktop action failed: %s", e)

        # ---- Iteration ceiling reached --------------------------------
        yield AgentEvent(
            kind="error",
            content=f"Max iterations ({self._max_iterations}) reached without a final answer.",
            data={"iterations": iteration},
        )
        yield AgentEvent(kind="done", content="Agent stopped (max iterations).", data={"iterations": iteration})

    def reset(self):
        """Clear conversation history, preserving the system prompt."""
        self._history = [{"role": "system", "content": self._system_prompt}]
        self._completed_actions.clear()
        self._session_steps.clear()
        self._last_iteration_had_failure = False
        self._last_validator_verdict = None
        logger.info("[agent.py:reset] Conversation history cleared.")

    def shutdown(self):
        """Best-effort release of background resources (rolling recorder,
        trace writer).  Safe to call multiple times."""
        try:
            if self._recorder is not None:
                self._recorder.stop()
                self._recorder = None
        except Exception as e:
            logger.debug("[agent] recorder stop during shutdown failed: %s", e)
        try:
            if self._trace is not None:
                self._trace.close()
                self._trace = None
        except Exception as e:
            logger.debug("[agent] trace close during shutdown failed: %s", e)

    def __del__(self):
        try:
            self.shutdown()
        except Exception:
            # Deliberately silent: __del__ may run during interpreter
            # teardown when the logging machinery is already gone.
            pass
