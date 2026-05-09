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


def _build_os_instructions() -> str:
    """Return OS-specific instructions to append to the system prompt."""
    system = _platform.system()
    release = _platform.release().lower()
    is_wsl = system == "Linux" and (
        "microsoft" in release or "wsl" in release or _proc_version_has_microsoft()
    )

    if is_wsl:
        return (
            "\n\nOS context: WSL (Windows Subsystem for Linux).\n"
            "Always append .exe to Windows programs (notepad.exe, msedge.exe, etc.).\n"
            "\n"
            "FINDING A WINDOWS EXECUTABLE — try these shell_run commands in order:\n"
            "  Step 1 (PATH check, fast):\n"
            "    where.exe msedge.exe\n"
            "  Step 2 (targeted search under Program Files, a few seconds):\n"
            '    find "/mnt/c/Program Files" "/mnt/c/Program Files (x86)" -name msedge.exe 2>/dev/null | head -5\n'
            "  Step 3 (PowerShell search, thorough):\n"
            "    powershell.exe -Command \"Get-ChildItem 'C:\\Program Files','C:\\Program Files (x86)'"
            " -Recurse -Filter msedge.exe -ErrorAction SilentlyContinue"
            " | Select-Object -First 5 -ExpandProperty FullName\"\n"
            "\n"
            "COMMON WINDOWS APP PATHS (check these with ls before launching):\n"
            "  Edge:    /mnt/c/Program Files (x86)/Microsoft/Edge/Application/msedge.exe\n"
            "  Chrome:  /mnt/c/Program Files/Google/Chrome/Application/chrome.exe\n"
            "  Firefox: /mnt/c/Program Files/Mozilla Firefox/firefox.exe\n"
            "  VS Code: /mnt/c/Users/<user>/AppData/Local/Programs/Microsoft VS Code/Code.exe\n"
            "\n"
            "LAUNCHING WITH FULL PATH from WSL: use desktop_launch with the path as the application,\n"
            "e.g. desktop_launch(application='/mnt/c/Program Files (x86)/Microsoft/Edge/Application/msedge.exe')\n"
            "or   desktop_launch(application='C:\\\\Program Files (x86)\\\\Microsoft\\\\Edge\\\\Application\\\\msedge.exe')\n"
            "\n"
            "CRITICAL: NEVER write shell commands or Windows CMD syntax in your text response.\n"
            "Writing 'find msedge -type f' or 'C:\\Users\\...>where msedge' as TEXT does nothing.\n"
            "You MUST call shell_run with the command string to actually execute it."
        )
    elif system == "Windows":
        return (
            "\n\nOS context: Windows native (NOT WSL — do NOT use Linux commands).\n"
            "\n"
            "FINDING A WINDOWS EXECUTABLE — try these shell_run commands in order:\n"
            "  Step 1 (PATH check, instant):\n"
            "    where msedge.exe\n"
            "  Step 2 (dir search, fast):\n"
            '    dir /s /b "C:\\Program Files\\msedge.exe" "C:\\Program Files (x86)\\msedge.exe" 2>nul\n'
            "  Step 3 (PowerShell, thorough):\n"
            "    powershell -Command \"Get-ChildItem 'C:\\Program Files','C:\\Program Files (x86)'"
            " -Recurse -Filter msedge.exe -EA SilentlyContinue"
            " | Select-Object -First 5 -ExpandProperty FullName\"\n"
            "\n"
            "COMMON WINDOWS APP PATHS (try desktop_launch with these directly):\n"
            "  Edge:    C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe\n"
            "  Chrome:  C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe\n"
            "  Firefox: C:\\Program Files\\Mozilla Firefox\\firefox.exe\n"
            "  Notepad: notepad.exe  (always on PATH)\n"
            "  Calc:    calc.exe     (always on PATH)\n"
            "\n"
            "LAUNCHING: use desktop_launch with the full Windows path.\n"
            "  e.g., desktop_launch(application='C:\\\\Program Files (x86)\\\\Microsoft\\\\Edge\\\\Application\\\\msedge.exe')\n"
            "\n"
            "IMPORTANT: shell_run uses cmd.exe. Use backslashes. "
            "For PowerShell: prefix with 'powershell -Command \"...\"'.\n"
            "Do NOT use Linux commands (find, ls, /mnt/c, grep) — they do not exist on Windows."
        )
    elif system == "Darwin":
        return (
            "\n\nOS context: macOS.\n"
            "FINDING AN EXECUTABLE: check /usr/local/bin, /opt/homebrew/bin, "
            "or use 'mdfind -name program' via shell_run.\n"
            "LAUNCHING: use desktop_launch with the executable path or app bundle:\n"
            "  e.g., desktop_launch(application='open -a Safari')\n"
            "  or    desktop_launch(application='/Applications/Firefox.app/Contents/MacOS/firefox')"
        )
    else:
        return (
            "\n\nOS context: Linux (native).\n"
            "FINDING AN EXECUTABLE: use 'which program' or "
            "'find /usr /opt -name program -type f 2>/dev/null | head -5' via shell_run.\n"
            "LAUNCHING: use desktop_launch with the executable path or name."
        )


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
        # Append OS-specific instructions so the model knows the runtime environment.
        self._system_prompt: str = (
            self._agent_cfg.get("system_prompt", "You are a helpful agent.")
            + _build_os_instructions()
        )
        # Desktop interaction rules injected once regardless of platform.
        self._system_prompt += (
            "\n\nDESKTOP INTERACTION RULES:\n"
            "1. FOCUS BEFORE TYPING: Before calling desktop_type or desktop_hotkey, "
            "always call desktop_click on the target window first to ensure it has "
            "keyboard focus. Never assume the correct window is active.\n"
            "2. VERIFY AFTER LAUNCH: After desktop_launch, call desktop_list_windows to "
            "confirm the application opened before trying to interact with it.\n"
            "3. CLICK USING WINDOW BOUNDS: desktop_list_windows returns x, y, width, height "
            "for every window in screen pixels. Always use these to compute click coordinates. "
            "Never guess pixel positions. "
            "To click in the client area: x = window.x + window.width // 2, "
            "y = window.y + 80 (to land below the title bar). "
            "Or use window.x + 40, window.y + 80 as a safe client-area offset.\n"
            "4. NEVER CONFUSE LAUNCH AND CLICK: desktop_launch starts a NEW process. "
            "If the app is already open, use desktop_click to focus it — do NOT launch again.\n"
            "5. VERIFY AFTER EVERY STEP: After each desktop action, call desktop_list_windows "
            "to confirm the expected result before moving to the next step."
        )

        # Browser interaction rules — keyboard shortcuts are more reliable than
        # clicking on browser chrome elements whose pixel coordinates vary.
        self._system_prompt += (
            "\n\nBROWSER INTERACTION RULES (Edge, Chrome, Firefox, Safari):\n"
            "CRITICAL: Never guess pixel coordinates for browser UI elements "
            "(address bar, tab bar, buttons). Use keyboard shortcuts instead — they "
            "always work regardless of window size or position.\n\n"
            "OPENING A NEW TAB AND NAVIGATING — mandatory sequence:\n"
            "  Step 1: desktop_click at window.x + window.width//2, window.y + 80 "
            "to focus the browser window\n"
            "  Step 2: desktop_hotkey(['ctrl', 't'])          ← opens a new tab\n"
            "  Step 3: desktop_hotkey(['ctrl', 'l'])          ← focuses the address bar\n"
            "  Step 4: desktop_type('https://example.com')    ← types the URL\n"
            "  Step 5: desktop_hotkey(['enter'])               ← navigates\n"
            "If the user said 'open a tab', you MUST do Ctrl+T before any URL navigation.\n"
            "If the user said 'navigate' or 'go to' on an existing tab, skip Ctrl+T.\n\n"
            "OTHER ESSENTIAL BROWSER HOTKEYS:\n"
            "  Ctrl+L or F6       — focus address bar (ALWAYS use this, never click the bar)\n"
            "  Ctrl+T             — new tab\n"
            "  Ctrl+W             — close current tab\n"
            "  Ctrl+Tab           — switch to next tab\n"
            "  Ctrl+Shift+Tab     — switch to previous tab\n"
            "  Ctrl+<1-9>         — jump to tab N\n"
            "  Ctrl+R or F5       — reload page\n"
            "  Ctrl+N             — new window\n\n"
            "BROWSER STATE CHECKS:\n"
            "  • After Ctrl+T, verify in desktop_list_windows that the tab count changed\n"
            "    (the window title often shows '… and N more pages').\n"
            "  • After typing and pressing Enter, take a screenshot to confirm navigation.\n"
            "  • If the browser is already open, NEVER launch it again — focus it with "
            "desktop_click, then use hotkeys."
        )

        # Mandatory tool output analysis rules.
        self._system_prompt += (
            "\n\nMANDATORY TOOL OUTPUT ANALYSIS — follow these for every tool call:\n"
            "shell_run:\n"
            "  • Read ALL of stdout AND stderr before deciding what to do next.\n"
            "  • stderr may contain errors even when exit_code=0 — always check it.\n"
            "  • If output mentions a log file path, use fs_read to open and read that file.\n"
            "  • If exit_code is non-zero OR stderr contains 'error'/'failed'/'exception', "
            "treat the step as failed and fix it before continuing.\n"
            "desktop_screenshot (vision enabled):\n"
            "  • You WILL receive the actual image. You MUST describe what you see:\n"
            "    which windows are open, what text or UI elements are visible, "
            "whether the expected app/state is present, and any error dialogs.\n"
            "  • If the screen does NOT show what you expected, treat it as a failure and "
            "diagnose before proceeding.\n"
            "  • Never skip the description. 'I took a screenshot' is not an analysis.\n"
            "fs_read:\n"
            "  • Read the FULL content returned.\n"
            "  • For log files, scan for: ERROR, WARNING, FAILED, Exception, Traceback.\n"
            "  • Report what you found before deciding the next step.\n"
            "General:\n"
            "  • Never assume a step succeeded without reading its output.\n"
            "  • If a result contains 'error', stop and fix it — do not continue to the next step."
        )

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
            initial_suffix = (
                "\n[Vision: ENABLED — screenshots are delivered to you as images. "
                "After every desktop action, a screenshot is auto-taken and shown to you. "
                "Examine each image carefully before proceeding.]"
            )
        else:
            initial_suffix = (
                "\n[Vision: DISABLED — you cannot see screenshots. "
                "Use desktop_list_windows (which includes x/y/width/height bounding boxes) "
                "to verify window state and compute click coordinates. "
                "Do NOT claim to see or describe screenshot contents.]"
            )

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
                        "content": (
                            "[AGENT POLICY — CRITICAL] Your last response described a "
                            "planned action in TEXT but issued no tool call for it.\n"
                            "IMPORTANT: All tool calls in previous iterations of this "
                            "session DID execute and succeed — those actions are done. "
                            "Only the action you wrote as text (not as a tool call) was "
                            "skipped. DO NOT re-run or re-launch anything already completed.\n"
                            f"Task still in progress: {user_input!r}\n"
                            "HOW TOOL CALLS WORK: Tool calls are a separate structured "
                            "response — writing an action in text does nothing and does not "
                            "undo previous tool calls. "
                            "Your NEXT response must be a tool_call for the NEXT "
                            "incomplete step. Do not describe what you will do — just call "
                            "the tool."
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
                        "content": (
                            f"[AGENT POLICY] The original task was: {user_input!r}\n"
                            "Your last response indicated confusion, inability to help, "
                            "or that you forgot the task — but you have NOT finished it.\n"
                            "DO NOT ask for clarification. DO NOT give up. Instead:\n"
                            "1. Re-read the original task above and identify the next "
                            "incomplete step.\n"
                            "2. Use desktop_list_windows to see what is currently open.\n"
                            "3. Use shell_run to search for missing programs or paths.\n"
                            "4. Continue executing until the entire task is complete.\n"
                            "Issue tool calls NOW to make progress on the task."
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
                                            "text": (
                                                "Here is the screenshot you just captured. "
                                                "Examine it carefully. "
                                                "Describe what you see, confirm whether the "
                                                "expected application/state is visible, and "
                                                "decide your next action based on what is "
                                                "actually shown — not on assumptions."
                                            ),
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
                                "[fs_read result — READ AND ANALYZE THE CONTENT BELOW "
                                "before deciding your next action. "
                                "For log files: look for ERROR, WARNING, FAILED, Exception, "
                                "Traceback. Report what you find.]\n"
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
                                "[NOTE: shell_run exited 0 but produced stderr output — "
                                "read it carefully for warnings or errors]\n"
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
                    "content": (
                        f"[RETRY REQUIRED] {len(failed_tools)} tool call(s) just failed: "
                        f"{', '.join(failed_tools)}.\n"
                        "Read the [TOOL FAILED] error(s) above carefully.\n"
                        "DO NOT proceed to any subsequent step.\n"
                        "Diagnose the failure, then retry ONLY that failed tool "
                        "with corrected arguments. Do not repeat any steps that already "
                        "succeeded.\n"
                        "Common fixes:\n"
                        "  • Wrong path → use shell_run to locate the file first\n"
                        "  • Wrong argument name → check tool schema\n"
                        "  • App not found → search with 'where.exe <name>' (Windows/WSL) "
                        "or 'which <name>' (Linux)\n"
                        "Issue the corrected tool call NOW."
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
                            launch_reminder = (
                                " REQUIRED NEXT STEP: call desktop_click inside the new "
                                "window to give it keyboard focus BEFORE desktop_type. "
                                "Use x=window.x+window.width//2, y=window.y+80 from the "
                                "bounding box above to land in the client area."
                            )
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
                                            "MANDATORY: Describe what you see in this image RIGHT NOW "
                                            "before your next action. Answer these questions:\n"
                                            "1. Which windows/applications are visible?\n"
                                            "2. Is the expected application open and in the expected state?\n"
                                            "3. Is there any error dialog, unexpected window, or wrong content?\n"
                                            "4. Based on what you see, what is the correct next step?\n"
                                            "Do NOT skip straight to a tool call — describe first, then act."
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
                "content": (
                    f"You are a command validator for a desktop automation agent on {os_label}.\n"
                    "Your job: check that proposed shell commands and app launches are "
                    "syntactically correct, use the right executable, and are not redundant.\n\n"
                    "ALWAYS APPROVE (these are preparation steps, never duplicates):\n"
                    "  • Search/locate commands: find, where, where.exe, which, ls, dir, "
                    "Get-ChildItem, mdfind, locate\n"
                    "  • PATH lookup: 'where.exe notepad.exe', 'which python', etc.\n"
                    "  • Any command whose purpose is to find a file or executable\n\n"
                    "CHECK these issues and CORRECT or REJECT accordingly:\n"
                    "  1. SYNTAX — Is the command well-formed? "
                    "(e.g. 'cmd - python main.py' is broken syntax; should be 'python main.py')\n"
                    "  2. CORRECT EXECUTABLE — Right program for the task? "
                    "If the user asked for 'notepad', the binary must be notepad.exe, "
                    "not notepad++.exe or another editor. "
                    "If the user asked for Edge, it must be msedge.exe, not chrome.exe.\n"
                    "  3. PATH VALIDITY — If a full path is given, does it look plausible "
                    "for this OS? WSL paths start with /mnt/c/..., Windows with C:\\...\n"
                    "  4. DUPLICATION — If an identical or equivalent action already succeeded "
                    "recently, flag it so the agent doesn't repeat it pointlessly.\n\n"
                    "REPLY FORMAT — output EXACTLY one line, no explanation:\n"
                    "  APPROVED\n"
                    "  CORRECTED: <json>   "
                    "  where <json> = corrected args, e.g. "
                    '{\"command\": \"notepad.exe\"} or '
                    '{\"application\": \"notepad.exe\", \"args\": []}\n'
                    "  REJECTED: <one-line reason>   "
                    "  only for commands that are clearly wrong AND not a search command\n\n"
                    "Default to APPROVED when uncertain. Never reject a search command."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Task: {user_input}\n"
                    f"Proposed {tool_name}: {cmd_display}\n\n"
                    f"{recent_block}\n\n"
                    "Validate the proposed command."
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
