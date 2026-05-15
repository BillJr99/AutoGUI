"""
screen_observer_client.py — Optional HTTP client for OS Screen Observer.

Used as a perception overlay in backends/base.py when screen_observer.enabled=true
in config.json.  All methods return None on any network/server failure so callers
can fall through to their native perception path.  A 30-second cooldown prevents
hammering a down server.
"""

import logging
import time

logger = logging.getLogger(__name__)

_COOLDOWN = 30.0


def _oso_walk_for_element(
    node: dict,
    name_query: str,
    control_type: str | None,
) -> list[dict]:
    """Recursively flatten OSO element tree into matching elements."""
    results = []
    name_lower = name_query.lower()
    node_name = (node.get("name") or "").lower()
    node_role = node.get("role", "")

    name_match = name_lower in node_name
    type_match = control_type is None or control_type.lower() in node_role.lower()

    if name_match and type_match and node.get("bounds"):
        b = node["bounds"]
        results.append({
            "name": node.get("name", ""),
            "control_type": node_role,
            "rect": {
                "x": b.get("x", 0),
                "y": b.get("y", 0),
                "width": b.get("width", 0),
                "height": b.get("height", 0),
            },
            "method": "screen_observer",
        })

    for child in node.get("children", []):
        results.extend(_oso_walk_for_element(child, name_query, control_type))

    return results


class ScreenObserverClient:
    """
    Thin async HTTP client for the OS Screen Observer REST API
    (http://127.0.0.1:5001 by default).

    All public methods are async and return None when the server is
    unreachable or returns an error, so callers can always fall back
    to their native path without extra error handling.
    """

    def __init__(self, cfg: dict):
        self._base = cfg.get("base_url", "http://127.0.0.1:5001").rstrip("/")
        self._timeout = float(cfg.get("timeout_seconds", 2.0))
        self._enabled = bool(cfg.get("enabled", False))
        self._disabled_until: float = 0.0

    @property
    def enabled(self) -> bool:
        return self._enabled

    def _cooled(self) -> bool:
        return time.monotonic() >= self._disabled_until

    def _back_off(self) -> None:
        self._disabled_until = time.monotonic() + _COOLDOWN

    async def _get(self, path: str, params: dict | None = None) -> dict | None:
        if not self._enabled or not self._cooled():
            return None
        try:
            import aiohttp
            async with aiohttp.ClientSession() as s:
                async with s.get(
                    f"{self._base}{path}",
                    params=params or {},
                    timeout=aiohttp.ClientTimeout(total=self._timeout),
                ) as r:
                    if r.status == 200:
                        return await r.json()
                    logger.debug("[OSO] GET %s -> HTTP %d", path, r.status)
                    return None
        except Exception as e:
            logger.debug("[OSO] GET %s failed: %s", path, e)
            self._back_off()
            return None

    async def is_available(self) -> bool:
        """Probe /api/healthz; returns True if the server is reachable."""
        result = await self._get("/api/healthz")
        return result is not None

    async def get_windows(self) -> dict | None:
        return await self._get("/api/windows")

    async def get_description(self, window_index: int | None = None) -> dict | None:
        p: dict = {}
        if window_index is not None:
            p["window_index"] = window_index
        return await self._get("/api/description", p)

    async def get_sketch(self, window_index: int | None = None) -> dict | None:
        p: dict = {}
        if window_index is not None:
            p["window_index"] = window_index
        return await self._get("/api/sketch", p)

    async def get_structure(self, window_index: int | None = None) -> dict | None:
        p: dict = {}
        if window_index is not None:
            p["window_index"] = window_index
        return await self._get("/api/structure", p)

    async def find_element_in_tree(
        self,
        name: str,
        control_type: str | None = None,
        window_index: int | None = None,
        index: int = 0,
    ) -> dict | None:
        """Get the structure tree and walk it for a matching element."""
        tree_result = await self.get_structure(window_index=window_index)
        if tree_result is None:
            return None
        matches = _oso_walk_for_element(
            tree_result.get("tree") or {},
            name_query=name,
            control_type=control_type,
        )
        if not matches:
            return None
        idx = max(0, min(int(index or 0), len(matches) - 1))
        return matches[idx]
