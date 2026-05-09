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
    **{f"f{i}": "{F" + str(i) + "}" for i in range(1, 13)},
}
# Characters that have special meaning in SendKeys and must be braced.
_SK_ESCAPE = set("+^%~{}[]() ")


def _to_sendkeys(keys: list[str]) -> str:
    """
    Convert a list like ['ctrl','alt','t'] to a SendKeys string like '^%(t)'.

    Rules (matching .NET SendKeys format):
    - Single modifier + single key: '^c', '%{F4}', '+{TAB}' (no parens)
    - Two or more modifiers, or two or more body keys: wrap body in parens
      e.g. ['ctrl','alt','t'] → '^%(t)'
           ['ctrl','shift','n'] → '^+(n)'
    """
    mods = ""
    body_parts: list[str] = []
    for k in keys:
        kl = k.lower()
        if kl in _SK_MODIFIERS:
            mods += _SK_MODIFIERS[kl]
        elif kl in _SK_SPECIAL:
            body_parts.append(_SK_SPECIAL[kl])
        elif len(k) == 1:
            body_parts.append("{" + k + "}" if k in _SK_ESCAPE else k)
        else:
            body_parts.append("{" + k.upper() + "}")
    body = "".join(body_parts)
    if not body:
        return mods
    if not mods:
        return body
    # Parentheses are needed when multiple modifiers or multiple body keys are
    # present, to ensure all modifiers apply to the entire group.
    if len(mods) > 1 or len(body_parts) > 1:
        return mods + "(" + body + ")"
    return mods + body


# ---------------------------------------------------------------------------
# Backend
# ---------------------------------------------------------------------------

class WSLBackend(DesktopBackend):

    def capabilities(self) -> dict:
        return {"find_element": False, "get_window_tree": False}

    # ------------------------------------------------------------------
    # PowerShell helper
    # ------------------------------------------------------------------

    async def _ps(self, script: str, timeout: int = 20) -> tuple[int, str, str]:
        """
        Execute *script* via powershell.exe -EncodedCommand.
        Returns (returncode, stdout, stderr).

        returncode is the PowerShell process exit code (0 = success), or a
        sentinel negative value on infrastructure error:
          -1  powershell.exe not found (WSL interop disabled)
          -2  process timed out

        Using -EncodedCommand (base64 UTF-16-LE) avoids all shell-quoting
        issues: the script is passed as raw bytes, not parsed by cmd.exe.
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
            return (-1, "", "powershell.exe not found — WSL interop may be disabled")
        except asyncio.TimeoutError:
            return (-2, "", f"PowerShell timed out after {timeout}s")

    # ------------------------------------------------------------------
    # Screenshot — all monitors via CopyFromScreen
    # ------------------------------------------------------------------

    async def screenshot(
        self,
        region: dict | None = None,
        save_dir: str = "screenshots",
        resize_width: int = 1280,
    ) -> dict:
        """
        Capture a screenshot using PowerShell + System.Drawing.

        Without a region, captures ALL monitors as a single combined image.
        The first line of stdout is metadata: "monitors=N w=W h=H".
        The remaining lines are the base64-encoded PNG.
        """
        try:
            from PIL import Image

            if region:
                script = (
                    "Add-Type -AssemblyName System.Drawing\n"
                    "$bmp = New-Object System.Drawing.Bitmap __W__, __H__\n"
                    "$g   = [System.Drawing.Graphics]::FromImage($bmp)\n"
                    "$g.CopyFromScreen(__X__, __Y__, 0, 0, $bmp.Size)\n"
                    "$g.Dispose()\n"
                    "$ms  = New-Object System.IO.MemoryStream\n"
                    "$bmp.Save($ms, [System.Drawing.Imaging.ImageFormat]::Png)\n"
                    "$bmp.Dispose()\n"
                    "Write-Host 'monitors=1 w=__W__ h=__H__'\n"
                    "[Convert]::ToBase64String($ms.ToArray())"
                ).replace("__X__", str(region["x"])) \
                 .replace("__Y__", str(region["y"])) \
                 .replace("__W__", str(region["width"])) \
                 .replace("__H__", str(region["height"]))
            else:
                script = (
                    "Add-Type -AssemblyName System.Windows.Forms\n"
                    "Add-Type -AssemblyName System.Drawing\n"
                    "$scr    = [System.Windows.Forms.Screen]::AllScreens\n"
                    "$left   = ($scr | % { $_.Bounds.Left   } | Measure-Object -Min).Minimum\n"
                    "$top    = ($scr | % { $_.Bounds.Top    } | Measure-Object -Min).Minimum\n"
                    "$right  = ($scr | % { $_.Bounds.Right  } | Measure-Object -Max).Maximum\n"
                    "$bottom = ($scr | % { $_.Bounds.Bottom } | Measure-Object -Max).Maximum\n"
                    "$w      = $right - $left\n"
                    "$h      = $bottom - $top\n"
                    "$n      = $scr.Count\n"
                    "$bmp    = New-Object System.Drawing.Bitmap $w, $h\n"
                    "$g      = [System.Drawing.Graphics]::FromImage($bmp)\n"
                    "$g.CopyFromScreen($left, $top, 0, 0, (New-Object System.Drawing.Size $w, $h))\n"
                    "$g.Dispose()\n"
                    "$ms     = New-Object System.IO.MemoryStream\n"
                    "$bmp.Save($ms, [System.Drawing.Imaging.ImageFormat]::Png)\n"
                    "$bmp.Dispose()\n"
                    "Write-Host \"monitors=$n w=$w h=$h\"\n"
                    "[Convert]::ToBase64String($ms.ToArray())"
                )

            returncode, stdout, stderr = await self._ps(script, timeout=30)
            if returncode != 0 or not stdout:
                return {"error": f"Screenshot failed (code {returncode}). stderr={stderr!r}"}

            lines = stdout.splitlines()
            meta_line = lines[0] if lines else ""
            b64_data  = "".join(lines[1:]) if len(lines) > 1 else stdout

            # Parse metadata if present
            monitors = 1
            for part in meta_line.split():
                if part.startswith("monitors="):
                    try:
                        monitors = int(part.split("=")[1])
                    except ValueError:
                        pass

            img_bytes = base64.b64decode(b64_data)
            img = Image.open(io.BytesIO(img_bytes))

            if resize_width and img.width > resize_width:
                ratio = resize_width / img.width
                img = img.resize(
                    (resize_width, int(img.height * ratio)), Image.LANCZOS
                )

            save_path = Path(save_dir)
            save_path.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = save_path / f"screenshot_{ts}.png"
            img.save(str(filename))

            buf = io.BytesIO()
            img.save(buf, format="PNG")
            out_b64 = base64.b64encode(buf.getvalue()).decode()

            return {
                "path": str(filename),
                "width": img.width,
                "height": img.height,
                "monitors": monitors,
                "base64_png": out_b64,
            }

        except Exception as e:
            logger.debug("[wsl:screenshot] %s", traceback.format_exc())
            return {"error": str(e)}

    # ------------------------------------------------------------------
    # Mouse operations — user32.dll via PowerShell
    # ------------------------------------------------------------------

    # Shared C# type definition for mouse operations (embedded in scripts).
    _MOUSE_TYPE = (
        "Add-Type -TypeDefinition @\"\n"
        "using System;\n"
        "using System.Runtime.InteropServices;\n"
        "public class WslMouse {\n"
        "    [DllImport(\"user32.dll\")] public static extern bool SetCursorPos(int x, int y);\n"
        "    [DllImport(\"user32.dll\")] public static extern void mouse_event("
        "uint flags, int dx, int dy, uint data, UIntPtr extra);\n"
        "}\n"
        "\"@ -Language CSharp\n"
    )

    async def click(
        self,
        x: int,
        y: int,
        button: str = "left",
        clicks: int = 1,
    ) -> dict:
        """Click at (x, y) using user32.dll mouse_event via PowerShell."""
        try:
            down_flag, up_flag = {
                "left":   ("0x0002", "0x0004"),
                "right":  ("0x0008", "0x0010"),
                "middle": ("0x0020", "0x0040"),
            }.get(button, ("0x0002", "0x0004"))

            script = (
                self._MOUSE_TYPE +
                "[WslMouse]::SetCursorPos(__X__, __Y__)\n"
                "Start-Sleep -Milliseconds 60\n"
                "for ($i = 0; $i -lt __CLICKS__; $i++) {\n"
                "    [WslMouse]::mouse_event(__DOWN__, 0, 0, 0, [UIntPtr]::Zero)\n"
                "    Start-Sleep -Milliseconds 20\n"
                "    [WslMouse]::mouse_event(__UP__, 0, 0, 0, [UIntPtr]::Zero)\n"
                "    if ($i -lt (__CLICKS__ - 1)) { Start-Sleep -Milliseconds 80 }\n"
                "}"
            ).replace("__X__",      str(int(x))) \
             .replace("__Y__",      str(int(y))) \
             .replace("__CLICKS__", str(int(clicks))) \
             .replace("__DOWN__",   down_flag) \
             .replace("__UP__",     up_flag)

            returncode, _, stderr = await self._ps(script, timeout=10)
            if returncode != 0:
                return {"error": f"click failed (code {returncode}): {stderr}"}
            if stderr:
                logger.debug("[wsl:click] stderr (non-fatal): %s", stderr)
            return {"success": True, "x": x, "y": y, "button": button, "clicks": clicks}
        except Exception as e:
            logger.debug("[wsl:click] %s", traceback.format_exc())
            return {"error": str(e)}

    # Shared C# type with GetCursorPos, SetCursorPos, and mouse_event in one class.
    _MOVE_TYPE = (
        "Add-Type -TypeDefinition @\"\n"
        "using System;\n"
        "using System.Runtime.InteropServices;\n"
        "public class WslMM {\n"
        "    [DllImport(\"user32.dll\")] public static extern bool GetCursorPos(out POINT p);\n"
        "    [DllImport(\"user32.dll\")] public static extern bool SetCursorPos(int x, int y);\n"
        "    [DllImport(\"user32.dll\")] public static extern void mouse_event("
        "uint f, int x, int y, uint d, UIntPtr e);\n"
        "    [StructLayout(LayoutKind.Sequential)]\n"
        "    public struct POINT { public int X; public int Y; }\n"
        "}\n"
        "\"@ -Language CSharp\n"
    )

    async def get_cursor_pos(self) -> dict:
        script = (
            self._MOVE_TYPE +
            "$p = New-Object WslMM+POINT\n"
            "[WslMM]::GetCursorPos([ref]$p) | Out-Null\n"
            "Write-Output \"$($p.X) $($p.Y)\""
        )
        try:
            returncode, stdout, stderr = await self._ps(script, timeout=10)
            if returncode != 0:
                return {"error": f"GetCursorPos failed (code {returncode}): {stderr}"}
            parts = stdout.strip().split()
            if len(parts) >= 2:
                return {"x": int(parts[0]), "y": int(parts[1])}
            return {"error": f"unexpected output: {stdout!r}"}
        except Exception as e:
            logger.debug("[wsl:get_cursor_pos] %s", traceback.format_exc())
            return {"error": str(e)}

    async def mouse_move(self, dx: int = 0, dy: int = 0, click: bool = False) -> dict:
        click_code = ""
        if click:
            click_code = (
                "Start-Sleep -Milliseconds 60\n"
                "[WslMM]::mouse_event(0x0002, 0, 0, 0, [UIntPtr]::Zero) | Out-Null\n"
                "Start-Sleep -Milliseconds 20\n"
                "[WslMM]::mouse_event(0x0004, 0, 0, 0, [UIntPtr]::Zero) | Out-Null\n"
            )
        script = (
            self._MOVE_TYPE +
            "$p = New-Object WslMM+POINT\n"
            "[WslMM]::GetCursorPos([ref]$p) | Out-Null\n"
            f"$nx = [Math]::Max(1, $p.X + {int(dx)})\n"
            f"$ny = [Math]::Max(1, $p.Y + {int(dy)})\n"
            "[WslMM]::SetCursorPos($nx, $ny) | Out-Null\n"
            + click_code +
            "Write-Output \"$($p.X) $($p.Y) $nx $ny\""
        )
        try:
            returncode, stdout, stderr = await self._ps(script, timeout=10)
            if returncode != 0:
                return {"error": f"mouse_move failed (code {returncode}): {stderr}"}
            if stderr:
                logger.debug("[wsl:mouse_move] stderr (non-fatal): %s", stderr)
            parts = stdout.strip().split()
            if len(parts) >= 4:
                from_x, from_y, to_x, to_y = int(parts[0]), int(parts[1]), int(parts[2]), int(parts[3])
            else:
                from_x = from_y = to_x = to_y = 0
            return {
                "success": True,
                "from": {"x": from_x, "y": from_y},
                "to": {"x": to_x, "y": to_y},
                "clicked": click,
            }
        except Exception as e:
            logger.debug("[wsl:mouse_move] %s", traceback.format_exc())
            return {"error": str(e)}

    async def scroll(
        self,
        x: int,
        y: int,
        clicks: int = 3,
        direction: str = "down",
    ) -> dict:
        """Scroll via MOUSEEVENTF_WHEEL using PowerShell."""
        try:
            # WHEEL_DELTA = 120 per notch; positive = up, negative = down.
            delta_signed = clicks * 120 if direction == "up" else -(clicks * 120)
            # Cast to uint32 two's-complement for the PowerShell uint parameter.
            delta_uint = delta_signed & 0xFFFFFFFF

            script = (
                self._MOUSE_TYPE +
                "[WslMouse]::SetCursorPos(__X__, __Y__)\n"
                "Start-Sleep -Milliseconds 30\n"
                "[WslMouse]::mouse_event(0x0800, 0, 0, __DELTA__, [UIntPtr]::Zero)"
            ).replace("__X__",     str(int(x))) \
             .replace("__Y__",     str(int(y))) \
             .replace("__DELTA__", str(delta_uint))

            returncode, _, stderr = await self._ps(script, timeout=10)
            if returncode != 0:
                return {"error": f"scroll failed (code {returncode}): {stderr}"}
            if stderr:
                logger.debug("[wsl:scroll] stderr (non-fatal): %s", stderr)
            return {"success": True, "direction": direction, "clicks": clicks}
        except Exception as e:
            logger.debug("[wsl:scroll] %s", traceback.format_exc())
            return {"error": str(e)}

    # ------------------------------------------------------------------
    # Keyboard operations — clipboard paste + SendKeys via PowerShell
    # ------------------------------------------------------------------

    async def type_text(self, text: str) -> dict:
        """
        Type text by writing to the Windows clipboard then sending Ctrl+V.
        Text is base64-encoded before embedding in the script to avoid
        any quoting issues with special characters.
        """
        try:
            text_b64 = base64.b64encode(text.encode("utf-8")).decode("ascii")
            script = (
                "$b = [Convert]::FromBase64String('__B64__')\n"
                "$t = [System.Text.Encoding]::UTF8.GetString($b)\n"
                "Set-Clipboard -Value $t\n"
                "Start-Sleep -Milliseconds 100\n"
                "Add-Type -AssemblyName System.Windows.Forms\n"
                "[System.Windows.Forms.SendKeys]::SendWait('^v')"
            ).replace("__B64__", text_b64)

            returncode, _, stderr = await self._ps(script, timeout=10)
            if returncode != 0:
                return {"error": f"type_text failed (code {returncode}): {stderr}"}
            if stderr:
                logger.debug("[wsl:type_text] stderr (non-fatal): %s", stderr)
            return {"success": True, "length": len(text)}
        except Exception as e:
            logger.debug("[wsl:type_text] %s", traceback.format_exc())
            return {"error": str(e)}

    async def hotkey(self, keys: list[str]) -> dict:
        """Send a keyboard shortcut via SendKeys."""
        try:
            sendkeys_str = _to_sendkeys(keys)
            if not sendkeys_str:
                return {"error": f"Could not map keys to SendKeys: {keys}"}

            # Escape single-quotes in the SendKeys string before embedding.
            safe = sendkeys_str.replace("'", "''")
            script = (
                "Add-Type -AssemblyName System.Windows.Forms\n"
                "[System.Windows.Forms.SendKeys]::SendWait('__SK__')"
            ).replace("__SK__", safe)

            returncode, _, stderr = await self._ps(script, timeout=10)
            if returncode != 0:
                return {"error": f"hotkey failed (code {returncode}): {stderr}"}
            if stderr:
                logger.debug("[wsl:hotkey] stderr (non-fatal): %s", stderr)
            return {"success": True, "keys": keys, "sendkeys": sendkeys_str}
        except Exception as e:
            logger.debug("[wsl:hotkey] %s", traceback.format_exc())
            return {"error": str(e)}

    # ------------------------------------------------------------------
    # Window management
    # ------------------------------------------------------------------

    async def list_windows(self) -> dict:
        """List visible Windows windows with titles, pids, and bounding boxes."""
        script = (
            "$ErrorActionPreference = 'SilentlyContinue'\n"
            "Add-Type -TypeDefinition @\"\n"
            "using System;\n"
            "using System.Runtime.InteropServices;\n"
            "public class WinBounds {\n"
            "    [DllImport(\"user32.dll\")]\n"
            "    public static extern bool GetWindowRect(IntPtr hWnd, out RECT r);\n"
            "    [StructLayout(LayoutKind.Sequential)]\n"
            "    public struct RECT { public int Left; public int Top; public int Right; public int Bottom; }\n"
            "}\n"
            "\"@\n"
            "Get-Process | Where-Object { $_.MainWindowTitle -ne '' } | ForEach-Object {\n"
            "    $r = New-Object WinBounds+RECT\n"
            "    [WinBounds]::GetWindowRect($_.MainWindowHandle, [ref]$r) | Out-Null\n"
            "    [PSCustomObject]@{\n"
            "        title  = $_.MainWindowTitle\n"
            "        pid    = [int]$_.Id\n"
            "        x      = [int]$r.Left\n"
            "        y      = [int]$r.Top\n"
            "        width  = [int]($r.Right  - $r.Left)\n"
            "        height = [int]($r.Bottom - $r.Top)\n"
            "    }\n"
            "} | ConvertTo-Json -Compress"
        )
        try:
            returncode, stdout, _ = await self._ps(script, timeout=15)
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
        """
        Launch a Windows application from WSL.

        Accepts args as a list or as a JSON string (the LLM sometimes sends "[]").
        Tries direct execution first, then falls back to PowerShell Start-Process.
        """
        # Normalize application in case the LLM sent a dict instead of a string.
        if not isinstance(application, str):
            application = str(application.get("name", application)) if isinstance(application, dict) else str(application)

        # Normalize args in case the LLM sent a JSON string instead of a list.
        if isinstance(args, str):
            try:
                args = json.loads(args)
                if not isinstance(args, list):
                    args = [str(args)] if args else []
            except (json.JSONDecodeError, ValueError):
                args = [args] if args.strip() else []
        args = [str(a) for a in (args or [])]

        parts = [application] + args
        try:
            proc = await asyncio.create_subprocess_exec(
                *parts,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                _, stderr = await asyncio.wait_for(proc.communicate(), timeout=3)
                if proc.returncode not in (0, None):
                    err = stderr.decode(errors="replace").strip()
                    raise RuntimeError(err)
            except asyncio.TimeoutError:
                # Still running after 3 s — normal for GUI apps.
                pass
            return {"success": True, "application": application, "args": args, "method": "direct"}
        except Exception as direct_err:
            # Fall back to PowerShell Start-Process.
            try:
                ps_args = " ".join('"' + a + '"' for a in args)
                ps_cmd  = ('Start-Process "' + application + '"'
                           + (' -ArgumentList ' + ps_args if ps_args else "")).strip()
                ps = await asyncio.create_subprocess_exec(
                    "powershell.exe",
                    "-NoProfile", "-NonInteractive", "-Command", ps_cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                _, ps_err = await asyncio.wait_for(ps.communicate(), timeout=10)
                if ps.returncode != 0:
                    raise RuntimeError(ps_err.decode(errors="replace").strip())
                return {
                    "success": True,
                    "application": application,
                    "args": args,
                    "method": "powershell",
                }
            except Exception as ps_err:
                logger.debug("[wsl:launch] %s", traceback.format_exc())
                return {"error": f"direct: {direct_err}; powershell: {ps_err}"}
