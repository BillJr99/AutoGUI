"""
backends/macos.py — Desktop backend for macOS.

Screenshot uses the system `screencapture` utility (always available).
Mouse/keyboard operations use pyautogui (base class).
Window listing and app launching use `osascript` / `open`, with a
Quartz.CGWindowListCopyWindowInfo fallback when AppleScript fails.

Element interaction: find_element walks the frontmost process's AX tree
via System Events (ported from the pi-extension's macOS backend), and
click_element performs the element's AXPress action directly through the
pyobjc ApplicationServices bindings (AXUIElementPerformAction) when they
are installed and the process is accessibility-trusted — falling back to
a coordinate click on the located rect otherwise.

Note: the Quartz / AX paths are mock-verified only in CI; manual
verification on real macOS hardware is required.
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


def _as_quote(s: str) -> str:
    """Escape a Python string for embedding in an AppleScript string literal."""
    return (s or "").replace("\\", "\\\\").replace('"', '\\"')


class MacOSBackend(DesktopBackend):

    def capabilities(self) -> dict:
        caps = super().capabilities()
        caps.update({
            "find_element": True,
            "get_window_tree": False,
            "activate_window": True,
            "get_active_window": True,
            "get_window_text": True,
            # True when pyobjc ApplicationServices is importable AND the
            # process holds Accessibility permission — the gate for the
            # AXUIElementPerformAction click path.
            "ax_actions": self._ax_actions_supported(),
        })
        return caps

    # ------------------------------------------------------------------
    # Accessibility (AX) support detection
    # ------------------------------------------------------------------

    @staticmethod
    def _ax_modules():
        """Return the pyobjc ApplicationServices module, or None when absent."""
        try:
            import ApplicationServices  # type: ignore
            return ApplicationServices
        except ImportError:
            return None

    def _ax_actions_supported(self) -> bool:
        """Capability gate for AXUIElementPerformAction-based clicking."""
        ax = self._ax_modules()
        if ax is None:
            return False
        try:
            return bool(ax.AXIsProcessTrusted())
        except Exception:
            return False

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

            img: Image.Image = Image.open(filename)
            if resize_width and img.width > resize_width:
                ratio = resize_width / img.width
                img = img.resize((resize_width, int(img.height * ratio)), Image.Resampling.LANCZOS)
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
            # AppleScript failed (System Events needs Accessibility
            # permission; window titles with embedded quotes can also break
            # the inline JSON).  Degrade to Quartz, which needs neither.
            fallback = await self._list_windows_quartz()
            if "error" not in fallback:
                return fallback
            return {"error": str(e)}

    async def _list_windows_quartz(self) -> dict:
        """Native fallback via Quartz.CGWindowListCopyWindowInfo (pyobjc).

        Same schema as the AppleScript path: ``{"windows": [{title, app,
        pid, x, y, width, height}], "count": int}``.
        """
        try:
            import Quartz  # type: ignore
        except ImportError:
            return {"error": "pyobjc Quartz not installed"}

        def _enum() -> list[dict]:
            options = (
                Quartz.kCGWindowListOptionOnScreenOnly
                | Quartz.kCGWindowListExcludeDesktopElements
            )
            infos = Quartz.CGWindowListCopyWindowInfo(
                options, Quartz.kCGNullWindowID
            ) or []
            windows: list[dict] = []
            for info in infos:
                try:
                    # Layer 0 = normal application windows; skip the menu
                    # bar, Dock, and other overlay layers.
                    if int(info.get("kCGWindowLayer") or 0) != 0:
                        continue
                    bounds = info.get("kCGWindowBounds") or {}
                    windows.append({
                        "title": str(info.get("kCGWindowName") or ""),
                        "app": str(info.get("kCGWindowOwnerName") or ""),
                        "pid": int(info.get("kCGWindowOwnerPID") or 0),
                        "x": int(bounds.get("X") or 0),
                        "y": int(bounds.get("Y") or 0),
                        "width": int(bounds.get("Width") or 0),
                        "height": int(bounds.get("Height") or 0),
                    })
                except Exception:
                    continue  # skip malformed entries
            return windows

        try:
            loop = asyncio.get_event_loop()
            windows = await loop.run_in_executor(None, _enum)
            return {"windows": windows, "count": len(windows), "method": "quartz"}
        except Exception as e:
            logger.debug("[macos:_list_windows_quartz] %s", traceback.format_exc())
            return {"error": str(e)}

    # ------------------------------------------------------------------
    # Accessibility element lookup + AXPress clicking
    # ------------------------------------------------------------------

    # AppleScript AX-tree walk, ported from the pi-extension's macOS
    # backend (pi-extension/src/backends/macos.ts).  System Events exposes
    # names/descriptions/roles plus position+size we can use as a rect.
    # collectMatches mutates |results| (AppleScript lists pass by
    # reference) and short-circuits once matchIdx hits are collected.
    _FIND_ELEMENT_HANDLERS = """\
on collectMatches(elem, target, role, results, matchIdx)
  try
    set elemRole to (role of elem) as string
  on error
    set elemRole to ""
  end try
  try
    set elemDesc to (description of elem) as string
  on error
    set elemDesc to ""
  end try
  try
    set elemName to (name of elem) as string
  on error
    set elemName to ""
  end try
  try
    set elemTitle to (title of elem) as string
  on error
    set elemTitle to ""
  end try
  set joined to (elemName & " " & elemDesc & " " & elemTitle)
  set haystack to my toLower(joined)
  set roleHay to my toLower(elemRole)
  set nameHit to (target is "" or haystack contains target)
  set roleHit to (role is "" or roleHay contains role)
  if nameHit and roleHit then
    try
      set p to position of elem
      set s to size of elem
      set safeName to my jsonEscape(elemName)
      set safeRole to my jsonEscape(elemRole)
      set end of results to "{\\"name\\":\\"" & safeName & "\\",\\"control_type\\":\\"" & safeRole & "\\",\\"rect\\":{\\"x\\":" & item 1 of p & ",\\"y\\":" & item 2 of p & ",\\"width\\":" & item 1 of s & ",\\"height\\":" & item 2 of s & "}}"
    on error
      -- Element matched but has no geometry; skip it.
    end try
  end if
  if (count of results) >= matchIdx then return
  try
    set kids to UI elements of elem
  on error
    return
  end try
  repeat with k in kids
    my collectMatches(k, target, role, results, matchIdx)
    if (count of results) >= matchIdx then exit repeat
  end repeat
end collectMatches

on toLower(s)
  set chars to {"A","B","C","D","E","F","G","H","I","J","K","L","M","N","O","P","Q","R","S","T","U","V","W","X","Y","Z"}
  set lowers to {"a","b","c","d","e","f","g","h","i","j","k","l","m","n","o","p","q","r","s","t","u","v","w","x","y","z"}
  set out to ""
  repeat with ch in s
    set found to false
    repeat with i from 1 to length of chars
      if (ch as string) is item i of chars then
        set out to out & item i of lowers
        set found to true
        exit repeat
      end if
    end repeat
    if not found then set out to out & (ch as string)
  end repeat
  return out
end toLower

on jsonEscape(s)
  set bslash to (ASCII character 92)
  set dquote to (ASCII character 34)
  set nl to (ASCII character 10)
  set cr to (ASCII character 13)
  set tabChar to (ASCII character 9)
  set hexChars to "0123456789abcdef"
  set out to ""
  repeat with ch in characters of s
    set c to ch as string
    if c is bslash then
      set out to out & bslash & bslash
    else if c is dquote then
      set out to out & bslash & dquote
    else if c is nl then
      set out to out & bslash & "n"
    else if c is cr then
      set out to out & bslash & "r"
    else if c is tabChar then
      set out to out & bslash & "t"
    else
      set code to id of c
      if code < 32 then
        set hi to (code div 16) + 1
        set lo to (code mod 16) + 1
        set out to out & bslash & "u00" & (character hi of hexChars) & (character lo of hexChars)
      else
        set out to out & c
      end if
    end if
  end repeat
  return out
end jsonEscape
"""

    async def find_element(
        self,
        name: str | None = None,
        control_type: str | None = None,
        window_title: str | None = None,
        index: int = 0,
    ) -> dict:
        """Find a UI element by walking the frontmost process's AX tree.

        Returns ``{"name", "control_type", "rect": {x, y, width, height}}``
        (the schema click_element expects) or ``{"error": str}``.
        """
        # OS Screen Observer first, mirroring the base-class behaviour.
        if self._screen_observer is not None and (name or control_type):
            oso = await super().find_element(
                name=name, control_type=control_type,
                window_title=window_title, index=index,
            )
            if isinstance(oso, dict) and "error" not in oso:
                return oso

        idx = max(0, int(index or 0)) + 1  # AppleScript is 1-indexed
        script = (
            self._FIND_ELEMENT_HANDLERS
            + f'\nset targetName to "{_as_quote(name or "")}"\n'
            f'set targetRole to "{_as_quote(control_type or "")}"\n'
            f'set winNeedle to "{_as_quote(window_title or "")}"\n'
            f"set matchIdx to {idx}\n"
            "set results to {}\n"
            'tell application "System Events"\n'
            "  set frontProc to first process whose frontmost is true\n"
            '  if winNeedle is "" then\n'
            "    set candidates to {window 1 of frontProc}\n"
            "  else\n"
            "    set candidates to (every window of frontProc whose name contains winNeedle)\n"
            "  end if\n"
            "  repeat with w in candidates\n"
            "    my collectMatches(w, my toLower(targetName), my toLower(targetRole), results, matchIdx)\n"
            "    if (count of results) >= matchIdx then exit repeat\n"
            "  end repeat\n"
            "end tell\n"
            "if (count of results) >= matchIdx then\n"
            "  return item matchIdx of results\n"
            "end if\n"
            'return "{\\"error\\":\\"No matching AX element found.\\"}"'
        )
        try:
            proc = await asyncio.create_subprocess_exec(
                "osascript", "-e", script,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
            if proc.returncode != 0:
                return {
                    "error": (
                        "macOS AX query failed. The terminal running AutoGUI "
                        "needs Accessibility permission (System Settings → "
                        "Privacy & Security → Accessibility). "
                        + stderr.decode(errors="replace").strip()[:400]
                    )
                }
            raw = stdout.decode(errors="replace").strip()
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                return {"error": f"AX helper returned non-JSON: {raw[:200]}"}
        except Exception as e:
            logger.debug("[macos:find_element] %s", traceback.format_exc())
            return {"error": str(e)}

    async def click_element(
        self,
        name: str,
        control_type: str | None = None,
        window_title: str | None = None,
        index: int = 0,
        button: str = "left",
        clicks: int = 1,
    ) -> dict:
        """Click a UI element, preferring the native AXPress action.

        When the pyobjc ApplicationServices bindings are available and the
        process is accessibility-trusted (capabilities()["ax_actions"]),
        performs AXUIElementPerformAction("AXPress") directly on the matched
        AX-tree element — DPI/coordinate-independent and immune to window
        moves.  Only plain single left-clicks map to AXPress; anything else
        (and any AX failure) falls back to the base find_element +
        coordinate-click path.
        """
        if not name:
            return {"error": "name is required"}
        if (
            self._ax_actions_supported()
            and (button or "left") == "left"
            and int(clicks or 1) == 1
        ):
            result = await self._ax_press(
                name=name,
                control_type=control_type,
                window_title=window_title,
                index=int(index or 0),
            )
            if result.get("success"):
                return result
            logger.debug(
                "[macos:click_element] AXPress failed (%s); falling back to "
                "coordinate click", result.get("error"),
            )
        return await super().click_element(
            name=name, control_type=control_type, window_title=window_title,
            index=index, button=button, clicks=clicks,
        )

    async def _ax_press(
        self,
        name: str,
        control_type: str | None,
        window_title: str | None,
        index: int,
    ) -> dict:
        """Locate an element in the AX tree and perform its AXPress action."""
        ax = self._ax_modules()
        if ax is None:
            return {"error": "pyobjc ApplicationServices not installed"}

        def _ax_attr(elem, attr):
            """Normalize pyobjc's (err, value) tuple / direct-value returns."""
            try:
                result = ax.AXUIElementCopyAttributeValue(elem, attr, None)
            except Exception:
                return None
            if isinstance(result, tuple):
                err, value = result
                return value if err == 0 else None
            return result

        def _resolve_pid() -> int | None:
            try:
                if window_title:
                    import Quartz  # type: ignore
                    infos = Quartz.CGWindowListCopyWindowInfo(
                        Quartz.kCGWindowListOptionOnScreenOnly
                        | Quartz.kCGWindowListExcludeDesktopElements,
                        Quartz.kCGNullWindowID,
                    ) or []
                    needle = window_title.lower()
                    for info in infos:
                        if needle in str(info.get("kCGWindowName") or "").lower():
                            return int(info.get("kCGWindowOwnerPID") or 0) or None
                import AppKit  # type: ignore
                front = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
                return int(front.processIdentifier()) if front is not None else None
            except Exception:
                return None

        def _do() -> dict:
            pid = _resolve_pid()
            if not pid:
                return {"error": "could not resolve target process for AX lookup"}
            app_elem = ax.AXUIElementCreateApplication(pid)
            needle = (name or "").lower()
            role_needle = (control_type or "").lower()

            # Breadth-first walk, bounded so a pathological tree terminates.
            queue = [app_elem]
            matches: list[tuple] = []
            visited = 0
            while queue and visited < 5000:
                elem = queue.pop(0)
                visited += 1
                role = str(_ax_attr(elem, "AXRole") or "")
                title = str(_ax_attr(elem, "AXTitle") or "")
                desc = str(_ax_attr(elem, "AXDescription") or "")
                haystack = f"{title} {desc}".lower()
                name_hit = not needle or needle in haystack
                role_hit = not role_needle or role_needle in role.lower()
                if name_hit and role_hit and (title or desc):
                    matches.append((elem, title or desc, role))
                    if len(matches) > index:
                        break
                children = _ax_attr(elem, "AXChildren") or []
                try:
                    queue.extend(list(children))
                except TypeError:
                    pass
            if len(matches) <= index:
                return {"error": "No matching AX element found."}

            elem, matched_name, matched_role = matches[index]
            err = ax.AXUIElementPerformAction(elem, "AXPress")
            code = err[0] if isinstance(err, tuple) else err
            if code not in (0, None):
                return {"error": f"AXUIElementPerformAction returned {code}"}
            return {
                "success": True,
                "method": "ax_press",
                "name": matched_name,
                "control_type": matched_role,
                "pid": pid,
            }

        try:
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(None, _do)
        except Exception as e:
            logger.debug("[macos:_ax_press] %s", traceback.format_exc())
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
                        match = w
                        break
                    if pid and w.get("pid") == pid:
                        match = w
                        break
                    if title and title.lower() in w.get("title", "").lower():
                        match = w
                        break
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
            restore_proc.stdin.write(old_clip)  # type: ignore[union-attr]  # stdin=PIPE above
            restore_proc.stdin.close()  # type: ignore[union-attr]
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
                # `open` hands off to LaunchServices — the app's real pid is
                # not observable from here, so pid is None by design.
                return {"success": True, "application": application, "args": args,
                        "pid": None, "method": "open -a"}

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
            return {"success": True, "application": application, "args": args,
                    "pid": direct.pid, "method": "direct"}
        except Exception as e:
            logger.debug("[macos:launch] %s", traceback.format_exc())
            return {"error": str(e)}
