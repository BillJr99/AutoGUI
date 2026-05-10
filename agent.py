"""
agent.py — Core agentic loop.

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
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable

from artifacts import ArtifactStore
from client import OpenWebUIClient
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
from failures import FailureClass, RecoveryAction, classify, escalate_action
from planner import Planner
from progress import ProgressStore
from prompt_loader import PromptLoader
from screen_record import ScreenRecorder
from skills import SkillStore
from subagent import Subagent
from tools import ToolRegistry
from trace import TraceWriter

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# OS detection
# ---------------------------------------------------------------------------

def _proc_version_has_microsoft() -> bool:
    try:
        from pathlib import Path
        return "microsoft" in Path("/proc/version").read_text().lower()
    except OSError:
        return False


def _build_os_instructions(loader: PromptLoader) -> str:
    """Return OS-specific instructions by loading the appropriate prompt file."""
    system = _platform.system()
    release = _platform.release().lower()
    is_wsl = system == "Linux" and (
        "microsoft" in release or "wsl" in release or _proc_version_has_microsoft()
    )
    if is_wsl:
        name = "system_os_wsl"
    elif system == "Windows":
        name = "system_os_windows"
    elif system == "Darwin":
        name = "system_os_macos"
    else:
        name = "system_os_linux"
    return loader.text(name)


# ---------------------------------------------------------------------------
# Event data classes
# ---------------------------------------------------------------------------

@dataclass
class AgentEvent:
    """
    A single event emitted by the agent loop.

    Fields
    ------
    kind : str
        One of "text", "tool_call", "tool_result", "error", "done".
    content : str
        Human-readable content string appropriate to the kind.
    data : dict
        Structured payload (tool name/args, result dict, iteration count, etc.).
    """
    kind: str
    content: str
    data: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------

class Agent:
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
        # noise.
        controller_protocol = ""
        if (self._agent_cfg.get("controller", {}) or {}).get("enabled", False):
            controller_protocol = self._prompts.text("controller_step_protocol")

        self._system_prompt: str = "\n\n".join(filter(None, [
            base,
            _build_os_instructions(self._prompts),
            self._prompts.text("system_desktop_rules"),
            self._prompts.text("system_browser_rules"),
            self._prompts.text("system_tool_analysis"),
            controller_protocol,
        ]))

        # Seconds to wait before dispatching each tool call (0 = immediate).
        self._confirm_delay: int = int(cfg.get("safety", {}).get("command_confirm_delay_seconds", 0))
        # When True, screenshot base64 is delivered as an image_url vision message
        # so vision-capable models can actually see it.  Set False for text-only models.
        self._vision_screenshots: bool = bool(self._agent_cfg.get("vision_screenshots", True))
        logger.info("[agent] vision_screenshots=%s", self._vision_screenshots)

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
        # Skills are OPT-IN: when skills_enabled is false (the default), the
        # SkillStore is not created and the skill_save / skill_list /
        # skill_run tools are not registered, so no skills/ directory is
        # written to disk and the conversation history doesn't carry
        # skill-suggestion blocks.  Set agent.skills_enabled=true to record
        # successful tool sequences as replayable macros.
        self._skills_enabled: bool = bool(self._agent_cfg.get("skills_enabled", False))
        self._suggest_skills: bool = bool(self._agent_cfg.get("suggest_skills", True))
        self._record_trace: bool = bool(self._agent_cfg.get("record_trace", True))

        try:
            self._trace = TraceWriter(trace_dir) if self._record_trace else None
        except Exception as e:
            logger.warning("[agent] TraceWriter init failed: %s", e)
            self._trace = None

        if self._skills_enabled:
            try:
                self._skill_store = SkillStore(skills_path)
            except Exception as e:
                logger.warning("[agent] SkillStore init failed: %s", e)
                self._skill_store = None
        else:
            self._skill_store = None
            logger.info("[agent] skills disabled (agent.skills_enabled=false)")

        if self._trace:
            try:
                self._trace.write_meta(
                    event="session_start",
                    model=cfg.get("openwebui", {}).get("model", "?"),
                    vision=self._vision_screenshots,
                )
            except Exception:
                pass

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
        self._controller_enabled: bool = bool(controller_cfg.get("enabled", False))
        self._step_max_iterations: int = max(1, int(controller_cfg.get("step_max_iterations", 8)))
        self._step_max_retries: int = max(0, int(controller_cfg.get("step_max_retries", 2)))
        self._auto_resume: bool = bool(controller_cfg.get("auto_resume", True))
        self._replan_on_block: bool = bool(controller_cfg.get("replan_on_block", True))

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
        self._task_progress = None
        self._step_retry_counts: dict[str, int] = {}

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
    # Skill tool registration
    # ------------------------------------------------------------------

    def _register_skill_tools(self):
        """Add skill_save / skill_list / skill_run to the registry."""
        store = self._skill_store
        registry = self._registry

        async def _skill_save(name: str, keywords=None, app: str = "") -> dict:
            if not self._session_steps:
                return {"error": "No successful steps in this session yet to save."}
            kw = keywords or []
            if isinstance(kw, str):
                kw = [k.strip() for k in re.split(r"[,;\s]+", kw) if k.strip()]
            try:
                skill = store.save(
                    name=str(name),
                    keywords=list(kw),
                    app=str(app or ""),
                    steps=list(self._session_steps),
                )
                return {"success": True, "name": skill["name"], "step_count": len(skill["steps"])}
            except Exception as e:
                return {"error": str(e)}

        async def _skill_list(query: str = "", limit: int = 5) -> dict:
            try:
                results = store.search(str(query) if query else "", limit=int(limit) if limit else 5)
                return {
                    "skills": [
                        {
                            "name": s.get("name"),
                            "app": s.get("app", ""),
                            "keywords": s.get("keywords", []),
                            "step_count": len(s.get("steps", [])),
                            "success_count": s.get("success_count", 0),
                        }
                        for s in results
                    ],
                    "count": len(results),
                }
            except Exception as e:
                return {"error": str(e)}

        async def _skill_run(name: str) -> dict:
            from skills import normalize_skill_steps
            skill = store.get(str(name))
            if not skill:
                return {"error": f"No skill named {name!r}"}
            executed: list[dict] = []
            for step in normalize_skill_steps(skill.get("steps", [])):
                tool = step.get("tool")
                args = step.get("args", {}) or {}
                if not tool:
                    continue
                result_json = await registry.dispatch(tool, args)
                try:
                    result = json.loads(result_json)
                except json.JSONDecodeError:
                    result = {"raw": result_json[:120]}
                executed.append({"tool": tool, "args": args, "ok": "error" not in result})
                if "error" in result:
                    return {
                        "skill": name,
                        "executed": executed,
                        "stopped_at": tool,
                        "error": result["error"],
                    }
            try:
                store.increment_success(str(name))
            except Exception:
                pass
            return {"skill": name, "executed": executed, "step_count": len(executed), "success": True}

        registry.add_tool(
            {"type": "function", "function": {
                "name": "skill_save",
                "description": (
                    "Save the sequence of tool calls completed in this session as a "
                    "named, replayable skill. Provide keywords describing when this "
                    "skill applies (e.g. 'open weather forecast in browser'). "
                    "Call this only after the task has succeeded — earlier failed "
                    "attempts in the same session are not included. "
                    "Saved skills can later be invoked by skill_run or replayed "
                    "outside the agent via replay.py. "
                    "NOTE: pixel-coordinate desktop_click steps used for window focus "
                    "are automatically dropped on replay (coordinates change between runs). "
                    "Prefer desktop_activate_window for focus — it is position-independent."
                ),
                "parameters": {"type": "object", "properties": {
                    "name": {"type": "string", "description": "Short unique identifier for the skill."},
                    "keywords": {"type": "array", "items": {"type": "string"},
                                 "description": "Words/phrases that describe when to use this skill."},
                    "app": {"type": "string",
                            "description": "Primary app or context this skill targets (optional)."},
                }, "required": ["name"]},
            }},
            _skill_save,
        )
        registry.add_tool(
            {"type": "function", "function": {
                "name": "skill_list",
                "description": (
                    "List saved skills, optionally filtered by a search query. "
                    "Use this at the start of a task to check whether a known "
                    "procedure already exists for what the user asked."
                ),
                "parameters": {"type": "object", "properties": {
                    "query": {"type": "string", "description": "Optional keyword filter."},
                    "limit": {"type": "integer", "description": "Max skills to return (default 5)."},
                }},
            }},
            _skill_list,
        )
        registry.add_tool(
            {"type": "function", "function": {
                "name": "skill_run",
                "description": (
                    "Replay every step of a previously saved skill in order. "
                    "Stops at the first failing step. Use only when the current "
                    "screen state and target app match the conditions under which "
                    "the skill was originally recorded."
                ),
                "parameters": {"type": "object", "properties": {
                    "name": {"type": "string"},
                }, "required": ["name"]},
            }},
            _skill_run,
        )

    # ------------------------------------------------------------------
    # Meta-tools: artifact, plan, checkpoint, ask
    # ------------------------------------------------------------------

    def _register_meta_tools(self):
        """Add get_artifact / list_artifacts / plan_get / plan_update_step /
        checkpoint / ask_subagent to the registry.  Always registered when
        the supporting stores exist, so the model can opt to use them."""
        registry = self._registry
        agent = self

        if self._artifacts is not None:
            async def _get_artifact(id: str) -> dict:
                aid = str(id or "")
                if not aid.startswith("artifact://"):
                    aid = "artifact://" + aid.lstrip("/")
                art = agent._artifacts.get(aid)
                if art is None:
                    return {"error": f"unknown artifact id: {id}"}
                body = agent._artifacts.get_body(aid) or ""
                return {
                    "id": aid,
                    "kind": art.kind,
                    "source": art.source,
                    "summary": art.summary,
                    "bytes": art.bytes_len,
                    "content": body,
                }

            async def _list_artifacts(kind: str = "", limit: int = 10) -> dict:
                items = agent._artifacts.list_recent(kind=kind or None, limit=int(limit) or 10)
                return {
                    "count": len(items),
                    "artifacts": [
                        {"id": a.id, "kind": a.kind, "source": a.source,
                         "summary": a.summary, "bytes": a.bytes_len}
                        for a in items
                    ],
                }

            registry.add_tool(
                {"type": "function", "function": {
                    "name": "get_artifact",
                    "description": (
                        "Fetch the body of a previously stored artifact (file content, "
                        "command output, OCR snippet) by id.  Use this when the agent "
                        "context only shows an artifact summary and you need the full text."
                    ),
                    "parameters": {"type": "object", "properties": {
                        "id": {"type": "string", "description": "artifact://<id> or just the id."},
                    }, "required": ["id"]},
                }},
                _get_artifact,
            )
            registry.add_tool(
                {"type": "function", "function": {
                    "name": "list_artifacts",
                    "description": (
                        "List recent artifacts captured during this task.  Use to find "
                        "a previously-read file before re-reading it from disk."
                    ),
                    "parameters": {"type": "object", "properties": {
                        "kind": {"type": "string"},
                        "limit": {"type": "integer"},
                    }},
                }},
                _list_artifacts,
            )

        async def _plan_get() -> dict:
            if agent._plan is None:
                return {"plan": None, "note": "No structured plan in this session."}
            return {
                "plan": agent._plan.to_dict(),
                "progress": agent._plan.progress_summary(),
            }

        async def _plan_update_step(
            id: str,
            status: str = "",
            notes: str = "",
        ) -> dict:
            if agent._plan is None:
                return {"error": "No structured plan in this session."}
            step = agent._plan.by_id(str(id))
            if step is None:
                return {"error": f"no step with id {id!r}"}
            if status:
                try:
                    step.status = StepStatus(status)
                except ValueError:
                    return {"error": f"invalid status {status!r}"}
            if notes:
                step.notes = (step.notes + "\n" + notes).strip() if step.notes else notes
            agent._persist_progress()
            return {"step": step.to_public()}

        registry.add_tool(
            {"type": "function", "function": {
                "name": "plan_get",
                "description": (
                    "Return the current typed plan (steps, ids, statuses).  Use to "
                    "remind yourself which step the controller expects you to work on."
                ),
                "parameters": {"type": "object", "properties": {}},
            }},
            _plan_get,
        )
        registry.add_tool(
            {"type": "function", "function": {
                "name": "plan_update_step",
                "description": (
                    "Manually mark a plan step done/skipped/blocked, or attach notes.  "
                    "The controller marks STEP_DONE / STEP_BLOCKED automatically; use "
                    "this only to skip an obsolete step or annotate progress."
                ),
                "parameters": {"type": "object", "properties": {
                    "id": {"type": "string"},
                    "status": {"type": "string", "enum": [
                        "pending", "running", "done", "failed", "skipped", "blocked",
                    ]},
                    "notes": {"type": "string"},
                }, "required": ["id"]},
            }},
            _plan_update_step,
        )

        if self._progress is not None:
            async def _checkpoint(label: str = "", data: dict | str = "") -> dict:
                if agent._task_progress is None:
                    return {"note": "no active task progress record"}
                payload: dict = {}
                if isinstance(data, dict):
                    payload = dict(data)
                elif isinstance(data, str) and data:
                    payload = {"note": data}
                if label:
                    payload["label"] = label
                agent._progress.update_checkpoint(agent._task_progress, payload)
                return {"saved": True, "checkpoint": payload}

            registry.add_tool(
                {"type": "function", "function": {
                    "name": "checkpoint",
                    "description": (
                        "Persist a free-form progress marker so the task can resume "
                        "after a crash or abort.  Use after non-trivial milestones "
                        "(\"finished tab 3 of 7\", \"wrote intermediate output\")."
                    ),
                    "parameters": {"type": "object", "properties": {
                        "label": {"type": "string"},
                        "data": {"type": "object"},
                    }},
                }},
                _checkpoint,
            )

        if self._subagent is not None:
            async def _ask_subagent(
                question: str,
                artifact_ids=None,
            ) -> dict:
                fetched: list[tuple[str, str]] = []
                if isinstance(artifact_ids, list) and agent._artifacts is not None:
                    for aid in artifact_ids[:6]:
                        body = agent._artifacts.get_body(str(aid))
                        if body:
                            art = agent._artifacts.get(str(aid))
                            label = art.summary if art else str(aid)
                            fetched.append((label[:80], body))
                result = await agent._subagent.ask(
                    str(question),
                    fetched_artifacts=fetched or None,
                )
                return {
                    "answer": result.answer,
                    "artifact_ids": result.artifact_ids,
                    "tool_calls_made": result.tool_calls_made,
                }

            registry.add_tool(
                {"type": "function", "function": {
                    "name": "ask_subagent",
                    "description": (
                        "Delegate a read-only lookup question to a focused subagent so "
                        "the answer doesn't bloat the main conversation history.  Good "
                        "for \"which of these N files mentions X\", \"summarise this JSON\", "
                        "and similar pure-read tasks.  The subagent has no desktop or shell "
                        "access — only fs_read / fs_list / browser_get_text."
                    ),
                    "parameters": {"type": "object", "properties": {
                        "question": {"type": "string"},
                        "artifact_ids": {"type": "array", "items": {"type": "string"},
                                         "description": "Optional pre-fetched artifact ids."},
                    }, "required": ["question"]},
                }},
                _ask_subagent,
            )

    def _persist_progress(self):
        """Snapshot the live plan into the progress store (no-op if disabled)."""
        if self._progress is None or self._task_progress is None or self._plan is None:
            return
        try:
            self._progress.update_plan_snapshot(self._task_progress, self._plan.to_dict())
        except Exception as e:
            logger.debug("[agent] progress snapshot failed: %s", e)

    def _resume_plan_state(self) -> None:
        """Apply persisted completed_step_ids onto the freshly parsed plan."""
        if self._plan is None or self._task_progress is None:
            return
        for sid in self._task_progress.completed_step_ids:
            step = self._plan.by_id(sid)
            if step is not None and step.status == StepStatus.PENDING:
                step.status = StepStatus.DONE
        for sid in self._task_progress.failed_step_ids:
            step = self._plan.by_id(sid)
            if step is not None and step.status == StepStatus.PENDING:
                step.status = StepStatus.FAILED

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
                except Exception:
                    pass
            if self._event_sink is not None:
                try:
                    self._event_sink(event)
                except Exception:
                    pass
            yield event

    # ------------------------------------------------------------------
    # Controller-driven path
    # ------------------------------------------------------------------

    async def _run_with_controller(self, user_input: str) -> AsyncIterator[AgentEvent]:
        """
        Plan -> step-by-step execution.

        For each PENDING step the controller composes a scoped prompt
        (see controller.build_step_prompt), drives the executor loop in a
        per-step history, and observes the step's STEP_DONE / STEP_BLOCKED
        marker plus any tool failures.  Plan progress is persisted after
        every step.
        """
        available_tools = set(self._registry.list_tools())
        windows_json = ""
        if "desktop_list_windows" in available_tools:
            try:
                windows_json = await self._registry.dispatch("desktop_list_windows", {})
            except Exception:
                pass

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
            )
        except Exception as e:
            logger.warning("[agent] typed planner failed: %s", e)
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

            step.status = StepStatus.RUNNING
            step.attempts += 1
            self._step_retry_counts[step.id] = self._step_retry_counts.get(step.id, 0)
            self._persist_progress()
            yield AgentEvent(
                kind="step_start",
                content=f"→ ({step.id}) {step.goal}",
                data={"step": step.to_public(), "attempts": step.attempts},
            )

            verdict, reason, used_iters, failure = await self._run_step(
                user_input=user_input,
                step=step,
                event_yield=None,  # we re-yield via the queue below
            )
            global_iter += used_iters

            # Process the verdict.
            if verdict == StepVerdict.DONE:
                step.status = StepStatus.DONE
                step.last_error = ""
                if self._task_progress is not None:
                    self._progress.mark_done(self._task_progress, step.id)
                self._persist_progress()
                yield AgentEvent(
                    kind="step_done",
                    content=f"✓ ({step.id}) {reason[:160]}",
                    data={"step": step.to_public(), "iterations": used_iters},
                )
                continue

            # Failed / blocked / exhausted — classify and decide.
            self._step_retry_counts[step.id] += 1
            retry_count = self._step_retry_counts[step.id]
            verdict_failure = failure or classify(
                tool_name="(none)",
                error_message=reason,
                predicate_failed=verdict == StepVerdict.BLOCKED,
            )
            action = escalate_action(
                verdict_failure,
                retry_count=retry_count,
                max_retries=self._step_max_retries,
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
            if self._task_progress is not None:
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
        if self._task_progress is not None:
            self._progress.finalize(self._task_progress, status=final_status)
        yield AgentEvent(
            kind="done",
            content=f"Controller {final_status} — {plan.progress_summary()}",
            data={
                "plan": plan.to_dict(),
                "iterations": global_iter,
                "status": final_status,
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
        """
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

        last_text = ""
        last_failure = None
        iterations = 0

        while iterations < self._step_max_iterations:
            iterations += 1
            try:
                response = await self._client.chat(
                    messages=local_history,
                    tools=self._registry.schemas,
                )
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
                # Final assistant message — parse the marker.
                verdict, reason = parse_step_outcome(text)
                return verdict, reason or text[:200], iterations, last_failure

            # Dispatch each tool call.
            step_failure_seen = False
            for tc in tool_calls:
                tool_name = tc.get("function", {}).get("name", "")
                call_id = tc.get("id", "")
                try:
                    args = json.loads(tc.get("function", {}).get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                result_json = await self._registry.dispatch(tool_name, args)

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
                    self._session_steps.append({"tool": tool_name, "args": dict(args)})

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

    def _maybe_store_artifact(self, tool_name: str, args: dict, result_json: str) -> str:
        """
        For tools whose results are large bodies (fs_read, browser_get_text,
        desktop_get_window_text), store the body as an artifact and return a
        slimmed-down history payload.  Other results pass through unchanged.
        """
        if self._artifacts is None:
            return result_json
        try:
            result = json.loads(result_json)
        except json.JSONDecodeError:
            return result_json
        if "error" in result:
            return result_json

        body_field, source = None, ""
        if tool_name == "fs_read":
            body_field = "content"
            source = str(args.get("path", ""))
        elif tool_name == "desktop_get_window_text":
            body_field = "text"
        elif tool_name == "browser_get_text":
            body_field = "text"
            source = str(args.get("selector", "") or "page")
        elif tool_name == "shell_run":
            # Stdout only when it's large enough to bloat history.
            stdout = result.get("stdout") or ""
            if isinstance(stdout, str) and len(stdout) > 4096:
                aid = self._artifacts.put(
                    stdout, kind="shell_stdout",
                    source=str(args.get("command", ""))[:120],
                )
                preview = stdout[:400] + "\n...\n[stored as " + aid + "]"
                result["stdout"] = preview
                result["stdout_artifact_id"] = aid
                return json.dumps(result, default=str)
            return result_json

        if body_field is not None:
            body = result.get(body_field)
            if isinstance(body, str) and len(body) > 4096:
                aid = self._artifacts.put(
                    body, kind=tool_name, source=source,
                )
                preview = body[:600] + "\n...\n[truncated; full content stored as " + aid + "]"
                result[body_field] = preview
                result[body_field + "_artifact_id"] = aid
                return json.dumps(result, default=str)
        return result_json

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
            except Exception:
                pass

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
            except Exception:
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
            except Exception:
                self._history.append({"role": "user",
                                      "content": user_input + initial_suffix})
        else:
            self._history.append({"role": "user", "content": user_input + initial_suffix})

        iteration = 0

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
                tc.get("function", {}).get("name", "") for tc in tool_calls
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
                except Exception:
                    pre_windows = None

            for tc in tool_calls:
                tool_name = tc.get("function", {}).get("name", "unknown")
                call_id = tc.get("id", "")
                raw_args = tc.get("function", {}).get("arguments", "{}")

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
                    except Exception:
                        pass  # fall back to original history_content

                # ---- fs_read: annotate result so model must analyze it -----
                if tool_name == "fs_read" and not self._result_is_error(result_json):
                    try:
                        r = json.loads(result_json)
                        if r.get("content"):
                            history_content = (
                                self._prompts.text("runtime_fs_read_annotation") + "\n"
                                + history_content
                            )
                    except Exception:
                        pass

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
                    except Exception:
                        pass

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
                        self._session_steps.append({"tool": tool_name, "args": dict(args)})

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
            called_names = {tc.get("function", {}).get("name", "") for tc in tool_calls}
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
                        except Exception:
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
                    except Exception:
                        pass

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

                        if self._vision_screenshots and b64:
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
                            # Vision off: file is saved to disk but not shown to the model.
                            yield AgentEvent(
                                kind="tool_result",
                                content=f"Auto-screenshot saved (vision off): {path_str} ({dims})",
                                data={"tool_name": "desktop_screenshot", "iteration": iteration},
                            )
                    except Exception:
                        pass

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
        except Exception:
            pass
        try:
            if self._trace is not None:
                self._trace.close()
                self._trace = None
        except Exception:
            pass

    def __del__(self):
        try:
            self.shutdown()
        except Exception:
            pass

    @property
    def history(self) -> list[dict]:
        """Return a read-only view of the current message history."""
        return list(self._history)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    # Phrases the model uses when it narrates an action instead of calling a tool.
    # Only match FUTURE / PRESENT intent — never past tense, which is a completion summary.
    # Checked against lower-cased response text.
    # Action narration patterns — always checked regardless of vision mode.
    # Catches the model describing what it plans to do instead of calling a tool.
    _NARRATED_ACTION_PATTERNS = [
        # "I will type / click / launch / take a screenshot …"
        r"\bi (?:will |am going to |am about to )(?:now )?"
        r"(?:type|enter|click|press|launch|start|run|execute|perform|open"
        r"|take|capture|check|verify|use|call|send|move|focus|switch|navigate|go to|visit)\b",
        # "Next, I will …"  |  "Now I will …"  |  "Then I will …"
        r"\b(?:next|now|then)(?:,)? i (?:will|am going to|need to|should|can)\b",
        # "I am currently typing …"  |  "I am now clicking …"
        r"\bi am (?:currently |now )?(?:typing|entering|clicking|pressing|launching|starting|running|executing|performing|opening|taking|capturing|checking|navigating)\b",
        # "I now type …"  |  "Let me type / launch …"
        r"\bi now (?:type|enter|click|press|launch|start|run|execute|perform|open|take|capture|check|navigate)\b",
        r"\blet me (?:now )?(?:type|enter|click|press|launch|start|run|execute|open|take|capture|check|navigate)\b",
        # "the text is being typed / will be entered"
        r"\bthe text (?:is being|will be) (?:typed?|entered?|written|inserted?)\b",
        # Model writing a tool call as function-call syntax in text instead of calling it.
        r"\b(?:desktop_(?:launch|type|click|screenshot|hotkey|scroll|list_windows)"
        r"|shell_run|fs_(?:read|write|list))\s*\(",
        # Model writing a tool name as a quoted JSON string in its response.
        r"""["'](?:desktop_(?:launch|type|click|screenshot|hotkey|scroll|list_windows)"""
        r"""|shell_run|fs_(?:read|write|list))["']""",
        # Model listing remaining/next steps as text instead of executing them.
        r"\bremaining steps?\b",
        r"\bnext steps?\s*[:\-]",
        r"\bsteps? (?:remaining|left|to (?:complete|finish|take|accomplish))\b",
        r"\bi (?:still |also )?need to (?:navigate|click|type|go|visit|open|close|press|enter|launch|find|select|scroll)\b",
        r"\bi (?:need|should|must|have to) (?:now )?(?:navigate to|go to|visit|click|type|open|close|press|enter|launch|find|select)\b",
        r"\bto (?:complete|finish|accomplish) (?:this )?(?:task|request|goal),? i (?:need|should|will|must)\b",
    ]

    # Screenshot hallucination patterns — ONLY checked when vision is OFF.
    # When vision is on, "the screenshot shows…" is legitimate analysis, not hallucination.
    _SCREENSHOT_HALLUCINATION_PATTERNS = [
        r"\bthe screenshot (?:shows?|reveals?|displays?|confirms?|indicates?)\b",
        r"\bi (?:can |could )?see\b.{0,40}\bscreen(?:shot)?\b",
        r"\blooking at the screenshot\b",
        r"\bfrom (?:the )?screenshot\b",
        r"\bbased on (?:the )?screenshot\b",
        r"\bthe screen (?:shows?|displays?|reveals?|now shows?)\b",
        r"\bi (?:took|captured|took a|captured a) screenshot\b",
        r"\bi (?:generated|created|produced|saved) a screenshot\b",
        r"\bscreenshot (?:was|has been|is) (?:taken|captured|saved|generated|complete)\b",
        r"\bi (?:verified?|confirmed?|checked?|inspected?) (?:the |via )?screenshot\b",
        r"\bby (?:taking|looking at|checking|reviewing|examining) the screenshot\b",
    ]

    # Phrases that clearly signal the model is abandoning the task.
    # Deliberately narrow — past-tense error descriptions must NOT match.
    _GIVING_UP_PATTERNS = [
        # "I'm sorry, but I cannot help/assist with this"
        r"\bi'?m sorry,? (?:but )?i (?:cannot|can'?t|am unable to) (?:help|assist)(?:\s+you)?(?:\s+with)?(?:\s+this)?\b",
        # "I cannot complete / accomplish / perform this task/request"
        r"\bi (?:cannot|can'?t|am unable to) (?:complete|accomplish|perform|execute|finish) this (?:task|request)\b",
        # "This task is beyond my capabilities / scope"
        r"\bthis (?:task|request) (?:is )?(?:beyond|outside) (?:my |the )?(?:capabilities|scope|ability|limitations)\b",
        # Explicit abandonment phrases
        r"\b(?:i give up|i am giving up|task (?:has )?failed|cannot proceed(?: with this)?)\b",
        # Model confused by tool result / forgot the task (common in Gemma)
        r"\bno specific question or instruction\b",
        r"\bnot in a format (?:i|that i) can (?:process|understand|read|interpret)\b",
        r"\bplease provide (?:the query|a query|your (?:question|request|task))\b",
        r"\bwhat (?:would you like|do you want|can i help) (?:me to do|you with)\b",
        r"\bi(?:'m| am) not sure what (?:you(?:'re| are) asking|the task is|you want)\b",
        r"\bcould you (?:please )?(?:clarify|rephrase|provide more|specify)\b",
    ]

    @classmethod
    def _text_implies_skipped_actions(cls, text: str, vision_on: bool = False) -> bool:
        """
        Return True when a stop-response looks like the model described tool-call
        actions in prose rather than actually issuing them.

        vision_on: when True, screenshot-description patterns are NOT flagged
        (the model legitimately describes images it received).
        """
        lower = text.lower()
        if any(re.search(pat, lower) for pat in cls._NARRATED_ACTION_PATTERNS):
            return True
        # Screenshot hallucination patterns only apply when the model has NOT
        # received any actual images — i.e., vision is disabled.
        if not vision_on:
            if any(re.search(pat, lower) for pat in cls._SCREENSHOT_HALLUCINATION_PATTERNS):
                return True
        return False

    @classmethod
    def _text_implies_giving_up(cls, text: str) -> bool:
        """Return True when the model's stop-response sounds like it is giving up."""
        lower = text.lower()
        return any(re.search(pat, lower) for pat in cls._GIVING_UP_PATTERNS)

    # ------------------------------------------------------------------
    # Best-of-N sampling (Phase 7)
    # ------------------------------------------------------------------

    def _bon_should_trigger(self) -> bool:
        if not self._bon_enabled:
            return False
        if self._bon_trigger_on_failure and self._last_iteration_had_failure:
            return True
        if self._bon_trigger_on_validator and self._last_validator_verdict:
            v = (self._last_validator_verdict or "").upper()
            if not v.startswith("APPROVED"):
                return True
        return False

    async def _bon_sample(
        self,
    ) -> tuple[dict, str]:
        """
        Sample N candidate completions from the primary client, then ask
        the same client (acting as a verifier with no tools) which is
        best.

        Returns (chosen_response, rationale).  The chosen response has the
        same shape as a normal client.chat() return so the caller can keep
        using extract_message / extract_tool_calls unchanged.

        Falls back to a single greedy call if anything goes wrong — BoN
        should never make the agent worse than baseline.
        """
        history = self._history
        tools_schema = self._registry.schemas

        # Sample N proposals concurrently with elevated temperature.
        try:
            tasks = [
                self._client.chat(
                    messages=history,
                    tools=tools_schema,
                    temperature=self._bon_temperature,
                )
                for _ in range(self._bon_n)
            ]
            responses = await asyncio.gather(*tasks, return_exceptions=True)
        except Exception as e:
            logger.warning("[agent.py:_bon_sample] gather failed: %s", e)
            return await self._client.chat(messages=history, tools=tools_schema), "bon-failed-greedy"

        candidates: list[tuple[int, dict, str]] = []
        for i, resp in enumerate(responses):
            if isinstance(resp, Exception):
                continue
            try:
                msg = self._client.extract_message(resp)
                summary = self._summarize_candidate(msg)
                candidates.append((i, resp, summary))
            except Exception:
                continue

        if not candidates:
            return await self._client.chat(messages=history, tools=tools_schema), "bon-no-candidates-greedy"

        if len(candidates) == 1:
            return candidates[0][1], "bon-single-candidate"

        # Quick self-consistency check: if a strong majority of candidates
        # propose the same first tool name + arg signature, pick that without
        # paying for the verifier round-trip.
        signatures: dict[str, list[int]] = {}
        for i, _resp, summary in candidates:
            signatures.setdefault(summary, []).append(i)
        if len(candidates) >= 3:
            best_sig, best_idxs = max(signatures.items(), key=lambda kv: len(kv[1]))
            if len(best_idxs) >= max(2, (len(candidates) + 1) // 2):
                idx = best_idxs[0]
                for i, resp, summary in candidates:
                    if i == idx:
                        return resp, f"bon-consensus({len(best_idxs)}/{len(candidates)}): {summary[:120]}"

        # No consensus → ask the verifier model.
        verifier = self._client
        block = "\n\n".join(
            f"[{i+1}] {summary}" for i, _resp, summary in candidates
        )
        verifier_messages = [
            {
                "role": "system",
                "content": (
                    "You are a verifier picking the single best next action for "
                    "a desktop automation agent. Consider only correctness, "
                    "safety, and alignment with the user's task. Answer with "
                    "ONLY the integer index of the best candidate (1-based)."
                ),
            },
            {
                "role": "user",
                "content": (
                    "User task / current state (recent history follows):\n"
                    + self._brief_history_summary() + "\n\n"
                    + "Candidates:\n" + block + "\n\n"
                    "Reply with just the index, no explanation."
                ),
            },
        ]
        try:
            v_resp = await verifier.chat(messages=verifier_messages, tools=None)
            v_msg = verifier.extract_message(v_resp)
            v_text = (verifier.extract_text(v_msg) or "").strip()
            m = re.search(r"\d+", v_text)
            if m:
                pick_1based = int(m.group(0))
                pick_idx = pick_1based - 1
                if 0 <= pick_idx < len(candidates):
                    chosen_summary = candidates[pick_idx][2]
                    return candidates[pick_idx][1], f"bon-verifier picked {pick_1based}: {chosen_summary[:120]}"
        except Exception as e:
            logger.warning("[agent.py:_bon_sample] verifier failed: %s", e)

        # Verifier flaked — fall back to the first candidate.
        return candidates[0][1], "bon-verifier-failed-fallback-first"

    @staticmethod
    def _summarize_candidate(message: dict) -> str:
        """One-line description of an assistant message for verifier prompts."""
        text = ""
        if isinstance(message.get("content"), str):
            text = message["content"][:160]
        tcs = message.get("tool_calls") or []
        if tcs:
            parts = []
            for tc in tcs[:3]:
                fn = (tc.get("function") or {})
                name = fn.get("name", "?")
                args = fn.get("arguments", "{}")
                if isinstance(args, str) and len(args) > 100:
                    args = args[:97] + "..."
                parts.append(f"{name}({args})")
            return f"tool_calls: {' | '.join(parts)}" + (f"  text: {text}" if text else "")
        return f"text: {text}" if text else "(empty)"

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

    async def _check_command_coherence(
        self,
        tool_name: str,
        args: dict,
        user_input: str,
    ) -> tuple[bool, dict, str]:
        """
        Ask the LLM (no tools) to validate a proposed command before execution.

        Checks:
        - Syntax validity (well-formed command string)
        - Correct executable for the task (right app name, right path format)
        - Whether the action duplicates something already completed

        Always approves search/locate commands regardless of the task.

        Returns (proceed, final_args, verdict_text).
        - proceed=False blocks execution; final_args may be a corrected version.
        """
        if tool_name == "shell_run":
            cmd_display = args.get("command", str(args))
        else:  # desktop_launch
            app = args.get("application", str(args))
            app_args = args.get("args", [])
            cmd_display = f"{app} {' '.join(str(a) for a in app_args)}".strip()

        # Build OS label for the validator so it can judge path formats.
        system = _platform.system()
        release = _platform.release().lower()
        if system == "Linux" and ("microsoft" in release or "wsl" in release
                                  or _proc_version_has_microsoft()):
            os_label = "WSL (Windows Subsystem for Linux) — Windows apps run via .exe, paths like /mnt/c/..."
        elif system == "Windows":
            os_label = "Windows native — paths use C:\\... backslashes"
        elif system == "Darwin":
            os_label = "macOS — paths use /Applications/... or Homebrew /opt/homebrew/bin"
        else:
            os_label = "Linux — paths use /usr/bin/... or /opt/..."

        # Recent successful actions for duplicate detection.
        recent = self._completed_actions[-8:] if self._completed_actions else []
        recent_block = (
            "Recently completed actions (do NOT duplicate unless the task explicitly needs it):\n"
            + "\n".join(f"  ✓ {a}" for a in recent)
        ) if recent else "No actions completed yet."

        validator_messages = [
            {
                "role": "system",
                "content": self._prompts.render("validator_system", os_label=os_label),
            },
            {
                "role": "user",
                "content": self._prompts.render(
                    "validator_user",
                    user_input=user_input,
                    tool_name=tool_name,
                    cmd_display=cmd_display,
                    recent_block=recent_block,
                ),
            },
        ]

        try:
            response = await self._client.chat(messages=validator_messages, tools=None)
            message = self._client.extract_message(response)
            verdict = self._client.extract_text(message).strip()
        except Exception as e:
            logger.warning("[agent.py:_check_command_coherence] Validator call failed: %s", e)
            return True, args, f"validator error (letting through): {e}"

        # Strip markdown code fences the model might wrap around the JSON.
        verdict = re.sub(r"^```[a-z]*\n?", "", verdict).rstrip("`").strip()

        upper = verdict.upper()

        if upper.startswith("APPROVED"):
            return True, args, "APPROVED"

        if upper.startswith("CORRECTED:"):
            json_str = verdict[len("CORRECTED:"):].strip()
            try:
                corrected = json.loads(json_str)
                logger.info(
                    "[agent.py:_check_command_coherence] Corrected %s args: %s → %s",
                    tool_name, args, corrected,
                )
                return True, corrected, f"CORRECTED: {json_str}"
            except (json.JSONDecodeError, ValueError) as e:
                logger.warning(
                    "[agent.py:_check_command_coherence] Could not parse correction JSON (%s): %s",
                    json_str[:80], e,
                )
                # Correction intended but JSON malformed — let the original through.
                return True, args, f"CORRECTED (parse failed, using original): {verdict[:120]}"

        if upper.startswith("REJECTED:"):
            reason = verdict[len("REJECTED:"):].strip()
            logger.warning(
                "[agent.py:_check_command_coherence] Rejected %s '%s' — %s",
                tool_name, cmd_display, reason,
            )
            return False, args, f"REJECTED: {reason}"

        # Unrecognised format — let through rather than silently blocking valid commands.
        logger.warning(
            "[agent.py:_check_command_coherence] Unexpected verdict format: %s", verdict[:120]
        )
        return True, args, f"unknown verdict (letting through): {verdict[:120]}"

    @staticmethod
    def _result_is_error(result_json: str) -> bool:
        """
        Return True when a tool result clearly indicates failure.
        Checks for the 'error' key, non-zero exit_code, or timed_out flag.
        """
        try:
            result = json.loads(result_json)
        except json.JSONDecodeError:
            return False
        if "error" in result:
            return True
        if result.get("exit_code") not in (None, 0):
            return True
        if result.get("timed_out"):
            return True
        return False

    @staticmethod
    def _summarize_result(tool_name: str, result_json: str) -> str:
        """
        Produce a short human-readable summary of a tool result for display.
        The full result_json is still appended to the history; this is for UI only.
        """
        try:
            result = json.loads(result_json)
        except json.JSONDecodeError:
            return result_json[:200]

        if "error" in result:
            return f"[ERROR] {result['error'][:200]}"

        # Tool-specific summaries
        if tool_name == "shell_run":
            rc = result.get("exit_code", "?")
            out = result.get("stdout", "")[:300]
            err = result.get("stderr", "")[:200]
            parts = [f"exit={rc}"]
            if out:
                parts.append(f"stdout: {out}")
            if err:
                parts.append(f"stderr: {err}")
            return " | ".join(parts)

        if tool_name == "desktop_screenshot":
            return f"Screenshot saved: {result.get('path', '?')} ({result.get('width')}×{result.get('height')})"

        if tool_name in ("desktop_click", "desktop_type", "desktop_hotkey", "desktop_scroll"):
            return f"OK: {result}"

        if tool_name == "fs_read":
            n = len(result.get("content", ""))
            trunc = " [truncated]" if result.get("truncated") else ""
            return f"Read {n} chars{trunc}"

        if tool_name == "fs_list":
            return f"Listed {result.get('count', 0)} entries"

        if tool_name == "fs_write":
            return f"Wrote {result.get('bytes_written', '?')} bytes to {result.get('path', '?')}"

        # Generic fallback
        return str(result)[:300]
