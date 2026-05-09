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
from typing import Any, AsyncIterator

from client import OpenWebUIClient
from prompt_loader import PromptLoader
from tools import ToolRegistry

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

    def __init__(self, client: OpenWebUIClient, registry: ToolRegistry, cfg: dict):
        self._client = client
        self._registry = registry
        self._agent_cfg = cfg.get("agent", {})
        self._max_iterations: int = self._agent_cfg.get("max_iterations", 30)

        # Load prompt files; fall back to config system_prompt for the base if missing.
        self._prompts = PromptLoader(cfg.get("prompts_dir", "prompts"))
        base = self._prompts.text("system_base") or self._agent_cfg.get(
            "system_prompt", "You are a helpful agent."
        )
        self._system_prompt: str = "\n\n".join(filter(None, [
            base,
            _build_os_instructions(self._prompts),
            self._prompts.text("system_desktop_rules"),
            self._prompts.text("system_browser_rules"),
            self._prompts.text("system_tool_analysis"),
        ]))

        # Seconds to wait before dispatching each tool call (0 = immediate).
        self._confirm_delay: int = int(cfg.get("safety", {}).get("command_confirm_delay_seconds", 0))
        # When True, screenshot base64 is delivered as an image_url vision message
        # so vision-capable models can actually see it.  Set False for text-only models.
        self._vision_screenshots: bool = self._agent_cfg.get("vision_screenshots", True)

        # The message history is the single source of truth for the conversation.
        # It persists across multiple calls to run(), allowing multi-turn dialogue.
        self._history: list[dict] = [
            {"role": "system", "content": self._system_prompt}
        ]

        # Running record of successful tool calls — passed to the coherence
        # checker so it can flag duplicates (e.g. launching the same app twice).
        self._completed_actions: list[str] = []

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    async def run(self, user_input: str) -> AsyncIterator[AgentEvent]:
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

        if "desktop_list_windows" in available_tools:
            try:
                windows_json = await self._registry.dispatch("desktop_list_windows", {})
                initial_suffix += f"\n\n[Desktop state at task start: {windows_json}]"
            except Exception:
                pass

        if self._vision_screenshots and "desktop_screenshot" in available_tools:
            try:
                shot_json = await self._registry.dispatch("desktop_screenshot", {})
                result_obj = json.loads(shot_json)
                b64 = result_obj.pop("base64_png", None)
                if b64:
                    self._history.append({
                        "role": "user",
                        "content": [
                            {"type": "text", "text": user_input + initial_suffix},
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
            try:
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
                    data={"tool_name": tool_name, "args": args, "call_id": call_id, "iteration": iteration},
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

                # ---- Error injection: fail loud, not quiet -----------------
                # Prepend a header inside the tool result AND track the failure
                # so we can inject a strong user-role retry directive afterward.
                if self._result_is_error(result_json):
                    failed_tools.append(tool_name)
                    history_content = (
                        "[TOOL FAILED: " + tool_name + "]\n"
                        + history_content
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
            called_names = {tc.get("function", {}).get("name", "") for tc in tool_calls}
            desktop_actions_taken = {
                n for n in called_names
                if n.startswith("desktop_")
                and n not in ("desktop_list_windows", "desktop_screenshot")
            }
            if desktop_actions_taken:
                if "desktop_list_windows" in available_tools:
                    try:
                        windows_json = await self._registry.dispatch("desktop_list_windows", {})
                        # After desktop_launch specifically, remind the model to click
                        # in the new window before attempting to type anything.
                        launch_reminder = ""
                        if "desktop_launch" in desktop_actions_taken:
                            launch_reminder = " " + self._prompts.text("runtime_launch_reminder")
                        self._history.append({
                            "role": "user",
                            "content": (
                                f"[Auto-verify after {', '.join(sorted(desktop_actions_taken))}] "
                                f"Current desktop state: {windows_json}"
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
                                            f"[Auto-verify screenshot after "
                                            f"{', '.join(sorted(desktop_actions_taken))}]\n"
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
        logger.info("[agent.py:reset] Conversation history cleared.")

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
