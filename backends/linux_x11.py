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
        return {"find_element": False, "get_window_tree": False, "activate_window": True, "get_active_window": True, "get_window_text": True}

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
        """List windows via wmctrl -lGpx (includes pid, geometry, wm_class)."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "wmctrl", "-lGpx",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
            lines = stdout.decode(errors="replace").strip().splitlines()
            windows = []
            for line in lines:
                # Format: wid desktop pid x y width height wm_class host title
                parts = line.split(None, 9)
                if len(parts) < 9:
                    continue
                wid = parts[0]
                pid = int(parts[2]) if parts[2].isdigit() else 0
                wm_class = parts[7]
                app = wm_class.split(".")[-1] if "." in wm_class else wm_class
                windows.append({
                    "id": wid,
                    "pid": pid,
                    "app": app,
                    "x": int(parts[3]),
                    "y": int(parts[4]),
                    "width": int(parts[5]),
                    "height": int(parts[6]),
                    "title": parts[9] if len(parts) > 9 else "",
                })
            return {"windows": windows, "count": len(windows)}
        except FileNotFoundError:
            return {"error": "wmctrl not found — install with: sudo apt install wmctrl"}
        except Exception as e:
            logger.debug("[x11:list_windows] %s", traceback.format_exc())
            return {"error": str(e)}

    async def _get_active_wid_int(self) -> int | None:
        """Return the active window ID as an integer, or None on failure."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "xdotool", "getactivewindow",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
            dec = stdout.decode().strip()
            return int(dec) if dec else None
        except Exception:
            return None

    async def activate_window(
        self,
        title: str = "",
        pid: int = 0,
        app: str = "",
        window_id: str = "",
    ) -> dict:
        """
        Focus an X11 window.  Method priority:
          1. wmctrl -ia <wid>  (raises window and gives focus)
          2. xdotool windowfocus --sync <wid>
          3. Click the title-bar area as a final fallback.
        Verification uses xdotool getactivewindow.
        """
        if not any([title, pid, app, window_id]):
            return {"error": "Provide at least one of: title, pid, app, window_id (hex wid)"}

        # Find the target window
        windows_result = await self.list_windows()
        if "error" in windows_result:
            return {"error": f"Cannot list windows: {windows_result['error']}"}

        match = None
        for w in windows_result.get("windows", []):
            if window_id and w.get("id", "").lower() == window_id.lower():
                match = w; break
            if pid and w.get("pid") == int(pid):
                match = w; break
            if title and title.lower() in w.get("title", "").lower():
                match = w; break
            if app and app.lower() in w.get("app", "").lower():
                match = w; break

        if not match:
            return {"error": "No window found matching the given criteria"}

        wid_hex = match["id"]
        try:
            wid_int = int(wid_hex, 16)
        except ValueError:
            wid_int = None

        # --- Try wmctrl -ia ---
        try:
            proc = await asyncio.create_subprocess_exec(
                "wmctrl", "-ia", wid_hex,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(proc.communicate(), timeout=5)
            if proc.returncode == 0:
                active = await self._get_active_wid_int()
                if wid_int is not None and active == wid_int:
                    return {"success": True, "active": True, "method": "wmctrl", **match}
        except FileNotFoundError:
            pass
        except Exception as e:
            logger.debug("[x11:activate_window:wmctrl] %s", traceback.format_exc())

        # --- Try xdotool windowfocus ---
        if wid_int is not None:
            try:
                proc = await asyncio.create_subprocess_exec(
                    "xdotool", "windowfocus", "--sync", str(wid_int),
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE,
                )
                await asyncio.wait_for(proc.communicate(), timeout=5)
                if proc.returncode == 0:
                    active = await self._get_active_wid_int()
                    if active == wid_int:
                        return {"success": True, "active": True, "method": "xdotool", **match}
            except FileNotFoundError:
                pass
            except Exception as e:
                logger.debug("[x11:activate_window:xdotool] %s", traceback.format_exc())

        # --- Click fallback ---
        if match.get("width"):
            cx = match["x"] + match["width"] // 2
            cy = match["y"] + 15
            click_r = await self.click(cx, cy)
            if not click_r.get("error"):
                return {"success": True, "active": True, "method": "click_fallback", **match}

        return {"success": True, "active": False, "method": "failed", **match}

    async def get_active_window(self) -> dict:
        """Return info about the active X11 window using xdotool."""
        wid_int = await self._get_active_wid_int()
        if wid_int is None:
            return {"found": False}
        wid_hex = hex(wid_int)
        windows_result = await self.list_windows()
        for w in windows_result.get("windows", []):
            try:
                if int(w["id"], 16) == wid_int:
                    return {"found": True, "window": {**w, "active": True}}
            except ValueError:
                pass
        return {"found": True, "window": {"id": wid_hex, "active": True}}

    async def get_window_text(self, max_chars: int = 50000) -> dict:
        """Select all + copy via xdotool, read via xclip, restore clipboard."""
        try:
            # Save old clipboard
            old_proc = await asyncio.create_subprocess_exec(
                "xclip", "-o", "-selection", "clipboard",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            old_out, _ = await asyncio.wait_for(old_proc.communicate(), timeout=5)

            # Select all + copy
            for key in ("ctrl+a", "ctrl+c"):
                proc = await asyncio.create_subprocess_exec(
                    "xdotool", "key", "--clearmodifiers", key,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE,
                )
                await asyncio.wait_for(proc.communicate(), timeout=5)
                await asyncio.sleep(0.3)
            await asyncio.sleep(0.3)

            # Read clipboard
            paste_proc = await asyncio.create_subprocess_exec(
                "xclip", "-o", "-selection", "clipboard",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            paste_out, _ = await asyncio.wait_for(paste_proc.communicate(), timeout=5)
            text = paste_out.decode(errors="replace")

            # Restore old clipboard
            restore_proc = await asyncio.create_subprocess_exec(
                "xclip", "-i", "-selection", "clipboard",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            restore_proc.stdin.write(old_out)
            restore_proc.stdin.close()
            await asyncio.wait_for(restore_proc.wait(), timeout=5)

            truncated = len(text) > max_chars
            text = text[:max_chars] if truncated else text
            return {"text": text, "length": len(text), "truncated": truncated}
        except FileNotFoundError as e:
            missing = "xclip" if "xclip" in str(e) else "xdotool"
            return {"error": f"{missing} not found — install with: sudo apt install {missing}"}
        except Exception as e:
            logger.debug("[x11:get_window_text] %s", traceback.format_exc())
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
