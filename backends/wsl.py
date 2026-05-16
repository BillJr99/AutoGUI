"""
backends/wsl.py — Desktop backend for WSL (Windows Subsystem for Linux).

All display operations (screenshot, click, type, hotkey, scroll) are
implemented via PowerShell interop rather than pyautogui/X11, because:
  - pyautogui needs a working X11 socket (/tmp/.X11-unix/X0) which is often
    absent in WSL2 even when WSLg is enabled.
  - PowerShell gives us direct access to the full Windows API, including
    multi-monitor capture via Screen.AllScreens + CopyFromScreen.

Window listing and application launching also use powershell.exe.

Requirements
------------
  - WSL interop must be enabled: /etc/wsl.conf → [interop] enabled = true
  - powershell.exe must be on the PATH (standard on Windows 10/11).
  - For display operations, the Windows desktop must be accessible (not
    required for list_windows / launch which use COM/PowerShell only).
"""

import asyncio
import base64
import binascii
import io
import json
import logging
import traceback
from datetime import datetime
from pathlib import Path

from backends.base import DesktopBackend

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# SendKeys key-name → format string mapping
# ---------------------------------------------------------------------------

_SK_MODIFIERS: dict[str, str] = {
    "ctrl": "^", "control": "^",
    "alt": "%",
    "shift": "+",
}

_SK_SPECIAL: dict[str, str] = {
    "enter": "{ENTER}", "return": "{ENTER}",
    "tab": "{TAB}",
    "escape": "{ESC}", "esc": "{ESC}",
    "backspace": "{BACKSPACE}", "bs": "{BS}",
    "delete": "{DELETE}", "del": "{DEL}",
    "insert": "{INSERT}", "ins": "{INS}",
    "home": "{HOME}", "end": "{END}",
    "pageup": "{PGUP}", "pgup": "{PGUP}",
    "pagedown": "{PGDN}", "pgdn": "{PGDN}",
    "up": "{UP}", "down": "{DOWN}", "left": "{LEFT}", "right": "{RIGHT}",
    "space": " ",
}
for _i in range(1, 13):
    _SK_SPECIAL[f"f{_i}"] = "{F" + str(_i) + "}"


class WSLBackend(DesktopBackend):

    _WIN_FOCUS_TYPE = (
        "Add-Type -TypeDefinition @\"\n"
        "using System;\n"
        "using System.Runtime.InteropServices;\n"
        "public class WinFocus {\n"
        "    [DllImport(\"user32.dll\")] public static extern bool SetForegroundWindow(IntPtr hWnd);\n"
        "    [DllImport(\"user32.dll\")] public static extern bool ShowWindowAsync(IntPtr hWnd, int nCmdShow);\n"
        "    [DllImport(\"user32.dll\")] public static extern IntPtr GetForegroundWindow();\n"
        "    [DllImport(\"user32.dll\")] public static extern bool GetWindowRect(IntPtr hWnd, out RECT lpRect);\n"
        "    [StructLayout(LayoutKind.Sequential)]\n"
        "    public struct RECT { public int Left; public int Top; public int Right; public int Bottom; }\n"
        "}\n"
        "\"@ -Language CSharp\n"
    )

    def capabilities(self) -> dict:
        caps = super().capabilities()
        caps.update({"find_element": False, "get_window_tree": False, "activate_window": True, "get_active_window": True, "get_window_text": True})
        return caps

    # ------------------------------------------------------------------
    # PowerShell helper
    # ------------------------------------------------------------------

    async def _ps(self, script: str, timeout: int = 20) -> tuple[int, str, str]:
        """
        Run a PowerShell script via powershell.exe under WSL.

        Returns (returncode, stdout, stderr).
        """
        encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        try:
            proc = await asyncio.create_subprocess_exec(
                "powershell.exe",
                "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            return (
                proc.returncode or 0,
                stdout.decode("utf-8", errors="replace").strip(),
                stderr.decode("utf-8", errors="replace").strip(),
            )
        except FileNotFoundError:
            return (-1, "", "powershell.exe not found (WSL interop may be disabled)")
        except asyncio.TimeoutError:
            return (-2, "", f"PowerShell timed out after {timeout}s")

    # ------------------------------------------------------------------
    # Screenshot via PowerShell / GDI+
    # ------------------------------------------------------------------

    async def screenshot(
        self,
        region: dict | None = None,
        save_dir: str = "screenshots",
        resize_width: int = 1280,
    ) -> dict:
        import time as _time
        cache_key = f"full:{resize_width}" if region is None else None
        if cache_key and self._screenshot_cache and self._cache_ttl > 0:
            ts, key, cached = self._screenshot_cache
            if key == cache_key and (_time.monotonic() - ts) < self._cache_ttl:
                return dict(cached, cache_hit=True)

        if region:
            x = int(region.get("x", 0))
            y = int(region.get("y", 0))
            w = int(region.get("width", 800))
            h = int(region.get("height", 600))
            script = (
                "Add-Type -AssemblyName System.Windows.Forms\n"
                "Add-Type -AssemblyName System.Drawing\n"
                f"$bmp = New-Object System.Drawing.Bitmap({w},{h})\n"
                "$g = [System.Drawing.Graphics]::FromImage($bmp)\n"
                f"$g.CopyFromScreen({x},{y},0,0,[System.Drawing.Size]::new({w},{h}))\n"
                "$ms = New-Object System.IO.MemoryStream\n"
                "$bmp.Save($ms,[System.Drawing.Imaging.ImageFormat]::Png)\n"
                "[Convert]::ToBase64String($ms.ToArray())\n"
            )
        else:
            script = (
                "Add-Type -AssemblyName System.Windows.Forms\n"
                "Add-Type -AssemblyName System.Drawing\n"
                "$screens = [System.Windows.Forms.Screen]::AllScreens\n"
                "$left   = ($screens | Measure-Object -Property Bounds.Left   -Minimum).Minimum\n"
                "$top    = ($screens | Measure-Object -Property Bounds.Top    -Minimum).Minimum\n"
                "$right  = ($screens | Measure-Object -Property { $_.Bounds.Left + $_.Bounds.Width  } -Maximum).Maximum\n"
                "$bottom = ($screens | Measure-Object -Property { $_.Bounds.Top  + $_.Bounds.Height } -Maximum).Maximum\n"
                "$width  = $right  - $left\n"
                "$height = $bottom - $top\n"
                "$bmp = New-Object System.Drawing.Bitmap($width,$height)\n"
                "$g   = [System.Drawing.Graphics]::FromImage($bmp)\n"
                "$g.CopyFromScreen($left,$top,0,0,[System.Drawing.Size]::new($width,$height))\n"
                "$ms = New-Object System.IO.MemoryStream\n"
                "$bmp.Save($ms,[System.Drawing.Imaging.ImageFormat]::Png)\n"
                "[Convert]::ToBase64String($ms.ToArray())\n"
            )
        try:
            _, b64, err = await self._ps(script, timeout=30)
            if not b64 or err:
                raise RuntimeError(err or "no output from PowerShell screenshot")

            raw = base64.b64decode(b64)
            from PIL import Image
            img = Image.open(io.BytesIO(raw))

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
            b64out = base64.b64encode(buf.getvalue()).decode()

            result = {
                "path": str(filename),
                "width": img.width,
                "height": img.height,
                "base64_png": b64out,
            }
            if cache_key and self._cache_ttl > 0:
                import time as _t
                self._screenshot_cache = (_t.monotonic(), cache_key, dict(result))
            return result
        except binascii.Error:
            return {"error": "PowerShell returned non-base64 output for screenshot"}
        except Exception as e:
            logger.warning("[wsl:screenshot] %s", e)
            logger.debug("[wsl:screenshot] %s", traceback.format_exc())
            return {"error": str(e)}

    # ------------------------------------------------------------------
    # Input via SendKeys / PowerShell
    # ------------------------------------------------------------------

    async def click(
        self,
        x: int,
        y: int,
        button: str = "left",
        clicks: int = 1,
    ) -> dict:
        btn = (button or "left").lower()
        btn_down = {"left": "LEFTDOWN", "right": "RIGHTDOWN", "middle": "MIDDLEDOWN"}.get(btn, "LEFTDOWN")
        btn_up   = {"left": "LEFTUP",   "right": "RIGHTUP",   "middle": "MIDDLEUP"  }.get(btn, "LEFTUP")
        n = max(1, int(clicks))
        script = (
            "Add-Type -AssemblyName System.Windows.Forms\n"
            "[System.Windows.Forms.Cursor]::Position = "
            f"[System.Drawing.Point]::new({int(x)},{int(y)})\n"
            "Add-Type @\"\n"
            "using System; using System.Runtime.InteropServices;\n"
            "public class Mouse {\n"
            "  [DllImport(\"user32.dll\",SetLastError=true)] "
            "  public static extern void mouse_event(uint dwFlags,int dx,int dy,int dwData,int dwExtraInfo);\n"
            "}\"\n"
            f"for ($i=0; $i -lt {n}; $i++) {{\n"
            f"  [Mouse]::mouse_event(0x{_btn_flag(btn_down)},0,0,0,0)\n"
            f"  Start-Sleep -Milliseconds 30\n"
            f"  [Mouse]::mouse_event(0x{_btn_flag(btn_up)},0,0,0,0)\n"
            f"  Start-Sleep -Milliseconds 20\n"
            "}\n"
        )
        _, _, err = await self._ps(script, timeout=10)
        if err:
            return {"error": err}
        return {"success": True, "x": x, "y": y, "button": btn, "clicks": n, "method": "powershell"}

    async def type_text(self, text: str) -> dict:
        """
        Type text in WSL by setting the clipboard via PowerShell and sending Ctrl+V.

        This is far more reliable than trying to replay keystrokes character by
        character across the WSL/Windows boundary.
        """
        if not text:
            return {"success": True, "length": 0, "method": "noop"}

        log_text = (text[:60] + "…") if len(text) > 60 else text
        logger.info("[wsl:type_text] len=%d text=%r", len(text), log_text)

        # Encode text as UTF-8 base64 so arbitrary Unicode survives the
        # PowerShell command-line boundary cleanly.
        b64 = base64.b64encode(text.encode("utf-8")).decode("ascii")
        script = (
            "Add-Type -AssemblyName System.Windows.Forms\n"
            f"$b64 = '{b64}'\n"
            "$bytes = [Convert]::FromBase64String($b64)\n"
            "$text  = [System.Text.Encoding]::UTF8.GetString($bytes)\n"
            "$old   = [System.Windows.Forms.Clipboard]::GetText()\n"
            "[System.Windows.Forms.Clipboard]::SetText($text)\n"
            "Start-Sleep -Milliseconds 50\n"
            "[System.Windows.Forms.SendKeys]::SendWait('^v')\n"
            "Start-Sleep -Milliseconds 100\n"
            "try { [System.Windows.Forms.Clipboard]::SetText($old) } catch {}\n"
        )
        _, _, err = await self._ps(script, timeout=15)
        if err:
            # Fallback: keystroke replay for ASCII-only text.
            logger.warning("[wsl:type_text] clipboard paste failed: %s", err)
            return await self._type_via_sendkeys(text)
        return {"success": True, "length": len(text), "method": "clipboard_paste"}

    async def _type_via_sendkeys(self, text: str) -> dict:
        safe = ""
        for ch in text:
            if ch in "+^%~(){}[]":
                safe += "{" + ch + "}"
            elif ord(ch) > 127:
                safe += "?"  # best-effort for non-ASCII
            else:
                safe += ch
        script = (
            "Add-Type -AssemblyName System.Windows.Forms\n"
            f"[System.Windows.Forms.SendKeys]::SendWait('{safe}')\n"
        )
        _, _, err = await self._ps(script, timeout=15)
        if err:
            return {"error": err}
        return {"success": True, "length": len(text), "method": "sendkeys"}

    async def hotkey(self, keys: list[str]) -> dict:
        """
        Press a keyboard chord via SendKeys.

        Modifier key names (ctrl, alt, shift) become SendKeys prefix characters.
        Other key names are looked up in the special-key table or treated as
        literal characters.

        Multiple modifiers are combined by nesting: ctrl+alt+del → ^%{DEL}.
        """
        if not keys:
            return {"error": "No keys specified"}

        keys_lower = [str(k).lower().strip() for k in keys]
        mods = ""
        body = ""
        non_mods = []
        for k in keys_lower:
            if k in _SK_MODIFIERS:
                mods += _SK_MODIFIERS[k]
            elif k in _SK_SPECIAL:
                non_mods.append(_SK_SPECIAL[k])
            elif len(k) == 1:
                non_mods.append(k)
            else:
                non_mods.append("{" + k.upper() + "}")

        if not non_mods:
            return {"error": f"No non-modifier keys in chord: {keys}"}

        body = "".join(non_mods)
        sendkeys_str = mods + body if not mods else f"{mods}({body})"

        script = (
            "Add-Type -AssemblyName System.Windows.Forms\n"
            f"[System.Windows.Forms.SendKeys]::SendWait('{sendkeys_str}')\n"
        )
        _, _, err = await self._ps(script, timeout=10)
        if err:
            return {"error": err}
        return {"success": True, "keys": list(keys), "chord": sendkeys_str, "method": "sendkeys"}

    async def scroll(
        self,
        x: int,
        y: int,
        clicks: int = 3,
        direction: str = "down",
    ) -> dict:
        delta = -120 * max(1, int(clicks)) if direction == "down" else 120 * max(1, int(clicks))
        script = (
            "Add-Type -AssemblyName System.Windows.Forms\n"
            "[System.Windows.Forms.Cursor]::Position = "
            f"[System.Drawing.Point]::new({int(x)},{int(y)})\n"
            "Add-Type @\"\n"
            "using System; using System.Runtime.InteropServices;\n"
            "public class Mouse2 {\n"
            "  [DllImport(\"user32.dll\",SetLastError=true)] "
            "  public static extern void mouse_event(uint dwFlags,int dx,int dy,int dwData,int dwExtraInfo);\n"
            "}\"\n"
            f"[Mouse2]::mouse_event(0x0800,0,0,{delta},0)\n"
        )
        _, _, err = await self._ps(script, timeout=10)
        if err:
            return {"error": err}
        return {"success": True, "direction": direction, "clicks": clicks, "method": "powershell"}

    # ------------------------------------------------------------------
    # Window management
    # ------------------------------------------------------------------

    async def list_windows(self) -> dict:
        """List visible windows; tries OS Screen Observer first, falls back to PowerShell."""
        if self._screen_observer is not None:
            result = await self._screen_observer.get_windows()
            if result is not None:
                return result
            logger.warning("[wsl:list_windows] OS Screen Observer unavailable; falling back to PowerShell")
        script = (
            self._WIN_FOCUS_TYPE +
            "$ErrorActionPreference = 'SilentlyContinue'\n"
            "$active = [WinFocus]::GetForegroundWindow()\n"
            "Get-Process | Where-Object { $_.MainWindowTitle -ne '' } | ForEach-Object {\n"
            "    $r = New-Object WinFocus+RECT\n"
            "    [WinFocus]::GetWindowRect($_.MainWindowHandle, [ref]$r) | Out-Null\n"
            "    [PSCustomObject]@{\n"
            "        id     = $_.MainWindowHandle.ToString()\n"
            "        title  = $_.MainWindowTitle\n"
            "        app    = $_.ProcessName\n"
            "        pid    = [int]$_.Id\n"
            "        active = ($_.MainWindowHandle -eq $active)\n"
            "        x      = [int]$r.Left\n"
            "        y      = [int]$r.Top\n"
            "        width  = [int]($r.Right  - $r.Left)\n"
            "        height = [int]($r.Bottom - $r.Top)\n"
            "    }\n"
            "} | ConvertTo-Json -Compress"
        )
        try:
            _, stdout, _ = await self._ps(script, timeout=15)
            if not stdout:
                return {"windows": [], "count": 0}
            data = json.loads(stdout)
            windows = data if isinstance(data, list) else [data]
            return {"windows": windows, "count": len(windows)}
        except Exception as e:
            logger.debug("[wsl:list_windows] %s", traceback.format_exc())
            return {"error": str(e)}

    async def launch(
        self,
        application: str,
        args: list[str] | None = None,
    ) -> dict:
        try:
            import json as _json
            if isinstance(args, str):
                try:
                    args = _json.loads(args)
                    if not isinstance(args, list):
                        args = [str(args)] if args else []
                except (ValueError, TypeError):
                    args = [args] if args.strip() else []
            args = [str(a) for a in (args or [])]

            parts_b64 = base64.b64encode(
                json.dumps([application] + args).encode("utf-8")
            ).decode("ascii")
            script = (
                "$ErrorActionPreference = 'SilentlyContinue'\n"
                f"$parts = [System.Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{parts_b64}')) | ConvertFrom-Json\n"
                "$exe  = $parts[0]\n"
                "$argv = if ($parts.Count -gt 1) { $parts[1..($parts.Count-1)] } else { @() }\n"
                "Start-Process -FilePath $exe -ArgumentList $argv\n"
            )
            _, _, err = await self._ps(script, timeout=10)
            if err:
                raise RuntimeError(err)
            return {"success": True, "application": application, "args": args}
        except Exception as e:
            logger.debug("[wsl:launch] %s", traceback.format_exc())
            return {"error": str(e)}

    async def activate_window(
        self,
        title: str = "",
        pid: int = 0,
        app: str = "",
        window_id: str = "",
    ) -> dict:
        if not any([title, pid, app, window_id]):
            return {"error": "Provide at least one of: title, pid, app, window_id"}

        target = {"id": window_id, "pid": pid, "title": title, "app": app}
        target_b64 = base64.b64encode(json.dumps(target).encode("utf-8")).decode("ascii")

        script = (
            self._WIN_FOCUS_TYPE +
            "$ErrorActionPreference = 'SilentlyContinue'\n"
            f"$t = [System.Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{target_b64}')) | ConvertFrom-Json\n"
            "$procs = @(Get-Process | Where-Object { $_.MainWindowTitle -ne '' -and $_.MainWindowHandle -ne [IntPtr]::Zero })\n"
            "if     ($t.id    -and $t.id    -ne '') { $p = $procs | Where-Object { $_.MainWindowHandle.ToString() -eq $t.id } | Select-Object -First 1 }\n"
            "elseif ($t.pid   -and $t.pid   -gt 0)  { $p = $procs | Where-Object { $_.Id -eq [int]$t.pid } | Select-Object -First 1 }\n"
            "elseif ($t.title -and $t.title -ne '') { $p = $procs | Where-Object { $_.MainWindowTitle.ToLowerInvariant().Contains($t.title.ToLowerInvariant()) } | Select-Object -First 1 }\n"
            "elseif ($t.app   -and $t.app   -ne '') { $p = $procs | Where-Object { $_.ProcessName.ToLowerInvariant().Contains($t.app.ToLowerInvariant()) } | Select-Object -First 1 }\n"
            "if (-not $p) { [PSCustomObject]@{ found = $false } | ConvertTo-Json -Compress; exit 1 }\n"
            "$h = $p.MainWindowHandle\n"
            "[WinFocus]::ShowWindowAsync($h, 9) | Out-Null\n"
            "Start-Sleep -Milliseconds 100\n"
            "[WinFocus]::SetForegroundWindow($h) | Out-Null\n"
            "Start-Sleep -Milliseconds 150\n"
            "$fg = [WinFocus]::GetForegroundWindow()\n"
            "$r = New-Object WinFocus+RECT\n"
            "[WinFocus]::GetWindowRect($h, [ref]$r) | Out-Null\n"
            "[PSCustomObject]@{\n"
            "    found  = $true\n"
            "    active = ($fg -eq $h)\n"
            "    id     = $h.ToString()\n"
            "    title  = $p.MainWindowTitle\n"
            "    app    = $p.ProcessName\n"
            "    pid    = [int]$p.Id\n"
            "    x      = [int]$r.Left\n"
            "    y      = [int]$r.Top\n"
            "    width  = [int]($r.Right - $r.Left)\n"
            "    height = [int]($r.Bottom - $r.Top)\n"
            "} | ConvertTo-Json -Compress\n"
        )
        try:
            _, stdout, _ = await self._ps(script, timeout=10)
            if not stdout:
                return {"error": "activate_window got no output"}
            result = json.loads(stdout)
            if not result.get("found"):
                return {"error": "No window found matching the given criteria"}
            if not result.get("active"):
                cx = result.get("x", 0) + result.get("width", 200) // 2
                cy = result.get("y", 0) + 15
                click_r = await self.click(cx, cy)
                if not click_r.get("error"):
                    result["active"] = True
                    result["method"] = "click_fallback"
            return {"success": True, **result}
        except json.JSONDecodeError:
            return {"error": f"activate_window: unexpected output: {stdout!r}"}
        except Exception as e:
            logger.debug("[wsl:activate_window] %s", traceback.format_exc())
            return {"error": str(e)}

    async def get_active_window(self) -> dict:
        script = (
            self._WIN_FOCUS_TYPE +
            "$ErrorActionPreference = 'SilentlyContinue'\n"
            "$h = [WinFocus]::GetForegroundWindow()\n"
            "if ($h -eq [IntPtr]::Zero) { [PSCustomObject]@{ found = $false } | ConvertTo-Json -Compress; exit 0 }\n"
            "$p = Get-Process | Where-Object { $_.MainWindowHandle -eq $h } | Select-Object -First 1\n"
            "$r = New-Object WinFocus+RECT\n"
            "[WinFocus]::GetWindowRect($h, [ref]$r) | Out-Null\n"
            "[PSCustomObject]@{\n"
            "    found  = $true\n"
            "    window = [PSCustomObject]@{\n"
            "        id     = $h.ToString()\n"
            "        title  = if ($p) { $p.MainWindowTitle } else { '' }\n"
            "        app    = if ($p) { $p.ProcessName } else { '' }\n"
            "        pid    = if ($p) { [int]$p.Id } else { 0 }\n"
            "        x      = [int]$r.Left\n"
            "        y      = [int]$r.Top\n"
            "        width  = [int]($r.Right - $r.Left)\n"
            "        height = [int]($r.Bottom - $r.Top)\n"
            "    }\n"
            "} | ConvertTo-Json -Compress -Depth 3\n"
        )
        try:
            _, stdout, _ = await self._ps(script, timeout=10)
            if not stdout:
                return {"found": False}
            return json.loads(stdout)
        except Exception as e:
            logger.debug("[wsl:get_active_window] %s", traceback.format_exc())
            return {"found": False, "error": str(e)}

    async def get_window_text(self, max_chars: int = 50000) -> dict:
        script = (
            "$ErrorActionPreference = 'SilentlyContinue'\n"
            "Add-Type -AssemblyName System.Windows.Forms\n"
            "$old = [System.Windows.Forms.Clipboard]::GetText()\n"
            "[System.Windows.Forms.SendKeys]::SendWait('^a')\n"
            "Start-Sleep -Milliseconds 300\n"
            "[System.Windows.Forms.SendKeys]::SendWait('^c')\n"
            "Start-Sleep -Milliseconds 500\n"
            "$text = [System.Windows.Forms.Clipboard]::GetText()\n"
            "try { [System.Windows.Forms.Clipboard]::SetText($old) } catch {}\n"
            f"$maxLen = {int(max_chars)}\n"
            "$truncated = $false\n"
            "if ($null -eq $text) { $text = '' }\n"
            "if ($text.Length -gt $maxLen) { $text = $text.Substring(0,$maxLen); $truncated = $true }\n"
            "[PSCustomObject]@{ text = $text; length = $text.Length; truncated = $truncated } | ConvertTo-Json -Compress\n"
        )
        try:
            _, stdout, stderr = await self._ps(script, timeout=15)
            if not stdout:
                return {"error": f"get_window_text got no output: {stderr}"}
            return json.loads(stdout)
        except json.JSONDecodeError:
            return {"text": stdout, "length": len(stdout), "truncated": False}
        except Exception as e:
            logger.debug("[wsl:get_window_text] %s", traceback.format_exc())
            return {"error": str(e)}

    # ------------------------------------------------------------------
    # Mouse cursor query
    # ------------------------------------------------------------------

    async def get_cursor_pos(self) -> dict:
        script = (
            "Add-Type -AssemblyName System.Windows.Forms\n"
            "$p = [System.Windows.Forms.Cursor]::Position\n"
            "[PSCustomObject]@{ x = $p.X; y = $p.Y } | ConvertTo-Json -Compress\n"
        )
        try:
            _, stdout, _ = await self._ps(script, timeout=5)
            if not stdout:
                return {"error": "get_cursor_pos: no output"}
            data = json.loads(stdout)
            return {"x": data["x"], "y": data["y"]}
        except Exception as e:
            return {"error": str(e)}


# ---------------------------------------------------------------------------
# Helper: mouse_event flag hex strings
# ---------------------------------------------------------------------------

def _btn_flag(name: str) -> str:
    """Return the hex value (without 0x) of a mouse_event dwFlags constant."""
    flags = {
        "LEFTDOWN":   "0002",
        "LEFTUP":     "0004",
        "RIGHTDOWN":  "0008",
        "RIGHTUP":    "0010",
        "MIDDLEDOWN": "0020",
        "MIDDLEUP":   "0040",
    }
    return flags.get(name, "0002")
