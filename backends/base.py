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
import traceback
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

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
        """
        return {"find_element": False, "get_window_tree": False}

    # ------------------------------------------------------------------
    # Display operations (pyautogui baseline)
    # ------------------------------------------------------------------

    async def screenshot(
        self,
        region: dict | None = None,
        save_dir: str = "screenshots",
        resize_width: int = 1280,
    ) -> dict:
        try:
            from PIL import Image, ImageGrab

            loop = asyncio.get_event_loop()

            def _capture() -> Image.Image:
                if region:
                    bbox = (
                        region["x"], region["y"],
                        region["x"] + region["width"],
                        region["y"] + region["height"],
                    )
                    img = ImageGrab.grab(bbox=bbox)
                else:
                    # all_screens=True spans all monitors on Windows (Pillow ≥ 7).
                    # On Linux/macOS the kwarg may not be supported — fall back.
                    try:
                        img = ImageGrab.grab(all_screens=True)
                    except TypeError:
                        img = ImageGrab.grab()
                if resize_width and img.width > resize_width:
                    ratio = resize_width / img.width
                    img = img.resize(
                        (resize_width, int(img.height * ratio)), Image.LANCZOS
                    )
                return img

            img = await loop.run_in_executor(None, _capture)

            save_path = Path(save_dir)
            save_path.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = save_path / f"screenshot_{ts}.png"
            img.save(str(filename))

            buf = io.BytesIO()
            img.save(buf, format="PNG")
            b64 = base64.b64encode(buf.getvalue()).decode()

            return {
                "path": str(filename),
                "width": img.width,
                "height": img.height,
                "base64_png": b64,
            }
        except ImportError as e:
            return {"error": f"pyautogui/Pillow not installed: {e}"}
        except Exception as e:
            logger.debug("[backend:screenshot] %s", traceback.format_exc())
            return {"error": str(e)}

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
        try:
            import pyautogui
            loop = asyncio.get_event_loop()
            # Clipboard paste handles Unicode reliably; fall back to write() otherwise.
            try:
                import pyperclip
                pyperclip.copy(text)
                await loop.run_in_executor(None, lambda: pyautogui.hotkey("ctrl", "v"))
            except ImportError:
                await loop.run_in_executor(
                    None, lambda: pyautogui.write(text, interval=0.02)
                )
            return {"success": True, "length": len(text)}
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
        return {"error": "find_element not supported on this platform"}

    async def get_window_tree(
        self,
        window_title: str | None = None,
        depth: int = 3,
    ) -> dict:
        return {"error": "get_window_tree not supported on this platform"}

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
