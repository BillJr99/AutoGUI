"""
backends/linux_wayland.py — Desktop backend for Linux/Wayland.

Screenshot uses grim (wlroots compositors; works with Sway, Hyprland, etc.).
Mouse/keyboard control uses ydotool (requires the ydotoold daemon running).
Window listing uses swaymsg (Sway-specific; returns error on other compositors).
App launching uses subprocess with a new session.

Install prerequisites:
  sudo apt install grim ydotool
  sudo systemctl enable --now ydotool  # or: sudo ydotoold &
  # Sway only:
  sudo apt install sway
"""

import asyncio
import base64
import io
import json
import logging
import traceback
from datetime import datetime
from pathlib import Path

from backends.base import DesktopBackend

logger = logging.getLogger(__name__)

# ydotool mouse button codes
_YDOTOOL_BUTTONS = {
    "left": "0xC0",
    "right": "0xC1",
    "middle": "0xC2",
}
_YDOTOOL_SCROLL = {
    "up": "0xC3",
    "down": "0xC4",
}


class WaylandBackend(DesktopBackend):

    def capabilities(self) -> dict:
        find_element = False
        try:
            import pyatspi  # noqa: F401
            find_element = True
        except Exception:
            pass
        return {
            "find_element": find_element,
            "get_window_tree": False,
            "activate_window": True,
            "get_active_window": False,
            "get_window_text": True,
        }

    async def find_element(
        self,
        name: str | None = None,
        control_type: str | None = None,
        window_title: str | None = None,
        index: int = 0,
    ) -> dict:
        """AT-SPI lookup. Same implementation strategy as the X11 backend."""
        from backends.linux_x11 import X11Backend
        return await X11Backend.find_element(
            self,
            name=name,
            control_type=control_type,
            window_title=window_title,
            index=index,
        )

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

            cmd = ["grim"]
            if region:
                cmd += [
                    "-g",
                    f"{region['x']},{region['y']} {region['width']}x{region['height']}",
                ]
            cmd.append(filename)

            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)
            if proc.returncode != 0:
                return {"error": f"grim failed: {stderr.decode(errors='replace').strip()}"}

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
        except FileNotFoundError:
            logger.warning("[wayland:screenshot] grim not found on PATH")
            return {"error": "grim not found — install with: sudo apt install grim"}
        except Exception as e:
            logger.warning("[wayland:screenshot] capture failed: %s", e)
            logger.debug("[wayland:screenshot] %s", traceback.format_exc())
            return {"error": str(e)}

    async def click(
        self,
        x: int,
        y: int,
        button: str = "left",
        clicks: int = 1,
    ) -> dict:
        btn = _YDOTOOL_BUTTONS.get(button, "0xC0")
        try:
            for _ in range(clicks):
                proc = await asyncio.create_subprocess_exec(
                    "ydotool", "click", btn,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE,
                )
                await asyncio.wait_for(proc.communicate(), timeout=5)
            return {"success": True, "x": x, "y": y, "button": button, "clicks": clicks}
        except FileNotFoundError:
            return {"error": "ydotool not found — install with: sudo apt install ydotool"}
        except Exception as e:
            logger.debug("[wayland:click] %s", traceback.format_exc())
            return {"error": str(e)}

    async def type_text(self, text: str) -> dict:
        """
        Type text on Wayland via ydotool.  --key-delay slows the per-key
        cadence so slow targets don't drop or repeat characters.
        """
        if not text:
            return {"success": True, "length": 0, "method": "noop"}
        log_text = (text[:60] + "…") if len(text) > 60 else text
        logger.info("[wayland:type_text] len=%d text=%r", len(text), log_text)
        try:
            proc = await asyncio.create_subprocess_exec(
                "ydotool", "type", "--key-delay", "20", "--", text,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(proc.communicate(), timeout=60)
            if proc.returncode != 0:
                # Older ydotool versions don't accept --key-delay; retry.
                proc = await asyncio.create_subprocess_exec(
                    "ydotool", "type", "--", text,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE,
                )
                await asyncio.wait_for(proc.communicate(), timeout=60)
            return {"success": True, "length": len(text), "method": "ydotool"}
        except FileNotFoundError:
            return {"error": "ydotool not found — install with: sudo apt install ydotool"}
        except Exception as e:
            logger.debug("[wayland:type_text] %s", traceback.format_exc())
            return {"error": str(e)}

    async def hotkey(self, keys: list[str]) -> dict:
        # ydotool key expects X11 keysym names joined by +, e.g. "ctrl+c"
        combo = "+".join(keys)
        try:
            proc = await asyncio.create_subprocess_exec(
                "ydotool", "key", "--", combo,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(proc.communicate(), timeout=5)
            return {"success": True, "keys": keys}
        except FileNotFoundError:
            return {"error": "ydotool not found — install with: sudo apt install ydotool"}
        except Exception as e:
            logger.debug("[wayland:hotkey] %s", traceback.format_exc())
            return {"error": str(e)}

    async def scroll(
        self,
        x: int,
        y: int,
        clicks: int = 3,
        direction: str = "down",
    ) -> dict:
        btn = _YDOTOOL_SCROLL.get(direction, "0xC4")
        try:
            for _ in range(clicks):
                proc = await asyncio.create_subprocess_exec(
                    "ydotool", "click", btn,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE,
                )
                await asyncio.wait_for(proc.communicate(), timeout=5)
            return {"success": True, "direction": direction, "clicks": clicks}
        except FileNotFoundError:
            return {"error": "ydotool not found — install with: sudo apt install ydotool"}
        except Exception as e:
            logger.debug("[wayland:scroll] %s", traceback.format_exc())
            return {"error": str(e)}

    async def list_windows(self) -> dict:
        """List windows via swaymsg (Sway compositor only)."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "swaymsg", "-t", "get_tree",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
            tree = json.loads(stdout.decode())

            windows: list[dict] = []

            def _walk(node: dict) -> None:
                if node.get("type") == "con" and node.get("name") and "focused" in node:
                    windows.append({"title": node["name"], "id": node.get("id")})
                for child in node.get("nodes", []) + node.get("floating_nodes", []):
                    _walk(child)

            _walk(tree)
            return {"windows": windows, "count": len(windows)}
        except FileNotFoundError:
            return {
                "error": (
                    "swaymsg not found — window listing is only supported on the Sway compositor. "
                    "Other Wayland compositors are not currently supported."
                )
            }
        except Exception as e:
            logger.debug("[wayland:list_windows] %s", traceback.format_exc())
            return {"error": str(e)}

    async def activate_window(
        self,
        title: str = "",
        pid: int = 0,
        app: str = "",
        window_id: str = "",
    ) -> dict:
        """
        Focus a Wayland window.  Only works on the Sway compositor via swaymsg.
        Falls back to clicking the window center as a universal last resort.
        Other compositors (GNOME, KDE, Hyprland) are not yet supported natively.
        """
        if not any([title, pid, app, window_id]):
            return {"error": "Provide at least one of: title, pid, app, window_id"}

        # Try swaymsg focus criteria (Sway compositor only)
        if title:
            criteria = f'[title="{title}"]'
        elif app:
            criteria = f'[app_id="{app}"]'
        else:
            criteria = None

        if criteria:
            try:
                proc = await asyncio.create_subprocess_exec(
                    "swaymsg", f"{criteria} focus",
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE,
                )
                _, stderr = await asyncio.wait_for(proc.communicate(), timeout=5)
                if proc.returncode == 0:
                    return {"success": True, "active": True, "method": "swaymsg"}
            except FileNotFoundError:
                pass
            except Exception as e:
                logger.debug("[wayland:activate_window:swaymsg] %s", traceback.format_exc())

        # Click fallback: find the window in swaymsg tree and click its center
        windows_result = await self.list_windows()
        for w in windows_result.get("windows", []):
            match = False
            if title and title.lower() in w.get("title", "").lower():
                match = True
            elif app and app.lower() in w.get("title", "").lower():
                match = True
            if match and w.get("width"):
                cx = w["x"] + w["width"] // 2
                cy = w["y"] + 15
                click_r = await self.click(cx, cy)
                if not click_r.get("error"):
                    return {"success": True, "active": True, "method": "click_fallback"}

        return {"error": "activate_window: no supported method available on this Wayland compositor"}

    async def get_window_text(self, max_chars: int = 50000) -> dict:
        """Select all + copy via ydotool, read via wl-paste, restore with wl-copy."""
        try:
            # Save old clipboard
            old_proc = await asyncio.create_subprocess_exec(
                "wl-paste", "--no-newline",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            old_out, _ = await asyncio.wait_for(old_proc.communicate(), timeout=5)

            # Select all + copy
            for combo in ("ctrl+a", "ctrl+c"):
                proc = await asyncio.create_subprocess_exec(
                    "ydotool", "key", "--", combo,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE,
                )
                await asyncio.wait_for(proc.communicate(), timeout=5)
                await asyncio.sleep(0.3)
            await asyncio.sleep(0.3)

            # Read clipboard
            paste_proc = await asyncio.create_subprocess_exec(
                "wl-paste", "--no-newline",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            paste_out, _ = await asyncio.wait_for(paste_proc.communicate(), timeout=5)
            text = paste_out.decode(errors="replace")

            # Restore old clipboard
            restore_proc = await asyncio.create_subprocess_exec(
                "wl-copy",
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
            missing = "wl-paste/wl-copy" if "wl-" in str(e) else "ydotool"
            return {"error": f"{missing} not found — install with: sudo apt install wl-clipboard ydotool"}
        except Exception as e:
            logger.debug("[wayland:get_window_text] %s", traceback.format_exc())
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
                pass
            return {"success": True, "application": application, "args": args}
        except Exception as e:
            logger.debug("[wayland:launch] %s", traceback.format_exc())
            return {"error": str(e)}
