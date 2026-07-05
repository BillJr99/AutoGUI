"""
agent.recovery — watchdog snapshots, drift anchors and recovery probes.

Post-action state capture (window titles / perceptual hashes), the stall
watchdog snapshot, the failure recovery probe and the default step verifier.
Split out of the original agent.py as a mixin of ``Agent``.
"""

import json
import logging
import time
from typing import TYPE_CHECKING

import visual_diff
from failures import classify

if TYPE_CHECKING:
    from tools import ToolRegistry

logger = logging.getLogger(__name__)


class RecoveryMixin:
    """Watchdog / drift / recovery-probe methods of ``Agent``."""

    if TYPE_CHECKING:
        # Typing-only declarations of Agent state used by these methods.
        _drift_anchor_enabled: bool
        _drift_anchor_phash: bool
        _last_screenshot_hash: bytes | None
        _oso_text_enabled: bool
        _recovery_probe_count: dict[str, int]
        _recovery_probe_enabled: bool
        _recovery_probe_max_per_step: int
        _registry: "ToolRegistry"
        _vision_screenshots: bool
        _visual_diff_enabled: bool

        # Provided by OsoBundleMixin.
        async def _build_oso_text_bundle(self) -> dict | None: ...

    async def _snapshot_watchdog_state(self) -> dict:
        """Cheap state snapshot for the watchdog signature.  Best-effort —
        any failure produces empty fields so the watchdog still works."""
        windows: list = []
        active: dict = {}
        tools = set(self._registry.list_tools())
        if "desktop_list_windows" in tools:
            try:
                raw = await self._registry.dispatch("desktop_list_windows", {})
                windows = json.loads(raw).get("windows") or []
            except Exception as e:
                failure_verdict = classify(tool_name="desktop_list_windows", error_message=str(e))
                logger.debug(
                    "[agent] watchdog window snapshot failed (%s): %s",
                    failure_verdict.cls.value, e,
                )
                windows = []
        if "desktop_get_active_window" in tools:
            try:
                raw = await self._registry.dispatch("desktop_get_active_window", {})
                active = json.loads(raw) or {}
            except Exception as e:
                failure_verdict = classify(tool_name="desktop_get_active_window", error_message=str(e))
                logger.debug(
                    "[agent] watchdog active-window snapshot failed (%s): %s",
                    failure_verdict.cls.value, e,
                )
                active = {}
        return {"windows": windows, "active": active}

    async def _capture_drift_anchor(self) -> dict:
        """Cheap post-action world snapshot for replay --drift-check.

        Captures the visible window titles (always when enabled — these
        are essentially free) and, when ``drift_anchor.capture_phash`` is
        on, a perceptual hash of the screen for visual drift detection.
        Best-effort: any failure yields a partial / empty dict so a
        recording session never breaks because the anchor couldn't be
        captured.  The shape mirrors what replay._check_drift expects.
        """
        if not self._drift_anchor_enabled:
            return {}
        anchor: dict = {}
        tools = set(self._registry.list_tools())
        if "desktop_list_windows" in tools:
            try:
                raw = await self._registry.dispatch("desktop_list_windows", {})
                wins = json.loads(raw).get("windows") or []
                titles = [str(w.get("title", "")) for w in wins if w.get("title")]
                if titles:
                    anchor["window_titles"] = titles
            except Exception as e:
                logger.debug("[agent] drift-anchor window titles capture failed: %s", e)
        if self._drift_anchor_phash and "desktop_screenshot" in tools:
            try:
                import base64 as _b64

                from visual_diff import hash_b64 as _vhash
                raw = await self._registry.dispatch("desktop_screenshot", {})
                shot = json.loads(raw)
                h = _vhash(shot.get("base64_png", ""))
                if h:
                    anchor["screen_phash_b64"] = _b64.b64encode(h).decode("ascii")
            except Exception as e:
                logger.debug("[agent] drift-anchor screenshot phash failed: %s", e)
        return anchor

    async def _emit_recovery_probe(self, step_id: str, reason: str) -> dict | None:
        """Assemble a perception bundle on predicate / step failure.

        Captures a screenshot (marked variant when available), the current
        active window, the visible window list, and (when an OSO client is
        attached to the backend) an observe() diff token.  Returns a dict
        suitable for embedding in an AgentEvent.data payload — or None when
        the per-step ceiling has been hit or the feature is disabled.

        Mirrors the pi-extension's `emitRecoveryProbe` in tools.ts so both
        runtimes produce the same shape.  Never throws — all calls are
        best-effort and missing pieces just produce absent fields.
        """
        if not self._recovery_probe_enabled:
            return None
        cap = self._recovery_probe_max_per_step
        seen = self._recovery_probe_count.get(step_id, 0)
        if cap > 0 and seen >= cap:
            return None
        self._recovery_probe_count[step_id] = seen + 1
        probe: dict = {"step": step_id, "reason": reason, "ts": time.time()}
        tools = self._registry.list_tools()
        # Prefer the marked screenshot when SoM is available; the model can
        # then call desktop_click_mark directly without another perception
        # round-trip.
        shot_tool = (
            "desktop_screenshot_marked"
            if "desktop_screenshot_marked" in tools
            else "desktop_screenshot" if "desktop_screenshot" in tools
            else None
        )
        if shot_tool:
            try:
                raw = await self._registry.dispatch(shot_tool, {})
                shot = json.loads(raw) if raw else {}
                # Strip the inline image bytes — keep the path + marks so
                # the event payload doesn't bloat traces / TUI buffers.
                shot.pop("data", None)
                probe["screenshot"] = shot
            except Exception as e:
                probe["screenshot_error"] = str(e)
        if "desktop_get_active_window" in tools:
            try:
                raw = await self._registry.dispatch("desktop_get_active_window", {})
                probe["active_window"] = json.loads(raw)
            except Exception as e:
                logger.debug("[agent] recovery probe active-window capture failed: %s", e)
        if "desktop_list_windows" in tools:
            try:
                raw = await self._registry.dispatch("desktop_list_windows", {})
                wins = json.loads(raw)
                if isinstance(wins, dict) and isinstance(wins.get("windows"), list):
                    probe["window_list"] = wins["windows"][:25]
            except Exception as e:
                logger.debug("[agent] recovery probe window-list capture failed: %s", e)
        # Direct OSO observe — only when the backend has an attached client.
        backend = getattr(self._registry, "_backend", None)
        oso = getattr(backend, "_screen_observer", None) if backend else None
        if oso is not None and getattr(oso, "enabled", False):
            try:
                obs = await oso.observe()
                if obs:
                    probe["oso_observe"] = {
                        "tree_hash": obs.get("tree_hash"),
                        "diff_token": obs.get("diff_token"),
                        "description": obs.get("description"),
                    }
            except Exception as e:
                logger.debug("[agent] recovery probe oso_observe failed: %s", e)
            # Full text bundle (description + sketch + depth-trimmed tree)
            # when text_observation is enabled.  Gives the recovery flow the
            # same perception payload as the success-path injection.
            if self._oso_text_enabled:
                try:
                    bundle = await self._build_oso_text_bundle()
                    if bundle is not None:
                        probe["oso_text"] = bundle
                except Exception as e:
                    logger.debug("[agent] recovery probe oso_text bundle failed: %s", e)
        return probe


    async def _apply_default_verifier(
        self, tool_name: str, args: dict, result_json: str,
    ) -> str:
        """
        For tool calls that have an obvious, cheap, post-condition check,
        run it inline and tag the result with a ``verifier`` field so the
        model reads structured evidence rather than trusting the tool's
        self-reported success.

        Currently covers:
          * fs_write       — read back, confirm content matches
          * browser_navigate — confirm window.location.href matches the URL
          * desktop_launch — verify a window appeared in the listing
          * any state-changing desktop tool, when vision is on — check
            that the screen perceptual hash actually moved
        """
        try:
            result = json.loads(result_json)
        except json.JSONDecodeError:
            return result_json
        if "error" in result:
            return result_json

        verifier: dict = {}

        if tool_name == "fs_write":
            path = str(args.get("path") or result.get("path") or "")
            wanted = args.get("content")
            if path and isinstance(wanted, str):
                try:
                    raw = await self._registry.dispatch(
                        "fs_read", {"path": path, "max_bytes": min(8192, len(wanted) + 16)},
                    )
                    parsed = json.loads(raw)
                    body = parsed.get("content") or ""
                    if wanted.strip() and wanted[:200] in body:
                        verifier = {"ok": True, "kind": "fs_write_readback"}
                    else:
                        verifier = {
                            "ok": False, "kind": "fs_write_readback",
                            "detail": "read-back body does not contain written content",
                        }
                except Exception as e:
                    verifier = {"ok": False, "kind": "fs_write_readback",
                                "detail": f"read-back failed: {e}"}

        elif tool_name == "browser_navigate":
            wanted_url = str(args.get("url") or "")
            if wanted_url and "browser_eval" in self._registry.list_tools():
                try:
                    raw = await self._registry.dispatch(
                        "browser_eval", {"expression": "window.location.href"},
                    )
                    parsed = json.loads(raw)
                    href = str(parsed.get("value") or "")
                    if wanted_url in href or href in wanted_url:
                        verifier = {"ok": True, "kind": "browser_url",
                                    "current_url": href}
                    else:
                        verifier = {"ok": False, "kind": "browser_url",
                                    "current_url": href,
                                    "detail": "current URL does not match request"}
                except Exception as e:
                    verifier = {"ok": False, "kind": "browser_url",
                                "detail": f"browser_eval failed: {e}"}

        elif tool_name == "desktop_launch":
            target = str(args.get("application") or "").rsplit(".", 1)[0].lower()
            if target and "desktop_list_windows" in self._registry.list_tools():
                try:
                    raw = await self._registry.dispatch("desktop_list_windows", {})
                    wins = json.loads(raw).get("windows") or []
                    matched = any(
                        target in (w.get("title", "") or "").lower()
                        or target in (w.get("app", "") or "").lower()
                        for w in wins
                    )
                    verifier = {
                        "ok": matched, "kind": "desktop_launch_window",
                        "detail": ("window with matching title/app found"
                                   if matched else
                                   f"no visible window matches {target!r}"),
                    }
                except Exception as e:
                    verifier = {"ok": False, "kind": "desktop_launch_window",
                                "detail": f"window listing failed: {e}"}

        # Visual diff: applies to any state-changing desktop tool when
        # vision is on AND the previous step's screenshot hash is on hand.
        if (
            self._visual_diff_enabled
            and self._vision_screenshots
            and tool_name in (
                "desktop_click", "desktop_click_mark", "desktop_click_text",
                "desktop_click_element", "desktop_type", "desktop_hotkey",
                "desktop_scroll",
            )
            and "desktop_screenshot" in self._registry.list_tools()
        ):
            try:
                raw = await self._registry.dispatch("desktop_screenshot", {})
                shot = json.loads(raw)
                b64 = shot.get("base64_png")
                curr_hash = visual_diff.hash_b64(b64) if b64 else None
                if curr_hash is not None:
                    diff = visual_diff.diff(self._last_screenshot_hash, curr_hash)
                    self._last_screenshot_hash = curr_hash
                    if self._last_screenshot_hash is not None and diff.likely_no_change:
                        verifier.setdefault("ok", False)
                        verifier["visual_diff"] = {
                            "hamming": diff.hamming,
                            "fraction_changed": diff.fraction_changed,
                            "note": "screen pixels barely changed; action may have been a no-op",
                        }
                        verifier.setdefault("kind", "visual_diff")
                        verifier.setdefault("detail",
                                            "perceptual hash stayed nearly identical")
            except Exception as e:
                logger.debug("[agent] visual-diff default verifier failed: %s", e)

        if verifier:
            result["verifier"] = verifier
            return json.dumps(result, default=str)
        return result_json
