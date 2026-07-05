"""
agent.guards — text-response guards against narration, give-ups and leaks.

Pattern lists + checks that detect the model narrating actions instead of
calling tools, abandoning the task, leaking raw tool-call syntax, or issuing
incoherent commands.  Split out of the original agent.py as a mixin of
``Agent``.
"""

import json
import logging
import platform as _platform
import re
from typing import TYPE_CHECKING

from .prompts import _proc_version_has_microsoft

if TYPE_CHECKING:
    from client import OpenWebUIClient
    from prompt_loader import PromptLoader

logger = logging.getLogger(__name__)


class GuardsMixin:
    """Response-guard methods (and their pattern tables) of ``Agent``."""

    if TYPE_CHECKING:
        # Typing-only declarations of Agent state used by these methods.
        _client: "OpenWebUIClient"
        _completed_actions: list[str]
        _prompts: "PromptLoader"

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

    # Compiled once at class scope so the per-iteration check is cheap.
    # Patterns intentionally cover the multiple Gemma 3/4 / Llama / Qwen
    # tool-call leak shapes we've seen in user reports rather than
    # matching one provider's format exactly.
    _LEAKED_TOOL_CALL_PATTERNS = (
        re.compile(r"<\|?tool_call\|?>", re.IGNORECASE),
        re.compile(r"<tool_call\|>", re.IGNORECASE),
        re.compile(r"<\|tool_call\|>", re.IGNORECASE),
        re.compile(r"^\s*action\s*:\s*\w", re.IGNORECASE | re.MULTILINE),
        re.compile(r"<function_call>|</function_call>", re.IGNORECASE),
    )

    @classmethod
    def _looks_like_leaked_tool_call(cls, text: str) -> bool:
        """True when the assistant text contains tool-call syntax that
        should have been emitted as a real tool_call.

        Used by _run_step to inject a one-shot format reminder instead
        of marking the step BLOCKED — the model intended to invoke a
        tool, the API just didn't parse it.  See the call site for the
        reminder copy.
        """
        if not text:
            return False
        return any(p.search(text) for p in cls._LEAKED_TOOL_CALL_PATTERNS)


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
