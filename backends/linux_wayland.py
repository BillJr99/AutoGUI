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
        caps = super().capabilities()
        find_element = False
        try:
            import pyatspi  # noqa: F401
            find_element = True
        except Exception:
            pass
        caps.update({
            "find_element": find_element,
            "get_window_tree": False,
            "activate_window": True,
            "get_active_window": False,
            "get_window_text": True,
        })
        return caps

    async def find_element(
        self,
        name: str | None = None,
        control_type: str | None = None,
        window_title: str | None = None,
        index: int = 0,
    ) -> dict:
        """
        AT-SPI element lookup — identical logic to the X11 backend.
        Works on Wayland because AT-SPI 2 communicates over D-Bus, not X11.
        """
        if not name and not control_type:
            return {"error": "Provide name or control_type"}
        try:
            import pyatspi  # type: ignore
        except ImportError:
            return {
                "error": (
                    "pyatspi not installed. Install with: "
                    "sudo apt install python3-pyatspi gir1.2-atspi-2.0"
                )
            }

        loop = asyncio.get_event_loop()

        def _walk():
            target_role = (control_type or "").lower().strip() or None
            target_name = (name or "").lower().strip() or None
            wanted_window = (window_title or "").lower().strip() or None
            results: list[dict] = []

            try:
                desktop = pyatspi.Registry.getDesktop(0)
            except Exception as e:
                return {"error": f"AT-SPI desktop unavailable: {e}"}

            def _node_to_dict(node):
                try:
                    role_name = node.getRoleName()
                except Exception:
                    role_name = ""
                try:
                    n = node.name or ""
                except Exception:
                    n = ""
                rect = None
                try:
                    comp = node.queryComponent()
                    extents = comp.getExtents(pyatspi.DESKTOP_COORDS)
                    rect = {"x": extents.x, "y": extents.y,
                            "width": extents.width, "height": extents.height}
                except Exception:
                    pass
                return {"name": n, "control_type": role_name, "rect": rect}

            def _recurse(node):
                if len(results) > 50:
                    return
                info = _node_to_dict(node)
                ok_name = (target_name is None) or (target_name in info["name"].lower())
                ok_role = (target_role is None) or (target_role in info["control_type"].lower())
                if info["rect"] and ok_name and ok_role and (info["name"] or info["control_type"]):
                    results.append(info)
                try:
                    for child in node:
                        _recurse(child)
                except Exception:
                    return

            try:
                for app in desktop:
                    try:
                        for top in app:
                            top_name = (top.name or "").lower()
                            if wanted_window and wanted_window not in top_name:
                                continue
                            _recurse(top)
                    except Exception:
                        continue
            except Exception as e:
                return {"error": f"AT-SPI walk failed: {e}"}

            if not results:
                return {"error": "No matching element found via AT-SPI."}
            try:
                idx = int(index or 0)
            except (TypeError, ValueError):
                idx = 0
            idx = max(0, min(idx, len(results) - 1))
            return results[idx]

        try:
            return await loop.run_in_executor(None, _walk)
        except Exception as e:
            logger.debug("[wayland:find_element] %s", traceback.format_exc())
            return {"error": str(e)}

    async def screenshot(
        self,
        region: dict | None = None,
        save_dir: str = "screenshots",
        resize_width: int = 1280,
    ) -> dict:
        """
        Capture the screen using grim.

        grim writes a PNG to stdout when given "-" as the output path.
        A region can be specified as "-g 'x,y WxH'".
        """
        import time as _time
        cache_key = f"full:{resize_width}" if region is None else None
        if cache_key and self._screenshot_cache and self._cache_ttl > 0:
            ts, key, cached = self._screenshot_cache
            if key == cache_key and (_time.monotonic() - ts) < self._cache_ttl:
                return dict(cached, cache_hit=True)

        cmd = ["grim"]
        if region:
            x = int(region.get("x", 0))
            y = int(region.get("y", 0))
            w = int(region.get("width", 800))
            h = int(region.get("height", 600))
            cmd += ["-g", f"{x},{y} {w}x{h}"]
        cmd.append("-")

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
            if proc.returncode != 0:
                raise RuntimeError(stderr.decode(errors="replace").strip() or "grim failed")

            from PIL import Image
            img = Image.open(io.BytesIO(stdout))

            if resize_width and img.width > resize_width:
                ratio = resize_width / img.width
                img = img.resize((resize_width, int(img.height * ratio)), Image.LANCZOS)

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
                import time as _t
                self._screenshot_cache = (_t.monotonic(), cache_key, dict(result))
            return result
        except FileNotFoundError:
            return {"error": "grim not found — install with: sudo apt install grim"}
        except Exception as e:
            logger.warning("[wayland:screenshot] %s", e)
            logger.debug("[wayland:screenshot] %s", traceback.format_exc())
            return {"error": str(e)}

    async def click(
        self,
        x: int,
        y: int,
        button: str = "left",
        clicks: int = 1,
    ) -> dict:
        btn_code = _YDOTOOL_BUTTONS.get((button or "left").lower(), "0xC0")
        n = max(1, int(clicks))
        try:
            for _ in range(n):
                proc = await asyncio.create_subprocess_exec(
                    "ydotool", "mousemove", "--absolute", "-x", str(int(x)), "-y", str(int(y)),
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE,
                )
                await asyncio.wait_for(proc.communicate(), timeout=5)
                proc = await asyncio.create_subprocess_exec(
                    "ydotool", "click", btn_code,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE,
                )
                _, stderr = await asyncio.wait_for(proc.communicate(), timeout=5)
                if proc.returncode != 0:
                    raise RuntimeError(stderr.decode(errors="replace").strip())
            return {"success": True, "x": x, "y": y, "button": button, "clicks": n, "method": "ydotool"}
        except FileNotFoundError:
            return {"error": "ydotool not found — install with: sudo apt install ydotool (and start ydotoold)"}
        except Exception as e:
            logger.debug("[wayland:click] %s", traceback.format_exc())
            return {"error": str(e)}

    async def type_text(self, text: str) -> dict:
        if not text:
            return {"success": True, "length": 0, "method": "noop"}
        log_text = (text[:60] + "…") if len(text) > 60 else text
        logger.info("[wayland:type_text] len=%d text=%r", len(text), log_text)
        try:
            proc = await asyncio.create_subprocess_exec(
                "ydotool", "type", "--delay", "30", "--", text,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
            if proc.returncode != 0:
                raise RuntimeError(stderr.decode(errors="replace").strip())
            return {"success": True, "length": len(text), "method": "ydotool"}
        except FileNotFoundError:
            return {"error": "ydotool not found — install with: sudo apt install ydotool"}
        except Exception as e:
            logger.debug("[wayland:type_text] %s", traceback.format_exc())
            return {"error": str(e)}

    async def hotkey(self, keys: list[str]) -> dict:
        if not keys:
            return {"error": "No keys specified"}
        key_str = "+".join(str(k).lower().strip() for k in keys)
        try:
            proc = await asyncio.create_subprocess_exec(
                "ydotool", "key", "--", key_str,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=5)
            if proc.returncode != 0:
                raise RuntimeError(stderr.decode(errors="replace").strip())
            return {"success": True, "keys": list(keys), "method": "ydotool"}
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
        btn_code = _YDOTOOL_SCROLL.get((direction or "down").lower(), "0xC4")
        n = max(1, int(clicks))
        try:
            proc = await asyncio.create_subprocess_exec(
                "ydotool", "mousemove", "--absolute", "-x", str(int(x)), "-y", str(int(y)),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(proc.communicate(), timeout=5)
            for _ in range(n):
                proc = await asyncio.create_subprocess_exec(
                    "ydotool", "click", btn_code,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE,
                )
                await asyncio.wait_for(proc.communicate(), timeout=5)
            return {"success": True, "direction": direction, "clicks": n, "method": "ydotool"}
        except FileNotFoundError:
            return {"error": "ydotool not found"}
        except Exception as e:
            logger.debug("[wayland:scroll] %s", traceback.format_exc())
            return {"error": str(e)}

    async def list_windows(self) -> dict:
        """List windows via swaymsg (Sway/wlroots only)."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "swaymsg", "-t", "get_tree",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=5)
            if proc.returncode != 0:
                raise RuntimeError(stderr.decode(errors="replace").strip())
            tree = json.loads(stdout.decode())
            windows = []

            def _walk(node):
                if node.get("type") in ("con", "floating_con") and node.get("name"):
                    rect = node.get("rect") or {}
                    windows.append({
                        "id": str(node.get("id", "")),
                        "title": node.get("name", ""),
                        "app": node.get("app_id") or (node.get("window_properties") or {}).get("class", ""),
                        "pid": node.get("pid", 0) or 0,
                        "x": rect.get("x", 0),
                        "y": rect.get("y", 0),
                        "width": rect.get("width", 0),
                        "height": rect.get("height", 0),
                        "active": bool(node.get("focused")),
                    })
                for child in node.get("nodes", []) + node.get("floating_nodes", []):
                    _walk(child)

            _walk(tree)
            return {"windows": windows, "count": len(windows)}
        except FileNotFoundError:
            return {"error": "swaymsg not found — window listing requires Sway or a compatible compositor"}
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
        """Focus a window by swaymsg criteria (Sway/wlroots only)."""
        if not any([title, pid, app, window_id]):
            return {"error": "Provide at least one of: title, pid, app, window_id"}

        criteria_parts = []
        if window_id:
            criteria_parts.append(f"con_id={window_id}")
        elif pid:
            criteria_parts.append(f"pid={pid}")
        elif app:
            criteria_parts.append(f'app_id="{app}"')
        elif title:
            criteria_parts.append(f'title="{title}"')

        criteria = "[" + " ".join(criteria_parts) + "]"
        try:
            proc = await asyncio.create_subprocess_exec(
                "swaymsg", f"{criteria} focus",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=5)
            if proc.returncode != 0:
                raise RuntimeError(stderr.decode(errors="replace").strip())
            return {"success": True, "criteria": criteria, "method": "swaymsg"}
        except FileNotFoundError:
            return {"error": "swaymsg not found"}
        except Exception as e:
            logger.debug("[wayland:activate_window] %s", traceback.format_exc())
            return {"error": str(e)}

    async def get_window_text(self, max_chars: int = 50000) -> dict:
        """Select all + copy via ydotool, read via wl-paste, restore clipboard."""
        try:
            old_proc = await asyncio.create_subprocess_exec(
                "wl-paste",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            old_out, _ = await asyncio.wait_for(old_proc.communicate(), timeout=5)

            for key in ("ctrl+a", "ctrl+c"):
                proc = await asyncio.create_subprocess_exec(
                    "ydotool", "key", "--", key,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE,
                )
                await asyncio.wait_for(proc.communicate(), timeout=5)
                await asyncio.sleep(0.3)
            await asyncio.sleep(0.3)

            paste_proc = await asyncio.create_subprocess_exec(
                "wl-paste",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            paste_out, _ = await asyncio.wait_for(paste_proc.communicate(), timeout=5)
            text = paste_out.decode(errors="replace")

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
            missing = "wl-paste" if "wl-paste" in str(e) else "ydotool"
            return {"error": f"{missing} not found — install wl-clipboard or ydotool"}
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
