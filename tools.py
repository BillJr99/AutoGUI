"""
tools.py — Tool registry and shell/filesystem implementations.

Architecture after the platform-specific backend refactor
----------------------------------------------------------
Desktop tool implementations have been moved into the backends/ package.
This module retains:
  1. Shell tool implementations (shell_run) — platform-agnostic via subprocess.
  2. Filesystem tool implementations (fs_read, fs_write, fs_list) — platform-agnostic.
  3. ToolRegistry — manages JSON Schema descriptors + async dispatch table.

At construction time, ToolRegistry calls platform_detect.detect() and
backends.get_backend() to instantiate the correct desktop backend.  All
desktop tool functions in the registry then delegate to backend methods.

New LLM tools (platform-dependent)
-----------------------------------
  desktop_find_element   — find a UI element by accessibility properties.
                           Supported: Windows (uiautomation), Linux X11 +
                           Wayland (AT-SPI via pyatspi), WSL (PowerShell
                           UIAutomation).
  desktop_get_window_tree — dump the accessibility tree for a window.
                           Supported: Windows.

These tools are registered only when the active backend reports support for
them via capabilities()["find_element"] / capabilities()["get_window_tree"].
"""

import asyncio
import json
import logging
import re
import traceback
from datetime import datetime
from pathlib import Path
from typing import Callable, Coroutine

import platform_detect
from backends import get_backend
from backends.base import DesktopBackend

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Destructive command guard
# ---------------------------------------------------------------------------

_DESTRUCTIVE_PATTERNS = [
    r"\brm\s+-[rRf]",
    r"\brmdir\b",
    r"\bformat\b",
    r"\bdd\s+if=",
    r"\bmkfs\b",
    r"\bshred\b",
    r"\btruncate\b",
    r"DROP\s+TABLE",
    r"DROP\s+DATABASE",
]


def _is_destructive(command: str) -> bool:
    for pat in _DESTRUCTIVE_PATTERNS:
        if re.search(pat, command, re.IGNORECASE):
            return True
    return False


def _coerce_path(path, default: str = "") -> str:
    """
    Normalize a path/string argument that the LLM may send as a dict instead of a string.
    Returns a plain string suitable for Path(), subprocess args, or cwd.
    """
    if path is None:
        return default
    if isinstance(path, (str, bytes)):
        return path.decode() if isinstance(path, bytes) else path
    if isinstance(path, dict):
        # Try common key names the LLM uses when it hallucinates a dict
        for key in (
            "path", "file", "filename",
            "dir", "directory",
            "name", "application", "app", "executable", "cmd", "command",
            "value", "text", "content",
        ):
            if key in path:
                return str(path[key])
        # Single-value dict: take the only value
        if len(path) == 1:
            return str(next(iter(path.values())))
        return default
    return str(path)


def _coerce_args(args) -> list[str]:
    """
    Normalize the `args` parameter that the LLM may send as a JSON string
    (e.g. "[]" or "[\"--flag\"]") instead of an actual list.
    Always returns a list of strings.
    """
    if args is None:
        return []
    if isinstance(args, list):
        return [str(a) for a in args]
    if isinstance(args, str):
        stripped = args.strip()
        if not stripped:
            return []
        try:
            parsed = json.loads(stripped)
            if isinstance(parsed, list):
                return [str(a) for a in parsed]
            return [str(parsed)]
        except (json.JSONDecodeError, ValueError):
            return [stripped]
    return [str(a) for a in args] if hasattr(args, "__iter__") else [str(args)]


# ---------------------------------------------------------------------------
# Shell tool
# ---------------------------------------------------------------------------

async def shell_run(
    command: str,
    working_dir=None,
    timeout: int = 30,
    confirm_destructive: bool = True,
) -> dict:
    """Execute a shell command; return stdout, stderr, exit_code, timed_out."""
    command = _coerce_path(command) if not isinstance(command, str) else command
    # LLM sometimes sends working_dir as a dict; normalize to string or None.
    if working_dir is not None:
        working_dir = _coerce_path(working_dir) or None
    if confirm_destructive and _is_destructive(command):
        return {
            "stdout": "",
            "stderr": (
                f"SAFETY BLOCK: '{command}' matches a destructive pattern. "
                "Confirm with the user before running."
            ),
            "exit_code": -1,
            "timed_out": False,
        }

    logger.info("[tools.py:shell_run] cmd=%r cwd=%s", command, working_dir)

    import platform as _platform
    if _platform.system() == "Windows":
        args = ["cmd", "/C", command]
    else:
        args = ["/bin/sh", "-c", command]

    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=working_dir,
        )
        try:
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            timed_out = False
        except asyncio.TimeoutError:
            proc.kill()
            stdout_b, stderr_b = b"", b""
            timed_out = True

        return {
            "stdout": stdout_b.decode("utf-8", errors="replace").strip(),
            "stderr": stderr_b.decode("utf-8", errors="replace").strip(),
            "exit_code": proc.returncode if not timed_out else -1,
            "timed_out": timed_out,
        }
    except Exception as e:
        print(f"[tools.py:shell_run] {e}")
        traceback.print_exc()
        return {"stdout": "", "stderr": str(e), "exit_code": -1, "timed_out": False}


# ---------------------------------------------------------------------------
# Filesystem tools
# ---------------------------------------------------------------------------

async def fs_read(path: str, max_bytes: int = 65536) -> dict:
    path = _coerce_path(path)
    try:
        p = Path(path).expanduser()
        if not p.exists():
            return {"error": f"Path does not exist: {path}"}
        if p.is_dir():
            return {"error": f"Path is a directory; use fs_list instead: {path}"}
        content = p.read_bytes()
        truncated = len(content) > max_bytes
        return {
            "content": content[:max_bytes].decode("utf-8", errors="replace"),
            "truncated": truncated,
            "size_bytes": len(content),
        }
    except Exception as e:
        print(f"[tools.py:fs_read] {e}")
        traceback.print_exc()
        return {"error": str(e)}


async def fs_write(
    path: str,
    content: str,
    mode: str = "w",
    snapshot_dir: str = "",
) -> dict:
    """
    Write content to a file. When overwriting an existing file and
    snapshot_dir is non-empty, copy the original aside first so the
    write is recoverable.
    """
    path = _coerce_path(path)
    content = content if isinstance(content, str) else str(content)
    try:
        p = Path(path).expanduser()
        snapshot_path: str | None = None
        if mode == "w" and snapshot_dir and p.exists() and p.is_file():
            try:
                import shutil
                snap_dir = Path(snapshot_dir).expanduser()
                snap_dir.mkdir(parents=True, exist_ok=True)
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                slug = re.sub(r"[^A-Za-z0-9._-]+", "_", str(p))
                snap = snap_dir / f"{ts}__{slug.strip('_')[:120]}"
                shutil.copy2(p, snap)
                snapshot_path = str(snap)
            except Exception as e:
                logger.warning("[fs_write] Snapshot failed for %s: %s", p, e)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open(mode, encoding="utf-8") as f:
            f.write(content)
        result = {"success": True, "path": str(p), "bytes_written": len(content.encode())}
        if snapshot_path:
            result["snapshot"] = snapshot_path
        return result
    except Exception as e:
        print(f"[tools.py:fs_write] {e}")
        traceback.print_exc()
        return {"error": str(e)}


async def fs_list(path: str, pattern: str = "*", max_entries: int = 200) -> dict:
    path = _coerce_path(path)
    try:
        p = Path(path).expanduser()
        if not p.exists():
            return {"error": f"Path does not exist: {path}"}
        entries = []
        for item in sorted(p.glob(pattern))[:max_entries]:
            stat = item.stat()
            entries.append({
                "name": item.name,
                "type": "dir" if item.is_dir() else "file",
                "size_bytes": stat.st_size,
                "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(),
            })
        return {"entries": entries, "count": len(entries), "path": str(p)}
    except Exception as e:
        print(f"[tools.py:fs_list] {e}")
        traceback.print_exc()
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Tool Registry
# ---------------------------------------------------------------------------

class ToolRegistry:
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
                self._backend_caps = self._backend.capabilities()
                logger.info("[tools.py] Backend capabilities: %s", self._backend_caps)
                cache_ttl = self._tools_cfg.get("perception_cache_ttl_seconds", 0.5)
                self._backend.configure_cache(cache_ttl)
            except Exception as e:
                print(f"[tools.py:ToolRegistry.__init__] Backend init failed: {e}")
                traceback.print_exc()

        # Optional eager OCR install — runs once at startup when the user
        # has explicitly opted in via tools.auto_install_tesseract.  Loud
        # by design (prints every command) so the user sees what's happening.
        if self._tools_cfg.get("auto_install_tesseract", False):
            try:
                from tesseract_install import ensure
                snap = ensure(auto_install=True)
                if snap.get("ready"):
                    logger.info("[tools.py] OCR ready: %s", snap.get("tesseract_binary"))
                else:
                    logger.warning("[tools.py] OCR install: %s", snap.get("message"))
            except Exception as e:
                logger.warning("[tools.py] tesseract_install failed: %s", e)

        # Same pattern for Playwright — only when browser tools are enabled.
        # auto_install_playwright defaults to True when allowed_browser=true:
        # if you've committed to browser automation, you almost certainly want
        # the deps installed.  Set the flag explicitly to false to opt out.
        self._browser_backend = None
        if self._tools_cfg.get("allowed_browser", False):
            if self._tools_cfg.get("auto_install_playwright", True):
                try:
                    from playwright_install import ensure as ensure_pw
                    snap = ensure_pw(auto_install=True)
                    if not snap.get("ready"):
                        logger.warning("[tools.py] Playwright install: %s", snap.get("message"))
                    else:
                        logger.info("[tools.py] Playwright + Chromium ready.")
                except Exception as e:
                    logger.warning("[tools.py] playwright_install failed: %s", e)
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
        shell_ok = self._tools_cfg.get("allowed_shell", True)
        fs_ok = self._tools_cfg.get("allowed_filesystem", True)
        desk_ok = self._tools_cfg.get("allowed_desktop", True) and self._backend is not None
        browser_ok = self._tools_cfg.get("allowed_browser", False)
        confirm = self._agent_cfg.get("confirm_destructive", True)
        shell_timeout = self._tools_cfg.get("shell_timeout_seconds", 30)
        save_dir = self._tools_cfg.get("screenshot_dir", "screenshots")
        resize_w = self._tools_cfg.get("max_screenshot_width", 1280)

        # ── Shell ──────────────────────────────────────────────────────
        if shell_ok:
            self._register(
                {
                    "type": "function",
                    "function": {
                        "name": "shell_run",
                        "description": (
                            "Execute a shell command and return stdout, stderr, and exit code. "
                            "Destructive patterns are blocked unless the user confirms."
                        ),
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "command": {"type": "string"},
                                "working_dir": {"type": "string"},
                            },
                            "required": ["command"],
                        },
                    },
                },
                # Parameter names must exactly match the schema keys so that
                # fn(**arguments) binds correctly when the LLM calls the tool.
                lambda command, working_dir=None: shell_run(
                    command, working_dir=working_dir,
                    timeout=shell_timeout, confirm_destructive=confirm,
                ),
            )

        # ── Filesystem ─────────────────────────────────────────────────
        if fs_ok:
            self._register(
                {"type": "function", "function": {
                    "name": "fs_read",
                    "description": "Read file contents. Returns text and truncation flag.",
                    "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
                }},
                fs_read,
            )
            snapshot_dir = self._safety_cfg.get(
                "fs_write_snapshot_dir", ""
            )  # empty string = snapshots disabled
            self._register(
                {"type": "function", "function": {
                    "name": "fs_write",
                    "description": "Write or append content to a file.",
                    "parameters": {"type": "object", "properties": {
                        "path": {"type": "string"},
                        "content": {"type": "string"},
                        "mode": {"type": "string", "enum": ["w", "a"]},
                    }, "required": ["path", "content"]},
                }},
                lambda path, content, mode="w": fs_write(
                    path, content, mode=mode, snapshot_dir=snapshot_dir,
                ),
            )
            self._register(
                {"type": "function", "function": {
                    "name": "fs_list",
                    "description": "List files and directories. Supports glob patterns.",
                    "parameters": {"type": "object", "properties": {
                        "path": {"type": "string"},
                        "pattern": {"type": "string"},
                    }, "required": ["path"]},
                }},
                fs_list,
            )

        # ── Desktop tools ──────────────────────────────────────────────
        if desk_ok:
            b = self._backend

            self._register(
                {"type": "function", "function": {
                    "name": "desktop_screenshot",
                    "description": (
                        "Capture a screenshot of the screen or a region. "
                        "Returns base64-encoded PNG. Use before clicking to verify screen state. "
                        "Tip: when you need to click a labelled UI element, prefer "
                        "desktop_screenshot_marked + desktop_click_mark instead — much more "
                        "reliable than guessing pixel coordinates."
                    ),
                    "parameters": {"type": "object", "properties": {
                        "region": {"type": "object", "description": "Optional {x,y,width,height}",
                                   "properties": {"x": {"type": "integer"}, "y": {"type": "integer"},
                                                  "width": {"type": "integer"}, "height": {"type": "integer"}}},
                    }},
                }},
                lambda region=None: b.screenshot(region=region, save_dir=save_dir, resize_width=resize_w),
            )
            self._register(
                {"type": "function", "function": {
                    "name": "desktop_screenshot_marked",
                    "description": (
                        "Capture a screenshot with numbered Set-of-Mark boxes drawn over "
                        "detected UI elements (top-level windows, plus accessibility-tree "
                        "controls when available). Use this BEFORE attempting to click any "
                        "named element — then call desktop_click_mark(mark_id) using one of "
                        "the ids returned in the 'marks' list. Far more reliable than "
                        "guessing pixel coordinates from a plain screenshot."
                    ),
                    "parameters": {"type": "object", "properties": {}},
                }},
                lambda: b.screenshot_marked(save_dir=save_dir, resize_width=resize_w),
            )
            self._register(
                {"type": "function", "function": {
                    "name": "desktop_click_mark",
                    "description": (
                        "Click the centre of a previously-marked UI element by its mark id. "
                        "Requires a recent desktop_screenshot_marked call. "
                        "If the screen has changed materially since then, refresh the marks "
                        "first or the id may resolve to the wrong location."
                    ),
                    "parameters": {"type": "object", "properties": {
                        "mark_id": {"type": "integer",
                                    "description": "Numeric id from the marks list."},
                    }, "required": ["mark_id"]},
                }},
                lambda mark_id: b.click_mark(int(mark_id)),
            )
            self._register(
                {"type": "function", "function": {
                    "name": "desktop_click_text",
                    "description": (
                        "Find a visible text label on screen and click its centre. "
                        "On Windows/macOS this consults the accessibility tree first; "
                        "as a fallback (or on Linux) it uses OCR via pytesseract if "
                        "installed. Prefer this over pixel-coordinate clicks for any "
                        "text-labelled button or link."
                    ),
                    "parameters": {"type": "object", "properties": {
                        "text": {"type": "string",
                                 "description": "The visible label to click (case-insensitive)."},
                        "occurrence": {"type": "integer",
                                       "description": "0-based index when multiple matches (default 0)."},
                    }, "required": ["text"]},
                }},
                lambda text, occurrence=0: b.click_text(str(text), int(occurrence) if occurrence else 0),
            )
            self._register(
                {"type": "function", "function": {
                    "name": "desktop_find_text",
                    "description": (
                        "Locate visible text on screen and return its bounding rect "
                        "without clicking. Useful for verifying that something is "
                        "displayed, or for computing a click position relative to a label."
                    ),
                    "parameters": {"type": "object", "properties": {
                        "text": {"type": "string"},
                        "occurrence": {"type": "integer"},
                    }, "required": ["text"]},
                }},
                lambda text, occurrence=0: b.find_text_on_screen(str(text), int(occurrence) if occurrence else 0),
            )
            self._register(
                {"type": "function", "function": {
                    "name": "desktop_click",
                    "description": (
                        "Click the mouse at absolute screen coordinates (x, y). "
                        "Coordinates are in screen pixels — use desktop_list_windows to get "
                        "a window's bounding box, then compute click position from it. "
                        "Never guess coordinates; always derive them from window bounds."
                    ),
                    "parameters": {"type": "object", "properties": {
                        "x": {"type": "integer"}, "y": {"type": "integer"},
                        "button": {"type": "string", "enum": ["left", "right", "middle"]},
                        "clicks": {"type": "integer", "description": "1=single, 2=double"},
                    }, "required": ["x", "y"]},
                }},
                lambda x, y, button="left", clicks=1: b.click(max(1, int(x)), max(1, int(y)), button=button, clicks=int(clicks)),
            )
            self._register(
                {"type": "function", "function": {
                    "name": "desktop_type",
                    "description": (
                        "Type text into the currently focused window. "
                        "IMPORTANT: You MUST call desktop_click inside the target window "
                        "first to give it keyboard focus — otherwise the text goes nowhere."
                    ),
                    "parameters": {"type": "object", "properties": {
                        "text": {"type": "string"}
                    }, "required": ["text"]},
                }},
                lambda text: b.type_text(text),
            )
            self._register(
                {"type": "function", "function": {
                    "name": "desktop_hotkey",
                    "description": (
                        "Press a keyboard shortcut. Keys are held simultaneously. "
                        "Common browser shortcuts (Edge/Chrome/Firefox): "
                        "['ctrl','t']=new tab, ['ctrl','l']=focus address bar (use instead of "
                        "clicking it), ['ctrl','w']=close tab, ['ctrl','tab']=next tab, "
                        "['ctrl','r']=reload, ['ctrl','n']=new window. "
                        "Other: ['ctrl','c']=copy, ['ctrl','v']=paste, ['ctrl','z']=undo, "
                        "['alt','f4']=close window, ['ctrl','alt','t']=terminal."
                    ),
                    "parameters": {"type": "object", "properties": {
                        "keys": {"type": "array", "items": {"type": "string"},
                                 "description": "Key names in order, e.g. ['ctrl','l']"},
                    }, "required": ["keys"]},
                }},
                lambda keys: b.hotkey(keys),
            )
            self._register(
                {"type": "function", "function": {
                    "name": "desktop_list_windows",
                    "description": (
                        "List currently open windows with their titles, PIDs, window IDs, and screen bounding boxes "
                        "(x, y, width, height in screen pixels). "
                        "Use x/y/width/height to compute click coordinates: "
                        "to click inside a window use x=window.x+window.width//2, "
                        "y=window.y+window.height//2 for center, or offset slightly from "
                        "x+20, y+80 to hit the client area below the title bar. "
                        "Use the id (window handle) or pid with desktop_activate_window to bring a window to front — "
                        "id is the most precise match."
                    ),
                    "parameters": {"type": "object", "properties": {}},
                }},
                lambda: b.list_windows(),
            )
            self._register(
                {"type": "function", "function": {
                    "name": "desktop_launch",
                    "description": (
                        "Launch an application by executable name or full path. "
                        "Automatically brings the window to the foreground after launching — "
                        "check window_activated in the result. "
                        "If the app is already running its existing window is activated instead "
                        "of opening a new instance. "
                        "If window_activated is false, call desktop_activate_window as a follow-up."
                    ),
                    "parameters": {"type": "object", "properties": {
                        "application": {"type": "string"},
                        "args": {"type": "array", "items": {"type": "string"}},
                    }, "required": ["application"]},
                }},
                lambda application, args=None: b.launch(_coerce_path(application), _coerce_args(args)),
            )
            self._register(
                {"type": "function", "function": {
                    "name": "desktop_activate_window",
                    "description": (
                        "Bring an already-open window to the foreground (make it the active focused window). "
                        "Call this before desktop_type or desktop_hotkey to guarantee the right window has focus. "
                        "Uses the best available native method for the platform (SetForegroundWindow on Windows/WSL, "
                        "AppleScript on macOS, wmctrl/xdotool on X11, swaymsg on Wayland) and falls back to "
                        "clicking the title-bar area if native focus is not confirmed. "
                        "Returns active=true when focus is verified. "
                        "Match priority: window_id (most precise) > pid > title > app. "
                        "Use id and pid from desktop_list_windows for exact matching."
                    ),
                    "parameters": {"type": "object", "properties": {
                        "title": {"type": "string",
                                  "description": "Partial window title (case-insensitive substring)"},
                        "pid": {"type": "integer",
                                "description": "Process ID from desktop_list_windows"},
                        "app": {"type": "string",
                                "description": "Process/app name (case-insensitive substring), e.g. 'msedge', 'chrome'"},
                        "window_id": {"type": "string",
                                      "description": "Window handle string (id field from desktop_list_windows) — most precise"},
                    }},
                }},
                lambda title="", pid=0, app="", window_id="": b.activate_window(
                    title=str(title) if title else "",
                    pid=int(pid) if pid else 0,
                    app=str(app) if app else "",
                    window_id=str(window_id) if window_id else "",
                ),
            )
            if self._backend_caps.get("get_active_window"):
                self._register(
                    {"type": "function", "function": {
                        "name": "desktop_get_active_window",
                        "description": (
                            "Return information about the currently focused window "
                            "(title, app, pid, id, x, y, width, height). "
                            "Use this to verify that desktop_activate_window succeeded, "
                            "or to check which window is in front before taking action. "
                            "Returns {found: false} when no foreground window is detected."
                        ),
                        "parameters": {"type": "object", "properties": {}},
                    }},
                    lambda: b.get_active_window(),
                )
            self._register(
                {"type": "function", "function": {
                    "name": "desktop_scroll",
                    "description": (
                        "Scroll the focused window. "
                        "Each 'click' scrolls one page (Page Down / Page Up) on Windows/WSL, "
                        "or one mouse-wheel notch (~3 lines) on other platforms. "
                        "Call desktop_activate_window first to ensure the right window has focus. "
                        "x and y are optional: when both are > 0 the window at that position is "
                        "focused before scrolling; pass 0 (or omit) to scroll the active window."
                    ),
                    "parameters": {"type": "object", "properties": {
                        "x": {"type": "integer", "description": "Screen x coordinate of scroll target (0 = active window)"},
                        "y": {"type": "integer", "description": "Screen y coordinate of scroll target (0 = active window)"},
                        "clicks": {"type": "integer", "description": "Number of scroll steps (default 3)"},
                        "direction": {"type": "string", "enum": ["up", "down"]},
                    }},
                }},
                lambda x=0, y=0, clicks=3, direction="down": b.scroll(
                    int(x) if x else 0, int(y) if y else 0,
                    clicks=int(clicks) if clicks else 3,
                    direction=direction or "down",
                ),
            )
            if self._backend_caps.get("get_window_text"):
                self._register(
                    {"type": "function", "function": {
                        "name": "desktop_get_window_text",
                        "description": (
                            "Extract the visible text from the focused window by selecting all (Ctrl+A / Cmd+A) "
                            "and copying to clipboard. Returns up to 50,000 characters of text. "
                            "Useful for reading search results, web page content, documents, or any "
                            "window whose contents cannot be fully seen in a screenshot. "
                            "For a browser: call desktop_activate_window, click in the page body area, "
                            "then call this tool. The clipboard is restored after reading. "
                            "Returns {text, length, truncated}."
                        ),
                        "parameters": {"type": "object", "properties": {
                            "max_chars": {"type": "integer",
                                          "description": "Maximum characters to return (default 50000)"},
                        }},
                    }},
                    lambda max_chars=50000: b.get_window_text(max_chars=int(max_chars) if max_chars else 50000),
                )
            self._register(
                {"type": "function", "function": {
                    "name": "desktop_get_cursor_pos",
                    "description": (
                        "Return the current mouse cursor position in screen pixels (x, y). "
                        "Use this before desktop_mouse_move to know the starting position."
                    ),
                    "parameters": {"type": "object", "properties": {}},
                }},
                lambda: b.get_cursor_pos(),
            )
            self._register(
                {"type": "function", "function": {
                    "name": "desktop_mouse_move",
                    "description": (
                        "Move the mouse cursor by a relative offset (dx, dy) from its current "
                        "position and optionally click. "
                        "Workflow: (1) call desktop_screenshot to see the screen, "
                        "(2) call desktop_get_cursor_pos to get the current position, "
                        "(3) compute the offset to reach the target, "
                        "(4) call desktop_mouse_move with that dx/dy. "
                        "Positive dx=right, negative dx=left; positive dy=down, negative dy=up."
                    ),
                    "parameters": {"type": "object", "properties": {
                        "dx": {"type": "integer", "description": "Horizontal offset in pixels"},
                        "dy": {"type": "integer", "description": "Vertical offset in pixels"},
                        "click": {"type": "boolean", "description": "Left-click at the new position"},
                    }, "required": ["dx", "dy"]},
                }},
                lambda dx=0, dy=0, click=False: b.mouse_move(int(dx), int(dy), bool(click)),
            )

            # ── Extended: find_element ────────────────────────────────
            if self._backend_caps.get("find_element"):
                self._register(
                    {"type": "function", "function": {
                        "name": "desktop_find_element",
                        "description": (
                            "Find a UI element by its accessibility properties (name, type). "
                            "Returns the element's name, control type, and screen rect. "
                            "Use this to locate buttons/fields by name without knowing pixel positions. "
                            "Supported on Windows (UIAutomation), Linux (AT-SPI), and WSL."
                        ),
                        "parameters": {"type": "object", "properties": {
                            "name": {"type": "string", "description": "Element name or label (partial match)."},
                            "control_type": {"type": "string",
                                             "description": "e.g. 'ButtonControl', 'EditControl', 'WindowControl'"},
                            "window_title": {"type": "string", "description": "Restrict to this window."},
                            "index": {"type": "integer", "description": "0-based index when multiple match."},
                        }},
                    }},
                    lambda name=None, control_type=None, window_title=None, index=0:
                        b.find_element(name=name, control_type=control_type,
                                       window_title=window_title, index=index),
                )
                self._register(
                    {"type": "function", "function": {
                        "name": "desktop_click_element",
                        "description": (
                            "Find a UI element via the OS accessibility API and click it. "
                            "PREFER THIS over desktop_click whenever the target has a "
                            "visible name/label — it talks to the actual control instead "
                            "of guessing pixel positions, so it survives DPI scaling, "
                            "window moves, and UI redraws. Fall back to desktop_click_text "
                            "or desktop_click_mark only when no a11y handle is exposed."
                        ),
                        "parameters": {"type": "object", "properties": {
                            "name": {"type": "string",
                                     "description": "Element name or label (partial match)."},
                            "control_type": {"type": "string",
                                             "description": "Control type filter, e.g. 'ButtonControl' on Windows or 'push button' on Linux AT-SPI."},
                            "window_title": {"type": "string",
                                             "description": "Restrict to this window's subtree."},
                            "index": {"type": "integer",
                                      "description": "0-based index when multiple match."},
                            "button": {"type": "string", "enum": ["left", "right", "middle"]},
                            "clicks": {"type": "integer",
                                       "description": "1=single, 2=double."},
                        }, "required": ["name"]},
                    }},
                    lambda name, control_type=None, window_title=None, index=0,
                           button="left", clicks=1: b.click_element(
                        name=str(name),
                        control_type=str(control_type) if control_type else None,
                        window_title=str(window_title) if window_title else None,
                        index=int(index) if index else 0,
                        button=str(button) if button else "left",
                        clicks=int(clicks) if clicks else 1,
                    ),
                )

            # ── Extended: get_window_tree ─────────────────────────────
            if self._backend_caps.get("get_window_tree"):
                self._register(
                    {"type": "function", "function": {
                        "name": "desktop_get_window_tree",
                        "description": (
                            "Dump the accessibility element tree for a window. "
                            "Shows all UI controls, their names, types, and positions. "
                            "Use before interacting with an unfamiliar window. "
                            "Supported on Windows and macOS."
                        ),
                        "parameters": {"type": "object", "properties": {
                            "window_title": {"type": "string", "description": "Window title fragment."},
                            "depth": {"type": "integer", "description": "Tree depth (1-5). Default: 3."},
                        }},
                    }},
                    lambda window_title=None, depth=3: b.get_window_tree(window_title=window_title, depth=depth),
                )

        # ── Browser tools (Phase 9) ───────────────────────────────────
        # Independent of desk_ok: a config can enable browser tools while
        # disabling desktop tools, useful for headless CI-style usage.
        if browser_ok and self._browser_backend is not None:
            bb_ = self._browser_backend
            self._register(
                {"type": "function", "function": {
                    "name": "browser_navigate",
                    "description": (
                        "Open a URL in the dedicated Playwright-driven Chromium "
                        "browser. Prefer the browser_* tool family for any task "
                        "that lives on a web page — it's far more reliable than "
                        "driving a regular browser via desktop_click coordinates."
                    ),
                    "parameters": {"type": "object", "properties": {
                        "url": {"type": "string"},
                    }, "required": ["url"]},
                }},
                lambda url: bb_.navigate(str(url)),
            )
            self._register(
                {"type": "function", "function": {
                    "name": "browser_back",
                    "description": "Go back one history entry in the browser.",
                    "parameters": {"type": "object", "properties": {}},
                }},
                lambda: bb_.back(),
            )
            self._register(
                {"type": "function", "function": {
                    "name": "browser_forward",
                    "description": "Go forward one history entry in the browser.",
                    "parameters": {"type": "object", "properties": {}},
                }},
                lambda: bb_.forward(),
            )
            self._register(
                {"type": "function", "function": {
                    "name": "browser_reload",
                    "description": "Reload the current page.",
                    "parameters": {"type": "object", "properties": {}},
                }},
                lambda: bb_.reload(),
            )
            self._register(
                {"type": "function", "function": {
                    "name": "browser_click",
                    "description": (
                        "Click an element matching a Playwright selector. "
                        "Selectors: CSS ('button.primary'), text ('text=Sign in'), "
                        "ARIA role ('role=button[name=\"Sign in\"]'), or xpath "
                        "('xpath=//button[contains(.,\"Sign in\")]')."
                    ),
                    "parameters": {"type": "object", "properties": {
                        "selector": {"type": "string"},
                    }, "required": ["selector"]},
                }},
                lambda selector: bb_.click(str(selector)),
            )
            self._register(
                {"type": "function", "function": {
                    "name": "browser_fill",
                    "description": (
                        "Fill an input/textarea identified by a Playwright "
                        "selector with the given value. Replaces existing content."
                    ),
                    "parameters": {"type": "object", "properties": {
                        "selector": {"type": "string"},
                        "value": {"type": "string"},
                    }, "required": ["selector", "value"]},
                }},
                lambda selector, value: bb_.fill(str(selector), str(value)),
            )
            self._register(
                {"type": "function", "function": {
                    "name": "browser_press",
                    "description": (
                        "Press a key inside an element (selector required) or in "
                        "the page focus (selector empty). Examples: 'Enter', "
                        "'Tab', 'Control+a', 'PageDown'."
                    ),
                    "parameters": {"type": "object", "properties": {
                        "selector": {"type": "string"},
                        "key": {"type": "string"},
                    }, "required": ["key"]},
                }},
                lambda key, selector="": bb_.press(str(selector or ""), str(key)),
            )
            self._register(
                {"type": "function", "function": {
                    "name": "browser_get_text",
                    "description": (
                        "Return the visible text of an element (selector) or of "
                        "the whole page body (no selector). Truncated to "
                        "max_chars."
                    ),
                    "parameters": {"type": "object", "properties": {
                        "selector": {"type": "string"},
                        "max_chars": {"type": "integer"},
                    }},
                }},
                lambda selector="", max_chars=50000: bb_.get_text(
                    str(selector or ""), int(max_chars) if max_chars else 50000
                ),
            )
            self._register(
                {"type": "function", "function": {
                    "name": "browser_screenshot",
                    "description": (
                        "Capture a PNG of the current browser page. "
                        "full_page=true captures the entire scrolled-out page."
                    ),
                    "parameters": {"type": "object", "properties": {
                        "full_page": {"type": "boolean"},
                    }},
                }},
                lambda full_page=False: bb_.screenshot(bool(full_page)),
            )
            self._register(
                {"type": "function", "function": {
                    "name": "browser_eval",
                    "description": (
                        "Evaluate a JavaScript expression in the page context "
                        "and return its value. Useful for reading window state, "
                        "extracting structured data, or driving rich web apps."
                    ),
                    "parameters": {"type": "object", "properties": {
                        "expression": {"type": "string"},
                    }, "required": ["expression"]},
                }},
                lambda expression: bb_.eval_js(str(expression)),
            )
            self._register(
                {"type": "function", "function": {
                    "name": "browser_close",
                    "description": "Shut down the browser instance and free resources.",
                    "parameters": {"type": "object", "properties": {}},
                }},
                lambda: bb_.close(),
            )

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
