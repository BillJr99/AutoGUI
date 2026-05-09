"""
backends/macos.py — Desktop backend for macOS.

Screenshot uses the system `screencapture` utility (always available).
Mouse/keyboard operations use pyautogui (base class).
Window listing and app launching use `osascript` / `open`.
"""

import asyncio
import base64
import io
import logging
import traceback
from datetime import datetime
from pathlib import Path

from backends.base import DesktopBackend

logger = logging.getLogger(__name__)


class MacOSBackend(DesktopBackend):

    def capabilities(self) -> dict:
        return {"find_element": False, "get_window_tree": False}

    async def screenshot(
        self,
        region: dict | None = None,
        save_dir: str = "screenshots",
        resize_width: int = 1280,
    ) -> dict:
        try:
            from PIL import Image

            save_path = Path(save_dir)
            save_path.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = str(save_path / f"screenshot_{ts}.png")

            cmd = ["screencapture", "-x", "-t", "png"]
            if region:
                cmd += [
                    "-R",
                    f"{region['x']},{region['y']},{region['width']},{region['height']}",
                ]
            cmd.append(filename)

            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)
            if proc.returncode != 0:
                return {"error": f"screencapture failed: {stderr.decode(errors='replace').strip()}"}

            img = Image.open(filename)
            if resize_width and img.width > resize_width:
                ratio = resize_width / img.width
                img = img.resize((resize_width, int(img.height * ratio)), Image.LANCZOS)
                img.save(filename)

            buf = io.BytesIO()
            img.save(buf, format="PNG")
            b64 = base64.b64encode(buf.getvalue()).decode()

            return {
                "path": filename,
                "width": img.width,
                "height": img.height,
                "base64_png": b64,
            }
        except Exception as e:
            logger.debug("[macos:screenshot] %s", traceback.format_exc())
            return {"error": str(e)}

    async def list_windows(self) -> dict:
        script = (
            'tell application "System Events"\n'
            '    set win_list to {}\n'
            '    repeat with proc in (every process where background only is false)\n'
            '        repeat with win in windows of proc\n'
            '            set end of win_list to (name of proc & ": " & name of win)\n'
            '        end repeat\n'
            '    end repeat\n'
            '    return win_list\n'
            'end tell'
        )
        try:
            proc = await asyncio.create_subprocess_exec(
                "osascript", "-e", script,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
            raw = stdout.decode(errors="replace").strip()
            windows = [{"title": w.strip()} for w in raw.split(",") if w.strip()]
            return {"windows": windows, "count": len(windows)}
        except Exception as e:
            logger.debug("[macos:list_windows] %s", traceback.format_exc())
            return {"error": str(e)}

    async def launch(
        self,
        application: str,
        args: list[str] | None = None,
    ) -> dict:
        try:
            # Try `open -a <AppName>` first (macOS app bundles).
            cmd = ["open", "-a", application]
            if args:
                cmd += ["--args"] + args
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)
            if proc.returncode == 0:
                return {"success": True, "application": application, "args": args, "method": "open -a"}

            # Fall back to running the path directly.
            parts = [application] + (args or [])
            direct = await asyncio.create_subprocess_exec(
                *parts,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
            try:
                await asyncio.wait_for(direct.communicate(), timeout=3)
            except asyncio.TimeoutError:
                pass
            return {"success": True, "application": application, "args": args, "method": "direct"}
        except Exception as e:
            logger.debug("[macos:launch] %s", traceback.format_exc())
            return {"error": str(e)}
