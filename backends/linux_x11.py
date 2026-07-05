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
        caps = super().capabilities()
        find_element = False
        try:
            import pyatspi  # noqa: F401
            find_element = True
        except Exception as e:
            logger.debug("[backend] pyatspi unavailable; find_element disabled: %s", e)
        caps.update({
            "find_element": find_element,
            "get_window_tree": False,
            "activate_window": True,
            "get_active_window": True,
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
        Locate a UI element via the GNOME/Linux Accessibility bus (AT-SPI 2).

        Requires pyatspi (and gir1.2-atspi-2.0). The whole walk runs in a
        worker thread because pyatspi calls are blocking.

        control_type filters by AT-SPI role name (case-insensitive),
        e.g. "push button", "text", "label", "menu item", "frame".
        window_title narrows the search to descendants of the matching
        top-level frame.
        """
        if not name and not control_type:
            return {"error": "Provide name or control_type"}
        try:
            import pyatspi  # type: ignore
        except ImportError:
            return {
                "error": (
                    "pyatspi not installed. Install with: "
                    "sudo apt install python3-pyatspi gir1.2-atspi-2.0  "
                    "(some apps must be launched with GTK_MODULES=gail:atk-bridge "
                    "or under a session that has accessibility enabled)."
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
                except Exception as e:
                    logger.debug("[backend:atspi] getRoleName failed on node: %s", e)
                    role_name = ""
                try:
                    n = node.name or ""
                except Exception as e:
                    logger.debug("[backend:atspi] node name read failed: %s", e)
                    n = ""
                rect = None
                try:
                    comp = node.queryComponent()
                    extents = comp.getExtents(pyatspi.DESKTOP_COORDS)
                    rect = {"x": extents.x, "y": extents.y,
                            "width": extents.width, "height": extents.height}
                except Exception as e:
                    logger.debug("[backend:atspi] node extents read failed: %s", e)
                return {"name": n, "control_type": role_name, "rect": rect}

            def _recurse(node, restrict_window=False):
                if len(results) > 50:
                    return
                info = _node_to_dict(node)
                ok_name = (target_name is None) or (target_name in info["name"].lower())
                ok_role = (target_role is None) or (target_role in info["control_type"].lower())
                if info["rect"] and ok_name and ok_role and (info["name"] or info["control_type"]):
                    results.append(info)
                try:
                    for child in node:
                        _recurse(child, restrict_window=restrict_window)
                except Exception as e:
                    logger.debug("[backend:atspi] child iteration failed, pruning branch: %s", e)
                    return

            try:
                for app in desktop:
                    try:
                        for top in app:
                            top_name = (top.name or "").lower()
                            if wanted_window and wanted_window not in top_name:
                                continue
                            _recurse(top)
                    except Exception as e:
                        logger.debug("[backend:atspi] app subtree walk failed, skipping: %s", e)
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
            logger.debug("[x11:find_element] %s", traceback.format_exc())
            return {"error": str(e)}

    async def type_text(self, text: str) -> dict:
        """
        Type text on X11.  Uses xdotool with an explicit `--delay` of
        30 ms (xdotool's default of 12 ms loses or duplicates keys on
        slow targets — this is the cause of "hello world" being typed
        as "hello ddddd").  Falls back to the base class (clipboard
        paste / pyautogui) when xdotool is missing or fails.
        """
        if not text:
            return {"success": True, "length": 0, "method": "noop"}
        log_text = (text[:60] + "…") if len(text) > 60 else text
        logger.info("[x11:type_text] len=%d text=%r", len(text), log_text)
        try:
            proc = await asyncio.create_subprocess_exec(
                "xdotool", "type", "--clearmodifiers", "--delay", "30", "--", text,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
            if proc.returncode != 0:
                err = stderr.decode(errors="replace").strip()
                raise RuntimeError(f"xdotool type failed: {err}")
            return {"success": True, "length": len(text), "method": "xdotool"}
        except FileNotFoundError:
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
            # wmctrl missing — degrade to the xdotool fallback (same schema)
            # before giving up with an install hint.
            fallback = await self._list_windows_xdotool()
            if "error" not in fallback:
                return fallback
            return {"error": "wmctrl not found — install with: sudo apt install wmctrl"}
        except Exception as e:
            logger.debug("[x11:list_windows] %s", traceback.format_exc())
            return {"error": str(e)}

    async def _list_windows_xdotool(self) -> dict:
        """wmctrl-less fallback: enumerate visible windows with xdotool.

        Returns the same schema as the wmctrl path: ``{"windows":
        [{id, pid, app, x, y, width, height, title}], "count": int}``.
        """
        try:
            proc = await asyncio.create_subprocess_exec(
                "xdotool", "search", "--onlyvisible", "--name", ".",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
            wids = [w for w in stdout.decode(errors="replace").split() if w.isdigit()]
            windows = []
            for wid in wids[:40]:  # cap the per-window subprocess fan-out
                info = {"id": hex(int(wid)), "pid": 0, "app": "",
                        "x": 0, "y": 0, "width": 0, "height": 0, "title": ""}
                try:
                    p = await asyncio.create_subprocess_exec(
                        "xdotool", "getwindowname", wid,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.DEVNULL,
                    )
                    out, _ = await asyncio.wait_for(p.communicate(), timeout=5)
                    info["title"] = out.decode(errors="replace").strip()

                    p = await asyncio.create_subprocess_exec(
                        "xdotool", "getwindowpid", wid,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.DEVNULL,
                    )
                    out, _ = await asyncio.wait_for(p.communicate(), timeout=5)
                    pid_str = out.decode(errors="replace").strip()
                    if pid_str.isdigit():
                        info["pid"] = int(pid_str)
                        try:
                            from pathlib import Path as _P
                            info["app"] = _P(f"/proc/{pid_str}/comm").read_text().strip()
                        except OSError:
                            pass

                    p = await asyncio.create_subprocess_exec(
                        "xdotool", "getwindowgeometry", "--shell", wid,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.DEVNULL,
                    )
                    out, _ = await asyncio.wait_for(p.communicate(), timeout=5)
                    for line in out.decode(errors="replace").splitlines():
                        key, _, value = line.partition("=")
                        if not value.strip().lstrip("-").isdigit():
                            continue
                        num = int(value.strip())
                        if key == "X":
                            info["x"] = num
                        elif key == "Y":
                            info["y"] = num
                        elif key == "WIDTH":
                            info["width"] = num
                        elif key == "HEIGHT":
                            info["height"] = num
                except Exception as e:
                    # keep the partial record — title/geometry best-effort
                    logger.debug("[backend:x11] window geometry probe failed for %s: %s",
                                 info.get("id"), e)
                windows.append(info)
            return {"windows": windows, "count": len(windows), "method": "xdotool"}
        except FileNotFoundError:
            return {"error": "xdotool not found — install with: sudo apt install xdotool"}
        except Exception as e:
            logger.debug("[x11:_list_windows_xdotool] %s", traceback.format_exc())
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
        except Exception as e:
            logger.debug("[backend:x11] active-window id lookup failed: %s", e)
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
        if self._screen_observer is not None and title:
            result = await self._screen_observer.bring_to_foreground(window_title=title)
            if result is not None and result.get("success"):
                return {"success": True, "method": "screen_observer",
                        "window": result.get("window", title)}

        # Find the target window
        windows_result = await self.list_windows()
        if "error" in windows_result:
            return {"error": f"Cannot list windows: {windows_result['error']}"}

        match = None
        for w in windows_result.get("windows", []):
            if window_id and w.get("id", "").lower() == window_id.lower():
                match = w
                break
            if pid and w.get("pid") == int(pid):
                match = w
                break
            if title and title.lower() in w.get("title", "").lower():
                match = w
                break
            if app and app.lower() in w.get("app", "").lower():
                match = w
                break

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
        except Exception:
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
            except Exception:
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
            restore_proc.stdin.write(old_out)  # type: ignore[union-attr]  # stdin=PIPE above
            restore_proc.stdin.close()  # type: ignore[union-attr]
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
            return {"success": True, "application": application, "args": args,
                    "pid": proc.pid, "method": "subprocess"}
        except Exception as e:
            logger.debug("[x11:launch] %s", traceback.format_exc())
            return {"error": str(e)}
