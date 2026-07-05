"""
tools.desktop — desktop tool registration.

Registers the desktop_* tool family (screenshot, click, type, hotkey,
scroll, launch, window management, extended a11y tools, desktop_wait_for)
against the active platform backend.  Split out of the original tools.py.
"""

import logging

from .shell_fs import _coerce_args, _coerce_path

logger = logging.getLogger(__name__)


class DesktopToolsMixin:
    """Registers the desktop_* tools on the ToolRegistry."""

    def _build_desktop_tools(self):
        desk_ok = self._tools_cfg.get("allowed_desktop", True) and self._backend is not None
        save_dir = self._tools_cfg.get("screenshot_dir", "screenshots")
        resize_w = self._tools_cfg.get("max_screenshot_width", 1280)

        # ── Desktop tools ──────────────────────────────────────────────────────
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

            # ── Extended: find_element ────────────────────────────────────
            if self._backend_caps.get("find_element") or self._backend_caps.get("screen_observer"):
                self._register(
                    {"type": "function", "function": {
                        "name": "desktop_find_element",
                        "description": (
                            "Find a UI element by its accessibility properties (name, type). "
                            "Returns the element's name, control type, and screen rect. "
                            "Use this to locate buttons/fields by name without knowing pixel positions. "
                            "Supported on Windows (UIAutomation), Linux (AT-SPI), WSL, "
                            "and via OS Screen Observer when configured."
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

            # ── Extended: get_window_tree ─────────────────────────────────
            # When OSO is attached, the tool exposes a window_index parameter
            # and a whole-screen mode.  Without OSO it falls back to the
            # native platform implementation (Windows UIAutomation today).
            # Descriptions are split so OSO terminology only enters the
            # LLM-visible schema when OSO is actually attached.
            _has_oso = bool(self._backend_caps.get("screen_observer"))
            if self._backend_caps.get("get_window_tree") or _has_oso:
                if _has_oso:
                    tree_desc = (
                        "Dump the accessibility element tree for a window or the whole screen. "
                        "Shows every UI control with its name, role and bounds — far more accurate "
                        "than guessing pixel coordinates from a screenshot. "
                        "Omit window_index for a whole-screen tree (all visible windows); pass "
                        "window_index from desktop_list_windows to scope to one window. "
                        "Encouraged before interacting with an unfamiliar or dense window. "
                        "Result is depth-limited; re-call with a specific window to drill in."
                    )
                    tree_params: dict = {
                        "window_title": {"type": "string", "description": "Window title fragment (native fallback path)."},
                        "window_index": {"type": "integer", "description": "Window index from desktop_list_windows. Omit for whole-screen tree."},
                        "depth": {"type": "integer", "description": "Tree depth (1-5). Default: 3."},
                    }
                else:
                    tree_desc = (
                        "Dump the accessibility element tree for a window. "
                        "Shows all UI controls, their names, types, and positions. "
                        "Use before interacting with an unfamiliar window."
                    )
                    tree_params = {
                        "window_title": {"type": "string", "description": "Window title fragment."},
                        "depth": {"type": "integer", "description": "Tree depth (1-5). Default: 3."},
                    }
                self._register(
                    {"type": "function", "function": {
                        "name": "desktop_get_window_tree",
                        "description": tree_desc,
                        "parameters": {"type": "object", "properties": tree_params},
                    }},
                    lambda window_title=None, window_index=None, depth=3: b.get_window_tree(
                        window_title=window_title, window_index=window_index, depth=depth,
                    ),
                )

            # ── Extended: desktop_describe_screen (OSO only) ────────────────
            # This tool has no native fallback, so it is registered only when
            # OSO is attached — keeping all OSO references out of the LLM
            # context when OSO is disabled.
            if _has_oso:
                self._register(
                    {"type": "function", "function": {
                        "name": "desktop_describe_screen",
                        "description": (
                            "Return a combined text view of the screen — prose description, "
                            "ASCII sketch, and accessibility-tree listing. Prefer this over "
                            "a raw screenshot when the UI is dense, icon-only, has small fonts, "
                            "or when you need element names/roles to click by accessibility. "
                            "Omit window_index for a whole-screen description (all visible "
                            "windows); pass window_index from desktop_list_windows to focus "
                            "on a single window. Encouraged at the start of an unfamiliar "
                            "task and whenever a screenshot left you uncertain."
                        ),
                        "parameters": {"type": "object", "properties": {
                            "window_index": {
                                "type": "integer",
                                "description": "Window index from desktop_list_windows. Omit for whole-screen view.",
                            },
                        }},
                    }},
                    lambda window_index=None: b.describe_screen(window_index=window_index),
                )

    def _build_wait_for_tool(self):
        desk_ok = self._tools_cfg.get("allowed_desktop", True) and self._backend is not None

        # ── desktop_wait_for ────────────────────────────────────────────────
        # Available whenever a desktop backend exists, regardless of
        # which other tools the agent gates.  The polling cost is low —
        # listing windows and consulting the a11y tree at 0.5 s cadence.
        if desk_ok:
            from wait_for import wait_for as _wait_for_impl
            backend_ref = self._backend

            async def _desktop_wait_for(
                window_title: str = "",
                element_name: str = "",
                text: str = "",
                window_id: str = "",
                timeout: float | None = None,
            ) -> dict:
                # ``timeout=None`` (or omitted) takes the wait_for default
                # of 15.0 s.  An explicit ``timeout=0`` is preserved and
                # passed through so wait_for can clamp it to its 0.5 s
                # floor (one quick poll); the previous ``or 15.0``
                # fallback silently turned 0 into 15 and made an
                # immediate-poll request impossible.
                effective_timeout = 15.0 if timeout is None else float(timeout)
                return await _wait_for_impl(
                    backend=backend_ref,
                    window_title=str(window_title or ""),
                    element_name=str(element_name or ""),
                    text=str(text or ""),
                    window_id=str(window_id or ""),
                    timeout=effective_timeout,
                )

            self._register(
                {"type": "function", "function": {
                    "name": "desktop_wait_for",
                    "description": (
                        "Block until a target becomes observable on the desktop, "
                        "or the timeout elapses.  Use this after desktop_launch / "
                        "browser_navigate / any action that triggers a slow UI "
                        "transition, instead of immediately clicking on something "
                        "that might not be drawn yet.  Provide one or more of: "
                        "window_title (substring), element_name (a11y name), "
                        "text (visible label via OCR), window_id.  When multiple "
                        "are given, the wait succeeds as soon as any one matches."
                    ),
                    "parameters": {"type": "object", "properties": {
                        "window_title": {"type": "string"},
                        "element_name": {"type": "string"},
                        "text": {"type": "string"},
                        "window_id": {"type": "string"},
                        "timeout": {"type": "number",
                                    "description": "Seconds to wait (default 15)."},
                    }},
                }},
                _desktop_wait_for,
            )
