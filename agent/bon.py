"""
agent.bon — best-of-N action sampling (Phase 7).

When enabled and an uncertainty trigger fires, the next action is chosen by
sampling N completions and asking the verifier model to pick the best.
Split out of the original agent.py as a mixin of ``Agent``.
"""

import asyncio
import logging
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from client import OpenWebUIClient
    from tools import ToolRegistry

logger = logging.getLogger(__name__)


class BonMixin:
    """Best-of-N sampling methods of ``Agent``."""

    if TYPE_CHECKING:
        # Typing-only declarations of Agent state used by these methods.
        _bon_enabled: bool
        _bon_n: int
        _bon_temperature: float
        _bon_trigger_on_failure: bool
        _bon_trigger_on_validator: bool
        _client: "OpenWebUIClient"
        _history: list[dict]
        _last_iteration_had_failure: bool
        _last_validator_verdict: str | None
        _registry: "ToolRegistry"

        # Provided by HistoryMixin.
        def _brief_history_summary(self) -> str: ...

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
            if isinstance(resp, BaseException):
                continue
            try:
                msg = self._client.extract_message(resp)
                summary = self._summarize_candidate(msg)
                candidates.append((i, resp, summary))
            except Exception as e:
                logger.debug("[agent] BoN candidate %d unusable, skipping: %s", i, e)
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
