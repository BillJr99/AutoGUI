"""
agent.oso_bundle — OSO text observation bundle assembly.

Builds the textual perception bundle (description + sketch + depth-trimmed
a11y tree) from the OSScreenObserver backend.  Split out of the original
agent.py as a mixin of ``Agent``.
"""

import json
import logging
from typing import TYPE_CHECKING

from app_memory import _normalize_app

if TYPE_CHECKING:
    from tools import ToolRegistry

logger = logging.getLogger(__name__)


class OsoBundleMixin:
    """OSO text-bundle methods of ``Agent``."""

    if TYPE_CHECKING:
        # Typing-only declarations of Agent state used by these methods.
        _oso_text_cfg: dict
        _registry: "ToolRegistry"

    async def _build_oso_text_bundle(self) -> dict | None:
        """Build a length-bounded text observation bundle via the backend.

        Resolves the active-window index when scope is 'active_window' (or
        'auto') so OSO can return a focused view.  Returns None when the
        backend or OSO is unavailable.
        """
        backend = getattr(self._registry, "_backend", None)
        if backend is None or getattr(backend, "_screen_observer", None) is None:
            return None
        cfg = self._oso_text_cfg
        scope = cfg.get("scope", "active_window")
        window_index: int | None = None
        if scope in ("active_window", "auto"):
            window_index = await self._best_effort_active_window_index()
        bundle = await backend.describe_screen_text(
            window_index=window_index,
            include_sketch=cfg["include_sketch"],
            include_tree=cfg["include_tree"],
            tree_start_depth=cfg["tree_start_depth"],
            tree_min_depth=cfg["tree_min_depth"],
            tree_max_chars=cfg["tree_max_chars"],
            max_chars=cfg["max_chars"],
        )
        if (
            scope == "auto"
            and bundle is not None
            and not (bundle.get("description") or bundle.get("sketch") or bundle.get("tree_text"))
        ):
            # Active-window view returned nothing — retry whole-screen.
            bundle = await backend.describe_screen_text(
                window_index=None,
                include_sketch=cfg["include_sketch"],
                include_tree=cfg["include_tree"],
                tree_start_depth=cfg["tree_start_depth"],
                tree_min_depth=cfg["tree_min_depth"],
                tree_max_chars=cfg["tree_max_chars"],
                max_chars=cfg["max_chars"],
            )
        return bundle

    async def _best_effort_active_window_index(self) -> int | None:
        """Best-effort index of the focused window from OSO /api/windows."""
        backend = getattr(self._registry, "_backend", None)
        oso = getattr(backend, "_screen_observer", None) if backend else None
        if oso is None:
            return None
        try:
            wins = await oso.get_windows()
        except Exception as e:
            logger.debug("[agent] OSO get_windows for active-window index failed: %s", e)
            return None
        if not wins:
            return None
        items = wins.get("windows") if isinstance(wins, dict) else None
        if not items:
            return None
        for i, w in enumerate(items):
            if w.get("focused") or w.get("active") or w.get("is_focused"):
                idx = w.get("window_index")
                return int(idx) if idx is not None else i
        return None

    async def _best_effort_active_app(self) -> str:
        """Return a normalised app slug for the active window, or '' on miss."""
        if "desktop_get_active_window" not in self._registry.list_tools():
            return ""
        try:
            raw = await self._registry.dispatch("desktop_get_active_window", {})
            info = json.loads(raw)
            win = info.get("window") or info or {}
            return _normalize_app(str(win.get("app") or win.get("title") or ""))
        except Exception as e:
            logger.debug("[agent] best-effort active-app lookup failed: %s", e)
            return ""
