"""
tools.browser — browser tool registration (Phase 9).

Registers the browser_* tool family backed by the Playwright-driven
BrowserBackend.  Split out of the original tools.py.
"""

import logging

logger = logging.getLogger(__name__)


class BrowserToolsMixin:
    """Registers the browser_* tools on the ToolRegistry."""

    def _build_browser_tools(self):
        browser_ok = self._tools_cfg.get("allowed_browser", False)

        # ── Browser tools (Phase 9) ───────────────────────────────────────
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
