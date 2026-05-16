"""
backends/base.py — Abstract base backend + shared pyautogui implementations.

DesktopBackend is a concrete class whose default method implementations use
pyautogui (works wherever a graphical display is accessible: Windows native,
macOS, Linux/X11, WSLg).  Platform-specific subclasses override the methods
that have better native implementations (window listing, app launching, etc.).
"""

import asyncio
import base64
import io
import logging
import platform as _platform
import time
import traceback
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)


def _is_macos() -> bool:
    return _platform.system() == "Darwin"

# Disable pyautogui's fail-safe (moving mouse to top-left corner raises an
# exception).  That guard is useful for interactive scripts but breaks
# automated agents that legitimately need to click near screen edges.
try:
    import pyautogui as _pyautogui
    _pyautogui.FAILSAFE = False
except Exception:
    # ImportError when pyautogui isn't installed; on Linux without an X
    # display (e.g. WSL) pyautogui raises OSError trying to open
    # ~/.Xauthority.  Either way, platform backends that need pyautogui
    # import it lazily inside their own methods.
    pass


class DesktopBackend:
    """
    Desktop automation backend.

    Default implementations use pyautogui + Pillow for all display operations.
    Subclasses override capabilities(), list_windows(), and launch() with
    platform-native alternatives.
    """

    def __init__(self):
        # Perception cache: short-lived memoization of screenshot / window list
        # so the auto-verify cycle (and back-to-back model calls) can reuse
        # results without re-grabbing the framebuffer or shelling out to wmctrl
        # every time.  Invalidated by any state-changing tool dispatch.
        self._cache_ttl: float = 0.5  # seconds
        self._screenshot_cache: tuple[float, str, dict] | None = None
        self._windows_cache: tuple[float, dict] | None = None

        # Last set of marks emitted by get_marks() / annotate_screenshot.
        # Looked up by the desktop_click_mark tool when the model picks an id.
        self._last_marks: list[dict] = []

        # Optional OS Screen Observer client; set via set_screen_observer().
        self._screen_observer = None

    # ------------------------------------------------------------------
    # Cache control
    # ------------------------------------------------------------------

    def configure_cache(self, ttl_seconds: float):
        self._cache_ttl = max(0.0, float(ttl_seconds or 0))

    def invalidate_cache(self):
        self._screenshot_cache = None
        self._windows_cache = None

    # ------------------------------------------------------------------
    # OS Screen Observer integration
    # ------------------------------------------------------------------

    def set_screen_observer(self, client) -> None:
        """Attach an optional ScreenObserverClient for enhanced perception."""
        self._screen_observer = client
        logger.info(
            "[backend] OS Screen Observer attached: %s",
            getattr(client, "_base", "?"),
        )

    # ------------------------------------------------------------------
    # Capabilities
    # ------------------------------------------------------------------

    def capabilities(self) -> dict:
        """
        Return a dict of optional capability flags.

        Keys
        ----
        find_element     : bool — platform supports accessibility element lookup
        get_window_tree  : bool — platform supports accessibility tree dump
        screen_observer  : bool — OS Screen Observer client is attached
        """
        caps = {"find_element": False, "get_window_tree": False}
        if self._screen_observer is not None and getattr(self._screen_observer, "enabled", False):
            caps["screen_observer"] = True
        return caps

    # ------------------------------------------------------------------
    # Display operations (pyautogui baseline)
    # ------------------------------------------------------------------

    @staticmethod
    def _grab_full_screen():
        """Capture all monitors into one PIL Image.

        Preference order:
        1. mss — grabs each physical monitor individually and stitches them.
           This avoids the X11 XGetImage BadMatch error that Pillow's
           ImageGrab.grab(all_screens=True) raises on uneven multi-monitor
           setups (monitors at different heights or with gaps between them).
        2. Pillow ImageGrab(all_screens=True) — works on Windows/macOS; may
           fail on X11 with asymmetric monitor geometry.
        3. Pillow ImageGrab() — primary monitor only (last resort).
        """
        try:
            import mss as _mss
            from PIL import Image
            with _mss.mss() as sct:
                monitors = sct.monitors[1:]  # skip index 0 (virtual combined rect)
                if not monitors:
                    monitors = sct.monitors
                if len(monitors) == 1:
                    raw = sct.grab(monitors[0])
                    return Image.frombytes("RGB", raw.size, raw.rgb)
                left   = min(m["left"]              for m in monitors)
                top    = min(m["top"]               for m in monitors)
                right  = max(m["left"] + m["width"] for m in monitors)
                bottom = max(m["top"] + m["height"] for m in monitors)
                canvas = Image.new("RGB", (right - left, bottom - top), (0, 0, 0))
                for monitor in monitors:
                    raw = sct.grab(monitor)
                    img = Image.frombytes("RGB", raw.size, raw.rgb)
                    canvas.paste(img, (monitor["left"] - left, monitor["top"] - top))
                return canvas
        except ImportError:
            pass
        except Exception as exc:
            logger.info(
                "[backend] mss capture failed (%s); falling back to Pillow. "
                "On X11 with uneven monitors this may raise XGetImage errors — "
                "install mss (`pip install mss`) to avoid them.",
                exc,
            )
        from PIL import ImageGrab
        try:
            return ImageGrab.grab(all_screens=True)
        except TypeError:
            pass
        except Exception as exc:
            logger.warning(
                "[backend] ImageGrab.grab(all_screens=True) failed (%s). "
                "This is common on X11 with monitors of different heights. "
                "Install mss (`pip install mss`) for reliable multi-monitor support.",
                exc,
            )
        return ImageGrab.grab()

    async def screenshot(
        self,
        region: dict | None = None,
        save_dir: str = "screenshots",
        resize_width: int = 1280,
    ) -> dict:
        # Cache lookup — only for full-screen captures with matching resize_width.
        cache_key = f"full:{resize_width}" if region is None else None
        if cache_key and self._screenshot_cache and self._cache_ttl > 0:
            ts, key, cached = self._screenshot_cache
            if key == cache_key and (time.monotonic() - ts) < self._cache_ttl:
                return dict(cached, cache_hit=True)
        try:
            from PIL import Image, ImageGrab  # noqa: F401 (ImageGrab used by region path)

            loop = asyncio.get_event_loop()

            def _capture() -> Image.Image:
                if region:
                    bbox = (
                        region["x"], region["y"],
                        region["x"] + region["width"],
                        region["y"] + region["height"],
                    )
                    return ImageGrab.grab(bbox=bbox)
                return self._grab_full_screen()

            img = await loop.run_in_executor(None, _capture)
            if resize_width and img.width > resize_width:
                ratio = resize_width / img.width
                img = img.resize(
                    (resize_width, int(img.height * ratio)), Image.LANCZOS
                )

            save_path = Path(save_dir)
            save_path.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = save_path / f"screenshot_{ts}.png"
            img.save(str(filename))

            buf = io.BytesIO()
            img.save(buf, format="PNG")
            b64 = base64.b64encode(buf.getvalue()).decode()

            result = {
                "path": str(filename),
                "width": img.width,
                "height": img.height,
                "base64_png": b64,
            }
            if cache_key and self._cache_ttl > 0:
                self._screenshot_cache = (time.monotonic(), cache_key, dict(result))
            return result
        except ImportError as e:
            logger.warning("[backend:screenshot] PIL/Pillow unavailable: %s", e)
            return {"error": f"pyautogui/Pillow not installed: {e}"}
        except Exception as e:
            logger.warning("[backend:screenshot] capture failed: %s", e)
            logger.debug("[backend:screenshot] %s", traceback.format_exc())
            return {"error": str(e)}

    async def screenshot_marked(
        self,
        save_dir: str = "screenshots",
        resize_width: int = 1280,
    ) -> dict:
        """
        Capture the screen and overlay numbered marks on detected UI elements.

        The list of marks is also persisted on the backend instance so that
        a subsequent desktop_click_mark(mark_id) can resolve the id without
        the model having to repeat the rectangle.

        Returns the same shape as screenshot() plus a "marks" list.
        """
        try:
            from PIL import Image

            from som import annotate

            loop = asyncio.get_event_loop()

            img = await loop.run_in_executor(None, self._grab_full_screen)
            full_w, full_h = img.width, img.height

            marks = await self.get_marks()
            self._last_marks = list(marks)

            if marks:
                img = await loop.run_in_executor(None, lambda: annotate(img, marks))

            if resize_width and img.width > resize_width:
                ratio = resize_width / img.width
                img = img.resize(
                    (resize_width, int(img.height * ratio)), Image.LANCZOS
                )

            save_path = Path(save_dir)
            save_path.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = save_path / f"marked_{ts}.png"
            img.save(str(filename))

            buf = io.BytesIO()
            img.save(buf, format="PNG")
            b64 = base64.b64encode(buf.getvalue()).decode()

            # Send a compact marks list back to the model — drop the rect since
            # the visual annotation conveys position better than numbers do.
            marks_summary = [
                {
                    "id": m.get("id"),
                    "name": (m.get("name") or "")[:60],
                    "role": m.get("role", ""),
                    "kind": m.get("kind", ""),
                }
                for m in marks
            ]
            return {
                "path": str(filename),
                "width": img.width,
                "height": img.height,
                "screen_width": full_w,
                "screen_height": full_h,
                "marks": marks_summary,
                "base64_png": b64,
            }
        except ImportError as e:
            logger.warning("[backend:screenshot_marked] PIL/Pillow unavailable: %s", e)
            return {"error": f"Pillow not installed: {e}"}
        except Exception as e:
            logger.warning("[backend:screenshot_marked] capture failed: %s", e)
            logger.debug("[backend:screenshot_marked] %s", traceback.format_exc())
            return {"error": str(e)}

    async def click_mark(self, mark_id: int) -> dict:
        """
        Click the centre of the rectangle stored under the given mark id by
        the most recent screenshot_marked / get_marks call.
        """
        try:
            mid = int(mark_id)
        except (TypeError, ValueError):
            return {"error": f"Invalid mark_id: {mark_id!r}"}

        marks = self._last_marks or await self.get_marks()
        for m in marks:
            if m.get("id") == mid:
                cx = int(m["x"]) + max(1, int(m.get("width", 0)) // 2)
                cy = int(m["y"]) + max(1, int(m.get("height", 0)) // 2)
                result = await self.click(cx, cy)
                result["mark_id"] = mid
                result["resolved_to"] = {"x": cx, "y": cy, "name": m.get("name", "")}
                return result
        return {
            "error": (
                f"No mark with id {mid} in the last marked screenshot. "
                "Call desktop_screenshot_marked first to refresh the marks."
            )
        }

    async def get_marks(self) -> list[dict]:
        """
        Return a list of marks for the current screen state.

        Default implementation: one mark per top-level window, plus the
        active window's title-bar / centre region.  Subclasses with
        accessibility-tree access should override to include child controls
        (buttons, fields, etc.) of the focused window.
        """
        marks: list[dict] = []
        try:
            wins = await self.list_windows()
        except Exception:
            return marks
        if not isinstance(wins, dict) or "windows" not in wins:
            return marks

        next_id = 1
        for w in wins.get("windows", []):
            try:
                width = int(w.get("width", 0))
                height = int(w.get("height", 0))
            except (TypeError, ValueError):
                continue
            if width <= 1 or height <= 1:
                continue
            marks.append({
                "id": next_id,
                "x": int(w.get("x", 0)),
                "y": int(w.get("y", 0)),
                "width": width,
                "height": height,
                "name": (w.get("title") or w.get("app", ""))[:60],
                "role": "window",
                "kind": "window",
            })
            next_id += 1
        return marks

    async def find_text_on_screen(
        self,
        query: str,
        occurrence: int = 0,
    ) -> dict:
        """
        Locate visible text on screen and return its rect.

        Default implementation tries pytesseract (if installed) on the
        current screenshot.  Subclasses with accessibility-tree access
        should override to consult the a11y tree first — much more
        reliable than OCR for native UI elements.
        """
        if not query:
            return {"error": "query cannot be empty"}
        try:
            import pytesseract  # type: ignore
            from PIL import ImageGrab
        except ImportError:
            return {
                "error": (
                    "OCR fallback requires pytesseract + the tesseract binary. "
                    "Run `scripts/install-dependencies.sh` (or "
                    "`scripts/install-dependencies.cmd` on Windows) to install, "
                    "or set `install_dependencies: true` in config.json to have "
                    "AutoGUI run that script at startup. Alternatively use "
                    "desktop_find_element for accessibility-tree lookup, which "
                    "doesn't need OCR."
                )
            }

        loop = asyncio.get_event_loop()
        try:
            img = await loop.run_in_executor(None, self._grab_full_screen)

            data = await loop.run_in_executor(
                None,
                lambda: pytesseract.image_to_data(
                    img, output_type=pytesseract.Output.DICT
                ),
            )
        except Exception as e:
            logger.debug("[backend:find_text_on_screen] %s", traceback.format_exc())
            return {"error": f"OCR failed: {e}"}

        q = query.strip().lower()
        words = data.get("text", [])
        matches: list[dict] = []
        for i, w in enumerate(words):
            if not w or not w.strip():
                continue
            if q in w.strip().lower():
                matches.append({
                    "text": w.strip(),
                    "x": int(data["left"][i]),
                    "y": int(data["top"][i]),
                    "width": int(data["width"][i]),
                    "height": int(data["height"][i]),
                    "conf": int(data.get("conf", [0] * len(words))[i] or 0),
                })

        if not matches:
            return {"found": False, "query": query, "matches": []}

        try:
            idx = int(occurrence)
        except (TypeError, ValueError):
            idx = 0
        idx = max(0, min(idx, len(matches) - 1))
        chosen = matches[idx]
        return {
            "found": True,
            "query": query,
            "match": chosen,
            "occurrence": idx,
            "total_matches": len(matches),
            "method": "ocr",
        }

    async def click_text(self, text: str, occurrence: int = 0) -> dict:
        """Find visible text and click the centre of its bounding box."""
        result = await self.find_text_on_screen(text, occurrence=occurrence)
        if "error" in result or not result.get("found"):
            return result
        m = result["match"]
        cx = int(m["x"]) + max(1, int(m["width"]) // 2)
        cy = int(m["y"]) + max(1, int(m["height"]) // 2)
        click_r = await self.click(cx, cy)
        click_r["resolved_to"] = {"x": cx, "y": cy, "text": m["text"]}
        click_r["method"] = result.get("method", "ocr")
        return click_r

    async def click_element(
        self,
        name: str,
        control_type: str | None = None,
        window_title: str | None = None,
        index: int = 0,
        button: str = "left",
        clicks: int = 1,
    ) -> dict:
        """
        Find a UI element via the platform's accessibility API and click it.

        This is the most reliable click variant — it talks to the actual
        UI control by name/role rather than synthesizing an event at a
        guessed pixel position.  Available wherever the backend reports
        find_element=True (Windows UIAutomation, Linux AT-SPI, etc.).
        """
        if not name:
            return {"error": "name is required"}
        located = await self.find_element(
            name=name,
            control_type=control_type,
            window_title=window_title,
            index=int(index or 0),
        )
        if not isinstance(located, dict) or "error" in located:
            return located if isinstance(located, dict) else {"error": str(located)}
        rect = located.get("rect") or {}
        try:
            x = int(rect["x"]); y = int(rect["y"])
            w = int(rect.get("width", rect.get("w", 0)))
            h = int(rect.get("height", rect.get("h", 0)))
        except (KeyError, TypeError, ValueError):
            return {"error": f"find_element returned no usable rect: {located}"}
        cx = x + max(1, w // 2)
        cy = y + max(1, h // 2)
        click_r = await self.click(cx, cy, button=button, clicks=int(clicks or 1))
        click_r["resolved_to"] = {
            "x": cx, "y": cy,
            "name": located.get("name", name),
            "control_type": located.get("control_type", control_type or ""),
        }
        click_r["method"] = "a11y"
        return click_r

    async def click(
        self,
        x: int,
        y: int,
        button: str = "left",
        clicks: int = 1,
    ) -> dict:
        try:
            import pyautogui
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                None, lambda: pyautogui.click(x, y, button=button, clicks=clicks)
            )
            return {"success": True, "x": x, "y": y, "button": button, "clicks": clicks}
        except Exception as e:
            logger.debug("[backend:click] %s", traceback.format_exc())
            return {"error": str(e)}

    async def type_text(self, text: str) -> dict:
        """
        Cross-platform text entry with the most reliable method first.

        Order:
          1. Clipboard paste with the OS-correct paste shortcut
             (Cmd+V on macOS, Ctrl+V elsewhere).  Most reliable —
             one event regardless of length, perfect Unicode.
          2. pyautogui.write with a generous per-character interval
             (50 ms by default) when the clipboard path fails or
             pyperclip isn't installed.  The slower default beats
             the previous 20 ms cadence which dropped/repeated keys
             on slower windows.

        Returns a dict including which method was actually used so
        the caller can diagnose subsequent typing artefacts.
        """
        try:
            import pyautogui
            loop = asyncio.get_event_loop()
            log_text = (text[:60] + "…") if len(text) > 60 else text
            logger.info("[backend:type_text] len=%d text=%r", len(text), log_text)

            paste_modifier = "command" if _is_macos() else "ctrl"

            try:
                import pyperclip
                # Save current clipboard contents and restore afterward so
                # the user's clipboard isn't clobbered by automation.
                try:
                    saved = pyperclip.paste()
                except Exception:
                    saved = None
                pyperclip.copy(text)
                # Tiny pause so the clipboard write is visible to the OS
                # before we trigger paste.
                await asyncio.sleep(0.05)
                await loop.run_in_executor(
                    None, lambda: pyautogui.hotkey(paste_modifier, "v")
                )
                await asyncio.sleep(0.1)
                if saved is not None:
                    try:
                        pyperclip.copy(saved)
                    except Exception:
                        pass
                return {"success": True, "length": len(text), "method": "clipboard_paste"}
            except ImportError:
                pass
            except Exception as e:
                logger.warning("[backend:type_text] clipboard paste failed: %s — falling back to keystrokes", e)

            # Per-character keystroke fallback with a comfortable interval.
            await loop.run_in_executor(
                None, lambda: pyautogui.write(text, interval=0.05)
            )
            return {"success": True, "length": len(text), "method": "keystrokes"}
        except Exception as e:
            logger.debug("[backend:type_text] %s", traceback.format_exc())
            return {"error": str(e)}

    async def hotkey(self, keys: list[str]) -> dict:
        try:
            import pyautogui
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, lambda: pyautogui.hotkey(*keys))
            return {"success": True, "keys": keys}
        except Exception as e:
            logger.debug("[backend:hotkey] %s", traceback.format_exc())
            return {"error": str(e)}

    async def get_cursor_pos(self) -> dict:
        try:
            import pyautogui
            pos = pyautogui.position()
            return {"x": pos.x, "y": pos.y}
        except Exception as e:
            logger.debug("[backend:get_cursor_pos] %s", traceback.format_exc())
            return {"error": str(e)}

    async def mouse_move(self, dx: int = 0, dy: int = 0, click: bool = False) -> dict:
        try:
            import pyautogui
            loop = asyncio.get_event_loop()

            def _move():
                pos = pyautogui.position()
                from_x, from_y = pos.x, pos.y
                to_x = max(1, from_x + dx)
                to_y = max(1, from_y + dy)
                pyautogui.moveTo(to_x, to_y)
                if click:
                    pyautogui.click()
                return from_x, from_y, to_x, to_y

            from_x, from_y, to_x, to_y = await loop.run_in_executor(None, _move)
            return {
                "success": True,
                "from": {"x": from_x, "y": from_y},
                "to": {"x": to_x, "y": to_y},
                "clicked": click,
            }
        except Exception as e:
            logger.debug("[backend:mouse_move] %s", traceback.format_exc())
            return {"error": str(e)}

    async def scroll(
        self,
        x: int,
        y: int,
        clicks: int = 3,
        direction: str = "down",
    ) -> dict:
        try:
            import pyautogui
            loop = asyncio.get_event_loop()
            amount = -clicks if direction == "down" else clicks
            await loop.run_in_executor(
                None, lambda: pyautogui.scroll(amount, x=x, y=y)
            )
            return {"success": True, "direction": direction, "clicks": clicks}
        except Exception as e:
            logger.debug("[backend:scroll] %s", traceback.format_exc())
            return {"error": str(e)}

    # ------------------------------------------------------------------
    # Window management — subclasses provide native implementations
    # ------------------------------------------------------------------

    async def list_windows(self) -> dict:
        """List open windows; tries OS Screen Observer first when configured."""
        if self._screen_observer is not None:
            result = await self._screen_observer.get_windows()
            if result is not None:
                return result
            logger.warning("[backend:list_windows] OS Screen Observer unavailable; falling back to native method")
        return {"error": "list_windows not implemented for this platform"}

    async def launch(
        self,
        application: str,
        args: list[str] | None = None,
    ) -> dict:
        return {"error": "launch not implemented for this platform"}

    # ------------------------------------------------------------------
    # Optional extended capabilities
    # ------------------------------------------------------------------

    async def find_element(
        self,
        name: str | None = None,
        control_type: str | None = None,
        window_title: str | None = None,
        index: int = 0,
    ) -> dict:
        """Find element via a11y API; tries OS Screen Observer tree walk when configured."""
        if self._screen_observer is not None and name:
            result = await self._screen_observer.find_element_in_tree(
                name=str(name),
                control_type=control_type,
                index=int(index or 0),
            )
            if result is not None:
                return result
            logger.warning("[backend:find_element] OS Screen Observer unavailable; falling back to native method")
        return {"error": "find_element not supported on this platform"}

    async def get_window_tree(
        self,
        window_title: str | None = None,
        depth: int = 3,
    ) -> dict:
        """Dump accessibility tree; tries OS Screen Observer /api/structure when configured."""
        if self._screen_observer is not None:
            result = await self._screen_observer.get_structure()
            if result is not None:
                return result
            logger.warning("[backend:get_window_tree] OS Screen Observer unavailable; falling back to native method")
        return {"error": "get_window_tree not supported on this platform"}

    async def describe_screen(self, window_index: int | None = None) -> dict:
        """
        Return a combined text description of the screen using OS Screen Observer.

        Combines accessibility tree prose, OCR text, and optional VLM
        interpretation depending on how OSScreenObserver is configured.
        Returns an error dict if the observer is not configured or not reachable.
        """
        if self._screen_observer is None:
            return {"error": "screen_observer is not configured in config.json (set screen_observer.enabled=true)"}
        result = await self._screen_observer.get_description(window_index=window_index)
        if result is None:
            return {
                "error": (
                    "OS Screen Observer is not reachable. "
                    "Start it with: python main.py --mode inspect "
                    "(in the OSScreenObserver directory)"
                )
            }
        return result

    async def activate_window(
        self,
        title: str = "",
        pid: int = 0,
        app: str = "",
        window_id: str = "",
    ) -> dict:
        return {"error": "activate_window not supported on this platform"}

    async def get_active_window(self) -> dict:
        return {"found": False, "error": "get_active_window not supported on this platform"}

    async def get_window_text(self, max_chars: int = 50000) -> dict:
        return {"error": "get_window_text not supported on this platform"}
