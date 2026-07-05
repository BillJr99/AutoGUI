"""
tools.registry — the ToolRegistry: JSON Schema catalog + async dispatch.

Architecture after the platform-specific backend refactor
----------------------------------------------------------
Desktop tool implementations live in the backends/ package; shell and
filesystem implementations live in tools.shell_fs.  This module retains
ToolRegistry itself — schema catalog, dispatch table, argument aliasing
and the action-scope safety gate.

At construction time, ToolRegistry calls platform_detect.detect() and
backends.get_backend() to instantiate the correct desktop backend.  All
desktop tool functions in the registry then delegate to backend methods.

New LLM tools (platform-dependent)
-----------------------------------
  desktop_find_element   — find a UI element by accessibility properties.
                           Supported: Windows (uiautomation), Linux X11 +
                           Wayland (AT-SPI via pyatspi), WSL (PowerShell
                           UIAutomation), or via OS Screen Observer.
  desktop_get_window_tree — dump the accessibility tree for a window.
                           Supported: Windows, or via OS Screen Observer.
  desktop_describe_screen — combined a11y+OCR+VLM description via OS Screen Observer.

These tools are registered only when the active backend reports support for
them via capabilities()["find_element"] / capabilities()["get_window_tree"] /
capabilities()["screen_observer"].
"""

import json
import logging
import re
import traceback
from typing import Callable

import platform_detect
from backends import get_backend

from .browser import BrowserToolsMixin
from .desktop import DesktopToolsMixin
from .shell_fs import ShellFsToolsMixin

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tool Registry
# ---------------------------------------------------------------------------

class ToolRegistry(
    ShellFsToolsMixin,
    DesktopToolsMixin,
    BrowserToolsMixin,
):
    """
    Manages the tool catalog (JSON Schemas sent to the LLM) and dispatch table
    (Python callables invoked when the LLM issues tool_calls).

    Desktop tools are resolved at construction time via platform_detect + backends.
    Shell and filesystem tools are always registered.
    Extended tools (find_element, get_window_tree) are conditionally registered
    based on the active backend's reported capabilities().
    """

    def __init__(self, cfg: dict):
        self._cfg = cfg
        self._tools_cfg = cfg.get("tools", {})
        self._agent_cfg = cfg.get("agent", {})
        self._safety_cfg = cfg.get("safety", {})
        self._dispatch: dict[str, Callable] = {}
        self._schemas: list[dict] = []

        # Platform detection and backend selection
        self._platform_info = platform_detect.detect()
        logger.info("[tools.py] Platform: %s", platform_detect.summarize(self._platform_info))

        self._backend = None
        self._backend_caps = {}
        if self._tools_cfg.get("allowed_desktop", True):
            try:
                self._backend = get_backend(self._platform_info)
                # Attach OS Screen Observer client before querying capabilities so
                # capabilities() can reflect its availability.
                _oso_cfg = cfg.get("screen_observer", {})
                if _oso_cfg.get("enabled"):
                    try:
                        from screen_observer_client import ScreenObserverClient as _OSO
                        self._backend.set_screen_observer(_OSO(_oso_cfg))
                        logger.info(
                            "[tools.py] OS Screen Observer enabled: %s",
                            _oso_cfg.get("base_url", "http://127.0.0.1:5001"),
                        )
                    except Exception as _oso_err:
                        logger.warning("[tools.py] OS Screen Observer init failed: %s", _oso_err)
                self._backend_caps = self._backend.capabilities()
                logger.info("[tools.py] Backend capabilities: %s", self._backend_caps)
                cache_ttl = self._tools_cfg.get("perception_cache_ttl_seconds", 0.5)
                self._backend.configure_cache(cache_ttl)
            except Exception as e:
                print(f"[tools.py:ToolRegistry.__init__] Backend init failed: {e}")
                traceback.print_exc()

        # Browser backend wiring.  Dependencies (Playwright + Chromium)
        # are NOT installed by the registry — that's the job of
        # `scripts/install-dependencies.*`, run either by hand or
        # automatically via the top-level `install_dependencies` config
        # flag in build_components.  If browser tools are enabled but
        # the deps are missing, the BrowserBackend simply returns a
        # helpful "please install" error on first call.
        self._browser_backend = None
        if self._tools_cfg.get("allowed_browser", False):
            try:
                from browser_backend import BrowserBackend
                browser_cfg = cfg.get("browser", {}) or {}
                self._browser_backend = BrowserBackend(
                    headless=bool(browser_cfg.get("headless", False)),
                    screenshot_dir=browser_cfg.get(
                        "screenshot_dir", "screenshots/browser"
                    ),
                    user_data_dir=browser_cfg.get("user_data_dir") or None,
                    viewport=browser_cfg.get("viewport") or None,
                )
            except Exception as e:
                logger.warning("[tools.py] BrowserBackend init failed: %s", e)
                self._browser_backend = None

        self._build()

    def _register(self, schema: dict, fn: Callable):
        self._schemas.append(schema)
        self._dispatch[schema["function"]["name"]] = fn

    def add_tool(self, schema: dict, fn: Callable):
        """
        Public hook so callers (e.g. the agent) can extend the catalog
        after construction.  Used to inject skill_save / skill_list /
        skill_run since those need access to the agent's session state.
        """
        self._register(schema, fn)

    def _build(self):
        # Registration order is preserved from the original single-module
        # tools.py: shell, filesystem, desktop, browser, desktop_wait_for.
        self._build_shell_fs_tools()
        self._build_desktop_tools()
        self._build_browser_tools()
        self._build_wait_for_tool()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def schemas(self) -> list[dict]:
        return list(self._schemas)

    # Common parameter name aliases: models sometimes use these instead of the
    # canonical schema names.  Applied before every dispatch call.
    _ARG_ALIASES: dict[str, dict[str, str]] = {
        "shell_run": {
            "cmd": "command", "command_string": "command",
            "wd": "working_dir", "cwd": "working_dir", "dir": "working_dir",
        },
        "desktop_launch": {
            "app": "application", "program": "application",
            "name": "application", "exe": "application", "path": "application",
            "binary": "application",
        },
        "desktop_click": {
            "pos_x": "x", "posx": "x", "xpos": "x", "column": "x",
            "pos_y": "y", "posy": "y", "ypos": "y", "row": "y",
            "btn": "button", "num_clicks": "clicks",
        },
        "desktop_type": {
            "string": "text", "content": "text", "message": "text",
            "input": "text", "value": "text",
        },
        "desktop_hotkey": {
            "key": "keys", "shortcut": "keys", "hotkeys": "keys",
        },
        "desktop_mouse_move": {
            "x": "dx", "delta_x": "dx", "offset_x": "dx",
            "y": "dy", "delta_y": "dy", "offset_y": "dy",
            "button_click": "click", "left_click": "click",
        },
        "desktop_activate_window": {
            "window_title": "title", "name": "title", "process": "title",
            "process_id": "pid",
            "id": "window_id", "handle": "window_id", "wid": "window_id",
            "application": "app", "process_name": "app", "exe": "app",
        },
        "fs_read": {"file": "path", "filename": "path", "filepath": "path"},
        "fs_write": {"file": "path", "filename": "path", "filepath": "path",
                     "text": "content", "data": "content"},
        "fs_list": {"dir": "path", "directory": "path", "folder": "path"},
    }

    # Tools that mutate desktop / system state — perception cache must be
    # flushed after them, and they're the ones that dry-run / scoping gate.
    _STATE_CHANGING_TOOLS = frozenset({
        "desktop_click", "desktop_click_mark", "desktop_click_text",
        "desktop_type", "desktop_hotkey", "desktop_scroll",
        "desktop_launch", "desktop_activate_window", "desktop_mouse_move",
        "shell_run", "fs_write", "skill_run",
    })

    # Tools that affect the GUI specifically — used by action scoping to
    # decide whether the active window should be checked against the
    # allow / block lists.
    _GUI_ACTION_TOOLS = frozenset({
        "desktop_click", "desktop_click_mark", "desktop_click_text",
        "desktop_type", "desktop_hotkey", "desktop_scroll",
        "desktop_mouse_move",
    })

    async def _check_action_scope(
        self,
        tool_name: str,
        arguments: dict,
    ) -> dict | None:
        """
        Apply the safety.allowed_apps / safety.blocked_window_titles policy.
        Returns a dict (passed back as the tool result) when the action is
        blocked, or None when it should proceed.

        Both lists default to empty (no enforcement).
        """
        allowed = [a.lower() for a in self._safety_cfg.get("allowed_apps") or []]
        blocked_titles = self._safety_cfg.get("blocked_window_titles") or []

        if not allowed and not blocked_titles:
            return None

        # desktop_launch: gate on the application argument itself.
        if tool_name == "desktop_launch":
            if not allowed:
                return None
            app = str(arguments.get("application", "")).lower()
            stem = app.rsplit("/", 1)[-1].rsplit("\\", 1)[-1].rsplit(".", 1)[0]
            for entry in allowed:
                if entry in app or entry in stem:
                    return None
            return {
                "error": (
                    f"Action scope: application {app!r} is not in safety.allowed_apps "
                    f"{allowed!r}. Update config or pick a permitted app."
                )
            }

        if tool_name not in self._GUI_ACTION_TOOLS:
            return None

        # GUI action: consult the active window via the backend, if it has
        # a get_active_window capability.  Without that capability we can't
        # enforce, so let the action through — better than silent blocking.
        if self._backend is None or not self._backend_caps.get("get_active_window"):
            return None
        try:
            info = await self._backend.get_active_window()
        except Exception as e:
            logger.debug("[scope] get_active_window failed: %s", e)
            return None
        if not isinstance(info, dict) or not info.get("found"):
            return None
        win = info.get("window") or info
        title = str(win.get("title", "") or "").lower()
        app = str(win.get("app", "") or "").lower()

        for pat in blocked_titles:
            try:
                if re.search(pat, title, re.IGNORECASE):
                    return {
                        "error": (
                            f"Action scope: active window title {title!r} matches "
                            f"safety.blocked_window_titles pattern {pat!r}."
                        )
                    }
            except re.error:
                continue

        if allowed:
            for entry in allowed:
                if entry in app or entry in title:
                    return None
            return {
                "error": (
                    f"Action scope: active window app={app!r} title={title!r} "
                    f"is not in safety.allowed_apps {allowed!r}."
                )
            }
        return None

    async def dispatch(self, tool_name: str, arguments: dict) -> str:
        fn = self._dispatch.get(tool_name)
        if fn is None:
            return json.dumps({"error": f"Unknown tool: {tool_name}"})

        # Normalize any aliased parameter names before calling.
        aliases = self._ARG_ALIASES.get(tool_name, {})
        if aliases:
            arguments = {aliases.get(k, k): v for k, v in arguments.items()}

        # Dry-run: short-circuit any state-changing tool with a synthetic
        # result.  Read-only tools still execute so the model can reason
        # over real screen / filesystem state.
        if (
            self._safety_cfg.get("dry_run", False)
            and tool_name in self._STATE_CHANGING_TOOLS
        ):
            return json.dumps({
                "dry_run": True,
                "would_execute": {"tool": tool_name, "args": arguments},
                "note": "safety.dry_run is on — no real action was taken.",
            })

        # Action scoping: refuse if the request falls outside the allow list
        # or hits the block list.
        block = await self._check_action_scope(tool_name, arguments)
        if block is not None:
            return json.dumps(block)

        try:
            logger.info("[tools.py:dispatch] %s(%s)", tool_name, list(arguments.keys()))
            result = await fn(**arguments)
            if tool_name in self._STATE_CHANGING_TOOLS and self._backend is not None:
                self._backend.invalidate_cache()
            return json.dumps(result, default=str)
        except TypeError as e:
            # Print argument types to help diagnose "not str/PathLike" errors
            type_info = {k: type(v).__name__ for k, v in arguments.items()}
            msg = f"{e}  [arg types: {type_info}]"
            print(f"[tools.py:dispatch:{tool_name}] TypeError: {msg}")
            traceback.print_exc()
            return json.dumps({"error": msg})
        except Exception as e:
            print(f"[tools.py:dispatch:{tool_name}] {e}")
            traceback.print_exc()
            return json.dumps({"error": str(e)})

    def list_tools(self) -> list[str]:
        return sorted(self._dispatch.keys())

    def platform_summary(self) -> str:
        return platform_detect.summarize(self._platform_info)

    def backend_capabilities(self) -> dict:
        return dict(self._backend_caps)
