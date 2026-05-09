"""
backends/linux_x11.py — Desktop backend for Linux/X11.

Screenshot and mouse control use pyautogui (base class).
Text input uses xdotool type for better Unicode support.
Window listing uses wmctrl.
App launching uses subprocess with a new session to detach from the terminal.

Install prerequisites:
  sudo apt install wmctrl xdotool python3-tk python3-dev
"""

import asyncio
import logging
import traceback

from backends.base import DesktopBackend

logger = logging.getLogger(__name__)


class X11Backend(DesktopBackend):

    def capabilities(self) -> dict:
        return {"find_element": False, "get_window_tree": False}

    async def type_text(self, text: str) -> dict:
        """Use xdotool type for reliable Unicode input on X11."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "xdotool", "type", "--clearmodifiers", "--", text,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
            if proc.returncode != 0:
                err = stderr.decode(errors="replace").strip()
                raise RuntimeError(f"xdotool type failed: {err}")
            return {"success": True, "length": len(text)}
        except FileNotFoundError:
            # xdotool not installed — fall back to pyautogui
            return await super().type_text(text)
        except Exception as e:
            logger.debug("[x11:type_text] %s", traceback.format_exc())
            return {"error": str(e)}

    async def list_windows(self) -> dict:
        try:
            proc = await asyncio.create_subprocess_exec(
                "wmctrl", "-l",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
            lines = stdout.decode(errors="replace").strip().splitlines()
            windows = []
            for line in lines:
                parts = line.split(None, 3)
                if len(parts) >= 4:
                    windows.append({
                        "wid": parts[0],
                        "desktop": parts[1],
                        "host": parts[2],
                        "title": parts[3],
                    })
            return {"windows": windows, "count": len(windows)}
        except FileNotFoundError:
            return {"error": "wmctrl not found — install with: sudo apt install wmctrl"}
        except Exception as e:
            logger.debug("[x11:list_windows] %s", traceback.format_exc())
            return {"error": str(e)}

    async def launch(
        self,
        application: str,
        args: list[str] | None = None,
    ) -> dict:
        try:
            parts = [application] + (args or [])
            proc = await asyncio.create_subprocess_exec(
                *parts,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
            try:
                _, stderr = await asyncio.wait_for(proc.communicate(), timeout=3)
                if proc.returncode not in (0, None):
                    raise RuntimeError(stderr.decode(errors="replace").strip())
            except asyncio.TimeoutError:
                pass  # GUI app still running — expected
            return {"success": True, "application": application, "args": args}
        except Exception as e:
            logger.debug("[x11:launch] %s", traceback.format_exc())
            return {"error": str(e)}
