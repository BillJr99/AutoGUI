"""
browser_backend.py — Playwright-driven browser automation.

Why a separate backend at all when desktop_* tools can already drive a
visible browser?  Because:

  * Browser tasks are the worst case for pyautogui — pixel positions
    move with scroll, viewport resize, font cache, ad-blockers, etc.
  * Playwright talks the DevTools Protocol directly: ARIA roles, real
    DOM events, exact selector matching, network mocking, console
    capture.  Way more reliable.

Lifecycle
---------
* Lazy launch on first browser_* call.
* Single Chromium browser, single context, single page (for v1).
  The model can navigate; it can't (yet) open multiple tabs.  Adding
  tabs would mean returning page handles which the model handles
  poorly without first-class objects, so we keep one focused page.
* `browser_close` releases everything.  Process exit also releases it.

Tools exposed (registered in tools.py when allowed_browser=true):
  browser_navigate(url) / browser_back / browser_forward / browser_reload
  browser_click(selector) / browser_fill(selector, value)
  browser_press(selector, key)
  browser_get_text(selector) — innerText of element or whole page
  browser_screenshot(full_page=False)
  browser_eval(expression) — quick JS escape hatch
  browser_close
"""

from __future__ import annotations

import asyncio
import base64
import logging
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)


class BrowserBackend:
    """Singleton-ish wrapper around a single Playwright Chromium page."""

    def __init__(
        self,
        headless: bool = False,
        screenshot_dir: str = "screenshots/browser",
        user_data_dir: str | None = None,
        viewport: dict | None = None,
    ):
        self._headless = headless
        self._screenshot_dir = Path(screenshot_dir)
        self._user_data_dir = user_data_dir
        self._viewport = viewport or {"width": 1280, "height": 800}
        self._lock = asyncio.Lock()

        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def _ensure(self) -> dict | None:
        """Return None on success, error dict on failure."""
        if self._page is not None:
            return None
        async with self._lock:
            if self._page is not None:
                return None
            try:
                from playwright.async_api import async_playwright
            except ImportError:
                return {
                    "error": (
                        "playwright not installed. Either set "
                        "tools.auto_install_playwright=true in config.json "
                        "and restart, or install manually:\n"
                        "  pip install playwright && playwright install chromium"
                    )
                }
            try:
                self._playwright = await async_playwright().start()
                if self._user_data_dir:
                    # Persistent profile — useful for keeping logins.
                    self._context = await self._playwright.chromium.launch_persistent_context(
                        user_data_dir=self._user_data_dir,
                        headless=self._headless,
                        viewport=self._viewport,
                    )
                    pages = self._context.pages
                    self._page = pages[0] if pages else await self._context.new_page()
                else:
                    self._browser = await self._playwright.chromium.launch(
                        headless=self._headless,
                    )
                    self._context = await self._browser.new_context(viewport=self._viewport)
                    self._page = await self._context.new_page()
            except Exception as e:
                logger.exception("[browser_backend] launch failed")
                return {"error": f"Playwright launch failed: {e}"}
        return None

    async def close(self) -> dict:
        try:
            if self._context is not None:
                await self._context.close()
            if self._browser is not None:
                await self._browser.close()
            if self._playwright is not None:
                await self._playwright.stop()
        except Exception as e:
            logger.debug("[browser_backend.close] %s", e)
        finally:
            self._page = None
            self._context = None
            self._browser = None
            self._playwright = None
        return {"closed": True}

    # ------------------------------------------------------------------
    # Navigation
    # ------------------------------------------------------------------

    async def navigate(self, url: str, timeout_ms: int = 30000) -> dict:
        err = await self._ensure()
        if err:
            return err
        try:
            resp = await self._page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
            return {
                "url": self._page.url,
                "status": resp.status if resp else None,
                "title": await self._page.title(),
            }
        except Exception as e:
            return {"error": f"navigate failed: {e}"}

    async def back(self) -> dict:
        err = await self._ensure()
        if err:
            return err
        try:
            await self._page.go_back(wait_until="domcontentloaded")
            return {"url": self._page.url}
        except Exception as e:
            return {"error": str(e)}

    async def forward(self) -> dict:
        err = await self._ensure()
        if err:
            return err
        try:
            await self._page.go_forward(wait_until="domcontentloaded")
            return {"url": self._page.url}
        except Exception as e:
            return {"error": str(e)}

    async def reload(self) -> dict:
        err = await self._ensure()
        if err:
            return err
        try:
            await self._page.reload(wait_until="domcontentloaded")
            return {"url": self._page.url}
        except Exception as e:
            return {"error": str(e)}

    # ------------------------------------------------------------------
    # Interaction
    # ------------------------------------------------------------------

    async def click(self, selector: str, timeout_ms: int = 10000) -> dict:
        err = await self._ensure()
        if err:
            return err
        try:
            await self._page.click(selector, timeout=timeout_ms)
            return {"clicked": selector, "url": self._page.url}
        except Exception as e:
            return {"error": f"click {selector!r} failed: {e}"}

    async def fill(self, selector: str, value: str, timeout_ms: int = 10000) -> dict:
        err = await self._ensure()
        if err:
            return err
        try:
            await self._page.fill(selector, str(value), timeout=timeout_ms)
            return {"filled": selector, "length": len(str(value))}
        except Exception as e:
            return {"error": f"fill {selector!r} failed: {e}"}

    async def press(self, selector: str, key: str, timeout_ms: int = 10000) -> dict:
        err = await self._ensure()
        if err:
            return err
        try:
            if selector:
                await self._page.press(selector, key, timeout=timeout_ms)
            else:
                await self._page.keyboard.press(key)
            return {"pressed": key}
        except Exception as e:
            return {"error": str(e)}

    # ------------------------------------------------------------------
    # Observation
    # ------------------------------------------------------------------

    async def get_text(self, selector: str = "", max_chars: int = 50000) -> dict:
        err = await self._ensure()
        if err:
            return err
        try:
            if selector:
                txt = await self._page.inner_text(selector, timeout=10000)
            else:
                txt = await self._page.inner_text("body", timeout=10000)
            truncated = len(txt) > max_chars
            txt = txt[:max_chars] if truncated else txt
            return {"text": txt, "length": len(txt), "truncated": truncated, "url": self._page.url}
        except Exception as e:
            return {"error": str(e)}

    async def screenshot(self, full_page: bool = False) -> dict:
        err = await self._ensure()
        if err:
            return err
        try:
            self._screenshot_dir.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            path = self._screenshot_dir / f"browser_{ts}.png"
            png = await self._page.screenshot(path=str(path), full_page=bool(full_page))
            b64 = base64.b64encode(png).decode()
            return {
                "path": str(path),
                "url": self._page.url,
                "title": await self._page.title(),
                "base64_png": b64,
            }
        except Exception as e:
            return {"error": str(e)}

    async def eval_js(self, expression: str) -> dict:
        err = await self._ensure()
        if err:
            return err
        try:
            value = await self._page.evaluate(expression)
            return {"value": value}
        except Exception as e:
            return {"error": str(e)}
