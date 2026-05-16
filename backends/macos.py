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
        caps = super().capabilities()
        caps.update({"find_element": False, "get_window_tree": False, "activate_window": True, "get_active_window": True, "get_window_text": True})
        return caps

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
            logger.warning("[macos:screenshot] capture failed: %s", e)
            logger.debug("[macos:screenshot] %s", traceback.format_exc())
            return {"error": str(e)}

    async def list_windows(self) -> dict:
        # Returns JSON array of {title, app, pid, x, y, width, height} objects.
        script = (
            'set out to "["\n'
            'set firstItem to true\n'
            'tell application "System Events"\n'
            '  repeat with p in (every process where background only is false and visible is true)\n'
            '    set pidVal to unix id of p\n'
            '    set appName to name of p\n'
            '    repeat with w in windows of p\n'
            '      try\n'
            '        set pos to position of w\n'
            '        set sz to size of w\n'
            '        set t to name of w\n'
            '        if firstItem is false then set out to out & ","\n'
            '        set firstItem to false\n'
            '        set out to out & "{\\"title\\":\\"" & t & "\\",\\"app\\":\\"" & appName & "\\",\\"pid\\":" & pidVal & ",\\"x\\":" & (item 1 of pos) & ",\\"y\\":" & (item 2 of pos) & ",\\"width\\":" & (item 1 of sz) & ",\\"height\\":" & (item 2 of sz) & "}"\n'
            '      end try\n'
            '    end repeat\n'
            '  end repeat\n'
            'end tell\n'
            'return out & "]"'
        )
        try:
            proc = await asyncio.create_subprocess_exec(
                "osascript", "-e", script,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
            raw = stdout.decode(errors="replace").strip()
            import json as _json
            windows = _json.loads(raw)
            return {"windows": windows, "count": len(windows)}
        except Exception as e:
            logger.debug("[macos:list_windows] %s", traceback.format_exc())
            return {"error": str(e)}

    async def activate_window(
        self,
        title: str = "",
        pid: int = 0,
        app: str = "",
        window_id: str = "",
    ) -> dict:
        """
        Bring a macOS window to the front.  Match priority: app > pid > title.
        Uses AppleScript `set frontmost of p to true`.  Falls back to clicking
        the title-bar area of the window if a matching window with known bounds
        can be found but focus is not confirmed.
        """
        if not any([title, pid, app]):
            return {"error": "Provide at least one of: title, pid, app"}
        if self._screen_observer is not None and title:
            result = await self._screen_observer.bring_to_foreground(window_title=title)
            if result is not None and result.get("success"):
                return {"success": True, "method": "screen_observer",
                        "window": result.get("window", title)}

        # Build the AppleScript process selector
        if app:
            find_clause = f'set p to first process whose name contains "{app}"'
        elif pid:
            find_clause = f'set p to first process whose unix id is {int(pid)}'
        else:
            # Title match: iterate to find the process that has a window with this title
            safe_title = title.replace('"', '\\"')
            find_clause = (
                f'set needle to "{safe_title}"\n'
                '  set p to missing value\n'
                '  repeat with proc in (every process where background only is false and visible is true)\n'
                '    repeat with w in windows of proc\n'
                '      if name of w contains needle then\n'
                '        set p to proc\n'
                '        exit repeat\n'
                '      end if\n'
                '    end repeat\n'
                '    if p is not missing value then exit repeat\n'
                '  end repeat\n'
                '  if p is missing value then error "No window with title containing: " & needle'
            )

        script = (
            f'tell application "System Events"\n'
            f'  try\n'
            f'    {find_clause}\n'
            f'    set frontmost of p to true\n'
            f'    set appName to name of p\n'
            f'    set pidVal to unix id of p\n'
            f'    -- Check focus\n'
            f'    set fp to first process whose frontmost is true\n'
            f'    set isActive to (fp is p)\n'
            f'    return "{{" & chr(34) & "success" & chr(34) & ":true," & chr(34) & "active" & chr(34) & ":" & isActive & "," & chr(34) & "app" & chr(34) & ":" & chr(34) & appName & chr(34) & "," & chr(34) & "pid" & chr(34) & ":" & pidVal & "}}"\n'
            f'  on error errMsg\n'
            f'    return "{{" & chr(34) & "success" & chr(34) & ":false," & chr(34) & "error" & chr(34) & ":" & chr(34) & errMsg & chr(34) & "}}"\n'
            f'  end try\n'
            f'end tell'
        )
        try:
            proc = await asyncio.create_subprocess_exec(
                "osascript", "-e", script,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)
            raw = stdout.decode(errors="replace").strip()

            import json as _json
            try:
                result = _json.loads(raw)
            except Exception:
                if proc.returncode != 0:
                    return {"error": stderr.decode(errors="replace").strip() or raw}
                return {"success": True, "active": True, "raw": raw}

            if not result.get("success"):
                return {"error": result.get("error", "activate_window failed")}

            if not result.get("active"):
                # Click fallback: find bounds then click title bar
                wins = (await self.list_windows()).get("windows", [])
                match = None
                for w in wins:
                    if app and app.lower() in w.get("app", "").lower():
                        match = w; break
                    if pid and w.get("pid") == pid:
                        match = w; break
                    if title and title.lower() in w.get("title", "").lower():
                        match = w; break
                if match:
                    cx = match["x"] + match["width"] // 2
                    cy = match["y"] + 15
                    click_r = await self.click(cx, cy)
                    if not click_r.get("error"):
                        result["active"] = True
                        result["method"] = "click_fallback"

            return {"success": True, **result}
        except Exception as e:
            logger.debug("[macos:activate_window] %s", traceback.format_exc())
            return {"error": str(e)}

    async def get_active_window(self) -> dict:
        """Return info about the currently focused application/window on macOS."""
        script = (
            'tell application "System Events"\n'
            '  try\n'
            '    set p to first process whose frontmost is true\n'
            '    set appName to name of p\n'
            '    set pidVal to unix id of p\n'
            '    try\n'
            '      set w to front window of p\n'
            '      set pos to position of w\n'
            '      set sz to size of w\n'
            '      set t to name of w\n'
            '      return "{\\"found\\":true,\\"window\\":{\\"title\\":\\"" & t & "\\",\\"app\\":\\"" & appName & "\\",\\"pid\\":" & pidVal & ",\\"x\\":" & (item 1 of pos) & ",\\"y\\":" & (item 2 of pos) & ",\\"width\\":" & (item 1 of sz) & ",\\"height\\":" & (item 2 of sz) & "}}"\n'
            '    on error\n'
            '      return "{\\"found\\":true,\\"window\\":{\\"title\\":\\"\\",\\"app\\":\\"" & appName & "\\",\\"pid\\":" & pidVal & ",\\"x\\":0,\\"y\\":0,\\"width\\":0,\\"height\\":0}}"\n'
            '    end try\n'
            '  on error\n'
            '    return "{\\"found\\":false}"\n'
            '  end try\n'
            'end tell'
        )
        try:
            proc = await asyncio.create_subprocess_exec(
                "osascript", "-e", script,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
            import json as _json
            return _json.loads(stdout.decode(errors="replace").strip())
        except Exception as e:
            logger.debug("[macos:get_active_window] %s", traceback.format_exc())
            return {"found": False, "error": str(e)}

    async def get_window_text(self, max_chars: int = 50000) -> dict:
        """
        Select all text in the focused window (Cmd+A, Cmd+C), read via pbpaste,
        restore the old clipboard with pbcopy, and return the text.
        """
        try:
            # Save old clipboard
            old_proc = await asyncio.create_subprocess_exec(
                "pbpaste",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            old_out, _ = await asyncio.wait_for(old_proc.communicate(), timeout=5)
            old_clip = old_out  # raw bytes

            # Select all + copy via osascript (uses Command key, not Control)
            select_script = (
                'tell application "System Events"\n'
                '    keystroke "a" using command down\n'
                '    delay 0.3\n'
                '    keystroke "c" using command down\n'
                'end tell'
            )
            proc = await asyncio.create_subprocess_exec(
                "osascript", "-e", select_script,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(proc.communicate(), timeout=10)
            await asyncio.sleep(0.5)

            # Read clipboard
            paste_proc = await asyncio.create_subprocess_exec(
                "pbpaste",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            paste_out, _ = await asyncio.wait_for(paste_proc.communicate(), timeout=5)
            text = paste_out.decode(errors="replace")

            # Restore old clipboard
            restore_proc = await asyncio.create_subprocess_exec(
                "pbcopy",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            restore_proc.stdin.write(old_clip)
            restore_proc.stdin.close()
            await asyncio.wait_for(restore_proc.wait(), timeout=5)

            truncated = len(text) > max_chars
            text = text[:max_chars] if truncated else text
            return {"text": text, "length": len(text), "truncated": truncated}
        except Exception as e:
            logger.debug("[macos:get_window_text] %s", traceback.format_exc())
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
