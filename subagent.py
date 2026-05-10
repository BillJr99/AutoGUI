"""
subagent.py — Lightweight subagent for read-heavy operations.

Some sub-tasks are pure information lookups: "read these five files and
tell me which one mentions X", "scan this command's stdout for the
license key", "summarise this 4 KB JSON blob".  Running them through
the full agent loop bloats the main history with observations the user
will never care about.

The ``Subagent`` runs a one-shot LLM call with a tightly scoped tool
allow-list (defaults to fs_read + fs_list, no desktop or shell), feeds
it any pre-fetched artifacts the parent already has, and returns a
short structured answer.

It is deliberately a thin wrapper over the existing ``OpenWebUIClient``
so it inherits validation, model selection, and timeout handling.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


_DEFAULT_READ_TOOLS = ("fs_read", "fs_list", "browser_get_text")


@dataclass
class SubagentResult:
    answer: str
    artifact_ids: list[str]
    tool_calls_made: int
    raw: str = ""


class Subagent:
    """
    One-shot read-only worker.

    Construction
    ------------
    ``client`` and ``registry`` are the same instances the main agent
    uses; the subagent just calls ``client.chat`` with a filtered tool
    schema list, then dispatches any tool calls through ``registry`` —
    no separate process, no separate API key.

    Use
    ---
    ``await subagent.ask(question, fetched_artifacts)`` returns a
    ``SubagentResult`` whose ``answer`` field is the LLM's terse reply.
    """

    def __init__(
        self,
        client,
        registry,
        *,
        allowed_tools: tuple[str, ...] = _DEFAULT_READ_TOOLS,
        max_tool_calls: int = 4,
        artifact_store=None,
    ):
        self._client = client
        self._registry = registry
        self._allowed = set(allowed_tools)
        self._max_tool_calls = max(1, int(max_tool_calls))
        self._artifacts = artifact_store

    # ------------------------------------------------------------------
    # Tool schema filter
    # ------------------------------------------------------------------

    def _filtered_schemas(self) -> list[dict]:
        return [
            s for s in self._registry.schemas
            if s.get("function", {}).get("name") in self._allowed
        ]

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    async def ask(
        self,
        question: str,
        *,
        fetched_artifacts: list[tuple[str, str]] | None = None,
        max_iterations: int = 3,
    ) -> SubagentResult:
        """
        Ask the subagent a question.

        ``fetched_artifacts`` is a list of ``(label, body)`` tuples that
        the caller has already retrieved — the subagent gets them
        verbatim in the system prompt so it doesn't need to re-fetch.
        """
        system = (
            "You are a focused read-only assistant.  Answer the user's question "
            "using the provided artifacts and the limited file/browser tools.  "
            "Reply in 1-3 sentences.  When citing a file, include its path.  "
            "Do not modify state, run shell commands, or interact with the GUI."
        )
        if fetched_artifacts:
            artifact_block = "\n\n".join(
                f"--- {label} ---\n{body[:4000]}" for label, body in fetched_artifacts
            )
            system += "\n\nPRE-FETCHED ARTIFACTS\n" + artifact_block

        history = [
            {"role": "system", "content": system},
            {"role": "user", "content": question},
        ]
        tools_schema = self._filtered_schemas() or None

        tool_calls_made = 0
        final_text = ""
        captured_ids: list[str] = []

        for _ in range(max(1, int(max_iterations))):
            try:
                response = await self._client.chat(messages=history, tools=tools_schema)
            except Exception as e:
                logger.warning("[subagent] chat failed: %s", e)
                return SubagentResult(
                    answer=f"(subagent error: {e})",
                    artifact_ids=captured_ids,
                    tool_calls_made=tool_calls_made,
                )

            try:
                message = self._client.extract_message(response)
            except Exception as e:
                logger.warning("[subagent] extract_message failed: %s", e)
                return SubagentResult(
                    answer="(subagent: malformed response)",
                    artifact_ids=captured_ids,
                    tool_calls_made=tool_calls_made,
                )

            history.append(message)
            text = self._client.extract_text(message) or ""
            tool_calls = self._client.extract_tool_calls(message) or []
            if not tool_calls:
                final_text = text.strip()
                break

            for tc in tool_calls:
                tool_name = tc.get("function", {}).get("name", "")
                # Most tool-calling APIs require every tool_call to be
                # answered with a role="tool" message; breaking out of
                # the loop without responding leaves dangling
                # tool_call_ids and the next chat() can stall or 400.
                # When the budget is exhausted we still emit a
                # synthetic error result so the conversation stays
                # well-formed.
                if tool_calls_made >= self._max_tool_calls:
                    history.append({
                        "role": "tool",
                        "tool_call_id": tc.get("id", ""),
                        "content": json.dumps({
                            "error": (
                                f"subagent tool budget exhausted "
                                f"({self._max_tool_calls}); call {tool_name!r} "
                                "skipped without execution."
                            ),
                        }),
                    })
                    continue
                if tool_name not in self._allowed:
                    history.append({
                        "role": "tool",
                        "tool_call_id": tc.get("id", ""),
                        "content": json.dumps({"error": f"tool {tool_name!r} not allowed in subagent"}),
                    })
                    continue
                try:
                    args = json.loads(tc.get("function", {}).get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                result_json = await self._registry.dispatch(tool_name, args)
                tool_calls_made += 1

                if self._artifacts is not None:
                    try:
                        result_obj = json.loads(result_json)
                        body = result_obj.get("content")
                        if isinstance(body, str) and len(body) > 256:
                            aid = self._artifacts.put(
                                body,
                                kind=tool_name,
                                source=str(args.get("path", "")) or tool_name,
                            )
                            captured_ids.append(aid)
                            # Replace inline body with artifact reference for the subagent's context.
                            result_obj["content"] = f"<stored as {aid}; use parent's get_artifact to fetch>"
                            result_json = json.dumps(result_obj)
                    except Exception:
                        pass

                history.append({
                    "role": "tool",
                    "tool_call_id": tc.get("id", ""),
                    "content": result_json,
                })

        return SubagentResult(
            answer=final_text or "(subagent produced no answer)",
            artifact_ids=captured_ids,
            tool_calls_made=tool_calls_made,
            raw=final_text,
        )
