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

# Capability keys from /api/capabilities 'supports' dict that AutoGUI gates on.
# Defaults apply when OSO is reachable but the key is absent (older OSO version).
# Keys match what observer.get_capabilities() actually returns.
_CAP_DEFAULTS: dict = {
    "accessibility_tree": True,   # AT-SPI / UIAutomation / AX tree available
    "ocr":                False,  # requires pytesseract
    "vlm":                False,  # requires config vlm.enabled + vlm.model
    "uia_invoke":         False,  # Windows UIAutomation invoke (Windows-only)
    "occlusion_detection": True,  # Z-order occlusion check available
    "drag":               True,   # drag gesture support
    "screenshot":         True,   # /api/screenshot available
    "monitors":           True,   # /api/monitors available
    "bring_to_foreground": True,  # /api/bring_to_foreground available
    "element_targeting":  True,   # element click/focus/invoke/set_value via element_id
    "observe_with_diff":  True,   # /api/observe returns diff token
}


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
        # Cached result of /api/capabilities; populated on first is_available() call.
        self._caps: dict = dict(_CAP_DEFAULTS)
        self._caps_fetched: bool = False

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def oso_capabilities(self) -> dict:
        """Return the last-fetched /api/capabilities 'supports' dict.

        Available after the first successful is_available() or get_capabilities()
        call.  Falls back to conservative defaults until then.
        """
        return dict(self._caps)

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
                    logger.warning("[OSO] GET %s -> HTTP %d; falling back to native method", path, r.status)
                    return None
        except Exception as e:
            logger.warning("[OSO] GET %s failed: %s; falling back to native method", path, e)
            self._back_off()
            return None

    async def _post(self, path: str, body: dict | None = None) -> dict | None:
        if not self._enabled or not self._cooled():
            return None
        try:
            import aiohttp
            async with aiohttp.ClientSession() as s:
                async with s.post(
                    f"{self._base}{path}",
                    json=body or {},
                    timeout=aiohttp.ClientTimeout(total=self._timeout),
                ) as r:
                    if r.status == 200:
                        return await r.json()
                    logger.warning("[OSO] POST %s -> HTTP %d; falling back to native method", path, r.status)
                    return None
        except Exception as e:
            logger.warning("[OSO] POST %s failed: %s; falling back to native method", path, e)
            self._back_off()
            return None

    async def is_available(self) -> bool:
        """Probe /api/healthz; returns True if the server is reachable.

        Also fetches /api/capabilities on the first successful probe so
        subsequent calls to oso_capabilities reflect what this OSO instance
        actually supports.
        """
        result = await self._get("/api/healthz")
        if result is not None and not self._caps_fetched:
            await self.get_capabilities()
        return result is not None

    async def get_capabilities(self) -> dict | None:
        """Fetch /api/capabilities and cache the 'supports' dict.

        Returns the full capabilities response, or None if unreachable.
        The 'supports' sub-dict is always accessible via oso_capabilities
        even after a failure (returns cached or default values).
        """
        result = await self._get("/api/capabilities")
        if result is not None:
            supports = result.get("supports") or {}
            merged = dict(_CAP_DEFAULTS)
            merged.update(supports)
            self._caps = merged
            self._caps_fetched = True
            logger.info(
                "[OSO] capabilities: version=%s supports=%s",
                result.get("version", "?"),
                self._caps,
            )
        return result

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

    async def get_screenshot(self, window_index: int | None = None) -> dict | None:
        """Fetch a screenshot from OSO (/api/screenshot).

        Returns {data: base64_png, format: 'png', encoding: 'base64', window: title}
        or None on failure.  Useful for per-window captures that OSO performs
        natively via its accessibility adapter.
        """
        p: dict = {}
        if window_index is not None:
            p["window_index"] = window_index
        return await self._get("/api/screenshot", p)

    async def get_monitors(self) -> dict | None:
        """Fetch monitor geometry from OSO (/api/monitors).

        Returns {ok, monitors: [{bounds, scale_factor, ...}]} or None on failure.
        Useful for multi-monitor coordinate translation and DPI-aware positioning.
        """
        return await self._get("/api/monitors")

    async def get_visible_areas(self, window_index: int) -> dict | None:
        """Fetch the unoccluded regions of a window (/api/visible_areas).

        Returns {window, visible_regions: [{x, y, width, height}]} or None.
        Useful for verifying a click target is reachable without hitting an
        overlapping window.
        """
        return await self._get("/api/visible_areas", {"window_index": window_index})

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

    async def find_element(
        self,
        name: str | None = None,
        control_type: str | None = None,
        window_title: str | None = None,
        window_index: int | None = None,
        index: int = 0,
    ) -> dict | None:
        """Find an element via OSO's selector engine (/api/find_element).

        Constructs an XPath-ish selector with substring matching so e.g.
        name="Save" matches a button labelled "Save as…".  Returns a dict
        with element_id (for follow-up element_click/invoke calls), rect,
        name, and control_type, or None on any failure.
        """
        if not self._caps.get("accessibility_tree", True):
            return None
        if not name and not control_type:
            return None
        role = control_type or "*"
        selector = f'//{role}[name*="{name}"]' if name else f"//{role}"
        p: dict = {"selector": selector}
        if window_index is not None:
            p["window_index"] = window_index
        elif window_title:
            p["window_title"] = window_title
        result = await self._get("/api/find_element", p)
        if result is None or not result.get("ok"):
            return None
        # pick nth match from all_matches when caller wants a specific index
        all_matches = result.get("all_matches") or []
        if all_matches and index > 0:
            idx = max(0, min(index, len(all_matches) - 1))
            m = all_matches[idx]
            return {
                "element_id": m.get("element_id"),
                "name": m.get("name", ""),
                "control_type": m.get("role", ""),
                "rect": m.get("bounds", {}),
                "method": "screen_observer_find_element",
            }
        bounds = result.get("bounds") or {}
        first = all_matches[0] if all_matches else {}
        return {
            "element_id": result.get("element_id"),
            "name": first.get("name", ""),
            "control_type": first.get("role", ""),
            "rect": bounds,
            "method": "screen_observer_find_element",
        }

    async def bring_to_foreground(
        self,
        window_title: str | None = None,
        window_index: int | None = None,
        window_uid: str | None = None,
    ) -> dict | None:
        """Ask OSO to bring a window to the foreground (/api/bring_to_foreground).

        OSO resolves the window via substring title match, index, or uid —
        so passing title="Notepad" correctly finds "Notepad.exe – Untitled".
        Returns {success, window, window_uid} or None on failure.
        """
        p: dict = {}
        if window_uid:
            p["window_uid"] = window_uid
        elif window_index is not None:
            p["window_index"] = window_index
        elif window_title:
            p["window_title"] = window_title
        else:
            return None
        return await self._get("/api/bring_to_foreground", p)

    async def observe(
        self,
        window_index: int | None = None,
        window_title: str | None = None,
    ) -> dict | None:
        """Observe the current window state (/api/observe).

        Returns a snapshot with tree_hash, description, and a diff_token
        that tracks what changed since the previous observe call.  Useful
        for verifying that an action produced the expected UI change.
        """
        p: dict = {}
        if window_index is not None:
            p["window_index"] = window_index
        elif window_title:
            p["window_title"] = window_title
        return await self._get("/api/observe", p)

    async def element_click(self, element_id: str) -> dict | None:
        """Click a previously located element by its element_id."""
        return await self._post("/api/element/click", {"element_id": element_id})

    async def element_focus(self, element_id: str) -> dict | None:
        """Focus a previously located element by its element_id."""
        return await self._post("/api/element/focus", {"element_id": element_id})

    async def element_invoke(self, element_id: str) -> dict | None:
        """Invoke the default action on an element (e.g. press a button)."""
        return await self._post("/api/element/invoke", {"element_id": element_id})

    async def element_set_value(self, element_id: str, value: str) -> dict | None:
        """Set the value of an element (e.g. fill a text field)."""
        return await self._post("/api/element/set_value", {"element_id": element_id, "value": value})
