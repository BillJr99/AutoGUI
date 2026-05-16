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
    **{f"f{i}": "{F" + str(i) + "}" for i in range(1, 13)},
}
# Characters that have special meaning in SendKeys and must be braced.
_SK_ESCAPE = set("+^%~{}[]() ")


def _decode_clixml(text: str) -> str:
    """Extract human-readable error/warning messages from PowerShell's
    CLIXML-serialised stderr.

    PowerShell wraps non-success streams in
    ``#< CLIXML\\n<Objs ...>...</Objs>`` when invoked via
    ``powershell.exe -Command`` / ``-EncodedCommand`` without
    ``-OutputFormat Text``.  We pass -OutputFormat Text now, but some
    older 5.1 builds still emit CLIXML for certain pipeline shapes —
    parse it as a backstop so the user always sees a real diagnostic
    instead of an unreadable XML blob.
    """
    try:
        import re as _re
        envelope = text.split("\n", 1)[1] if text.startswith("#< CLIXML") else text
        # Pull every <S S="Error"> ... </S> — that's where the actual
        # exception messages live.  Order is preserved so the user
        # sees them in the same sequence PowerShell produced them.
        msgs = _re.findall(
            r'<S S="Error">(.*?)</S>',
            envelope,
            flags=_re.DOTALL,
        )
        if not msgs:
            return text  # unrecognised CLIXML shape; show raw
        decoded = []
        for m in msgs:
            # CLIXML escapes some control chars as _x000D_ / _x000A_
            # (UTF-16 hex escapes); replace the common ones with their
            # actual characters.
            m = (
                m.replace("_x000D_", "\r")
                 .replace("_x000A_", "\n")
                 .replace("_x0009_", "\t")
            )
            decoded.append(m.strip())
        return " | ".join(s for s in decoded if s)
    except Exception:
        return text


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
        caps = super().capabilities()
        caps.update({"find_element": False, "get_window_tree": False, "activate_window": True, "get_active_window": True, "get_window_text": True})
        return caps

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
                # NOTE: do NOT add -OutputFormat Text here.  Main branch
                # (which the user confirmed worked) uses just
                # -NoProfile -NonInteractive -EncodedCommand, and
                # adding -OutputFormat Text in fbf58bd silently broke
                # screenshots — PowerShell echoed the source script
                # back as stderr instead of executing it.  The
                # _decode_clixml helper below handles any CLIXML that
                # PowerShell DOES emit on stderr without needing the
                # -OutputFormat flag.
                "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            stdout_text = stdout.decode("utf-8", errors="replace").strip()
            stderr_text = stderr.decode("utf-8", errors="replace").strip()
            # Decode CLIXML if PowerShell serialized non-success
            # streams that way (older 5.1 builds with certain
            # pipeline shapes).  No-op for plain-text stderr.
            if stderr_text.startswith("#< CLIXML"):
                stderr_text = _decode_clixml(stderr_text)
            return (
                proc.returncode or 0,
                stdout_text,
                stderr_text,
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

            # Script body restored byte-for-byte to the version on main
            # (which the user confirmed worked).  Specifically:
            #   - NO $ErrorActionPreference='Stop'.  Add-Type emits a
            #     non-terminating warning when the assembly is already
            #     loaded, and 'Stop' promoted that warning to a fatal
            #     error which made the entire script abort before
            #     producing output (this was the empty-base64 +
            #     "stderr=script content" symptom the user kept hitting).
            #   - Write-Host for the metadata line, NOT a bare string.
            #     Bare strings hit the success stream too, but
            #     Write-Host has reliably worked across every WSL
            #     PowerShell variant the user tested on main.
            #   - NO try/catch wrapper.  PowerShell's default behaviour
            #     prints a real exception to stderr; our _decode_clixml
            #     helper cleans up the CLIXML envelope downstream.
            # System.Drawing.Bitmap's 2-arg constructor (width, height)
            # picks a default pixel format that's incompatible with
            # CopyFromScreen on some multi-monitor / WSL-interop setups
            # — the user hits "New-Object : Exception calling .ctor with
            # 2 argument(s): Parameter is not valid".  Passing an
            # explicit Format32bppArgb (3-arg constructor) makes the
            # bitmap-creation deterministic across machines.  Same fix
            # applies to both region and full-screen capture.
            pixfmt_setup = (
                "$pixfmt = [System.Drawing.Imaging.PixelFormat]::Format32bppArgb\n"
            )
            if region:
                script = (
                    "Add-Type -AssemblyName System.Drawing\n"
                    + pixfmt_setup +
                    "$bmp = New-Object System.Drawing.Bitmap __W__, __H__, $pixfmt\n"
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
                    "$w      = [int]($right - $left)\n"
                    "$h      = [int]($bottom - $top)\n"
                    "$n      = $scr.Count\n"
                    + pixfmt_setup +
                    "$bmp    = New-Object System.Drawing.Bitmap $w, $h, $pixfmt\n"
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

            # Find the metadata line (PowerShell sometimes prepends
            # blank lines or progress junk before "monitors=...") and
            # treat everything after it as the base64 payload.  Falling
            # back to "first line is meta, rest is b64" as before only
            # works when stdout is exactly two lines; CRLF/CR splitting,
            # PSReadLine output, or Write-Host buffering can break that.
            lines = stdout.splitlines()
            meta_idx = next(
                (i for i, ln in enumerate(lines) if ln.lstrip().startswith("monitors=")),
                -1,
            )
            if meta_idx >= 0:
                meta_line = lines[meta_idx]
                b64_data = "".join(ln.strip() for ln in lines[meta_idx + 1:])
            else:
                # No metadata found — treat the whole stdout as base64.
                meta_line = ""
                b64_data = "".join(ln.strip() for ln in lines)

            # Parse metadata if present
            monitors = 1
            for part in meta_line.split():
                if part.startswith("monitors="):
                    try:
                        monitors = int(part.split("=")[1])
                    except ValueError:
                        pass

            if not b64_data:
                # Empty base64 means PowerShell ran but emitted nothing
                # parseable as image data.  Image.open would later raise
                # ``cannot identify image file`` — surface a clearer
                # diagnostic instead so the user knows where it went wrong.
                return {
                    "error": (
                        "Screenshot failed: empty base64 payload from PowerShell. "
                        f"stdout_head={stdout[:200]!r} stderr_head={stderr[:200]!r}"
                    ),
                }
            try:
                img_bytes = base64.b64decode(b64_data)
            except (ValueError, binascii.Error) as e:
                return {
                    "error": (
                        f"Screenshot failed: base64 decode error ({e}). "
                        f"b64_head={b64_data[:120]!r}"
                    ),
                }
            try:
                img = Image.open(io.BytesIO(img_bytes))
                img.load()  # force the decode now so any UnidentifiedImageError surfaces here
            except Exception as e:
                return {
                    "error": (
                        f"Screenshot failed: PIL could not decode "
                        f"{len(img_bytes)} bytes ({type(e).__name__}: {e}). "
                        f"First 16 bytes (hex): {img_bytes[:16].hex()}"
                    ),
                }

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
            # WARNING (not DEBUG) so the failure is visible in the user's
            # log + TUI bridge.  WSL screenshots fail in practice when
            # PowerShell times out or the System.Drawing assemblies are
            # unavailable; hiding it at DEBUG made the root cause
            # invisible and produced misleading "Auto-screenshot saved
            # (vision off): ? (None×None)" messages downstream.
            logger.warning("[wsl:screenshot] capture failed: %s", e)
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

    # C# type for window focus operations (focus, verify, bounds).
    # ShowWindowAsync is non-blocking; SW_RESTORE=9 un-minimises.
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

    # C# type for WindowFromPoint + GetAncestor — used by scroll to focus the
    # right window before sending keyboard scroll events.
    _SCROLL_FOCUS_TYPE = (
        "Add-Type -TypeDefinition @\"\n"
        "using System;\n"
        "using System.Runtime.InteropServices;\n"
        "public class WinScrFocus {\n"
        "    [StructLayout(LayoutKind.Sequential)]\n"
        "    public struct POINT { public int X; public int Y; }\n"
        "    [DllImport(\"user32.dll\")] public static extern IntPtr WindowFromPoint(POINT p);\n"
        "    [DllImport(\"user32.dll\")] public static extern IntPtr GetAncestor(IntPtr h, uint f);\n"
        "    [DllImport(\"user32.dll\")] public static extern bool SetForegroundWindow(IntPtr h);\n"
        "}\n"
        "\"@ -Language CSharp\n"
    )

    async def scroll(
        self,
        x: int = 0,
        y: int = 0,
        clicks: int = 3,
        direction: str = "down",
    ) -> dict:
        """
        Scroll using keyboard Page Down/Up for reliable behaviour in all
        Windows apps (including browsers where mouse-wheel routing can fail).

        If x and y are both > 0, use WindowFromPoint to bring that window to
        the foreground before sending keys; otherwise the current foreground
        window receives the scroll.

        Each "click" = one Page Down or Page Up keystroke.
        """
        try:
            n = max(1, int(clicks))
            key = "{PGDN}" if direction == "down" else "{PGUP}"
            sk_str = key * n

            x_int, y_int = int(x), int(y)
            if x_int > 0 and y_int > 0:
                focus_block = (
                    self._SCROLL_FOCUS_TYPE +
                    "$ErrorActionPreference = 'SilentlyContinue'\n"
                    "$pt = New-Object WinScrFocus+POINT\n"
                    f"$pt.X = {x_int}; $pt.Y = {y_int}\n"
                    "$child = [WinScrFocus]::WindowFromPoint($pt)\n"
                    # GA_ROOT = 2: get the top-level ancestor window
                    "$root = [WinScrFocus]::GetAncestor($child, 2)\n"
                    "if ($root -ne [IntPtr]::Zero) { [WinScrFocus]::SetForegroundWindow($root) | Out-Null }\n"
                    "Start-Sleep -Milliseconds 100\n"
                )
            else:
                focus_block = "$ErrorActionPreference = 'SilentlyContinue'\n"

            script = (
                focus_block +
                "Add-Type -AssemblyName System.Windows.Forms\n"
                f"[System.Windows.Forms.SendKeys]::SendWait('{sk_str}')\n"
            )

            returncode, _, stderr = await self._ps(script, timeout=10)
            if returncode != 0:
                return {"error": f"scroll failed (code {returncode}): {stderr}"}
            if stderr:
                logger.debug("[wsl:scroll] stderr (non-fatal): %s", stderr)
            return {"success": True, "direction": direction, "clicks": clicks, "method": "keyboard"}
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
        """List visible Windows windows with titles, pids, bounding boxes, and active status."""
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
        After a successful launch the window is brought to the foreground automatically.
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

        # Derive a process-name hint (basename without .exe) for window activation.
        app_basename = application.replace("\\", "/").rsplit("/", 1)[-1]
        name_hint = app_basename.lower()
        if name_hint.endswith(".exe"):
            name_hint = name_hint[:-4]

        parts = [application] + args
        result: dict = {}
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
            result = {"success": True, "application": application, "args": args, "method": "direct"}
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
                result = {
                    "success": True,
                    "application": application,
                    "args": args,
                    "method": "powershell",
                }
            except Exception as ps_err:
                logger.debug("[wsl:launch] %s", traceback.format_exc())
                return {"error": f"direct: {direct_err}; powershell: {ps_err}"}

        # Bring the launched application's window to the foreground.
        act = await self._activate_after_launch(name_hint)
        result["window_activated"] = act.get("window_activated", False)
        return result

    # ------------------------------------------------------------------
    # Window text extraction
    # ------------------------------------------------------------------

    async def get_window_text(self, max_chars: int = 50000) -> dict:
        """
        Select all text in the focused window (Ctrl+A then Ctrl+C), read the
        clipboard, restore the previous clipboard content, and return the text.
        Works in browsers, text editors, terminals, and most Windows apps.
        """
        script = (
            "$ErrorActionPreference = 'SilentlyContinue'\n"
            "Add-Type -AssemblyName System.Windows.Forms\n"
            # Save old clipboard (null if empty)
            "$old = Get-Clipboard -Raw\n"
            # Select all + copy
            "[System.Windows.Forms.SendKeys]::SendWait('^a')\n"
            "Start-Sleep -Milliseconds 300\n"
            "[System.Windows.Forms.SendKeys]::SendWait('^c')\n"
            "Start-Sleep -Milliseconds 500\n"
            # Read new clipboard
            "$text = Get-Clipboard -Raw\n"
            # Restore old clipboard
            "try { if ($null -ne $old -and $old -ne '') { Set-Clipboard -Value $old } else { Set-Clipboard -Value '' } } catch {}\n"
            # Truncate and return as JSON
            f"$maxLen = {int(max_chars)}\n"
            "$truncated = $false\n"
            "if ($null -eq $text) { $text = '' }\n"
            "if ($text.Length -gt $maxLen) { $text = $text.Substring(0, $maxLen); $truncated = $true }\n"
            "[PSCustomObject]@{ text = $text; length = $text.Length; truncated = $truncated } | ConvertTo-Json -Compress\n"
        )
        try:
            returncode, stdout, stderr = await self._ps(script, timeout=15)
            if not stdout:
                return {"error": f"get_window_text got no output (rc={returncode}): {stderr}"}
            return json.loads(stdout)
        except json.JSONDecodeError:
            return {"text": stdout, "length": len(stdout), "truncated": False}
        except Exception as e:
            logger.debug("[wsl:get_window_text] %s", traceback.format_exc())
            return {"error": str(e)}

    # ------------------------------------------------------------------
    # Window activation
    # ------------------------------------------------------------------

    async def _activate_after_launch(self, name_hint: str) -> dict:
        """
        Find a window whose process name matches name_hint and bring it to front.

        Checks immediately first (for apps already running), then polls for up to
        4 s to handle newly started processes that need time to create their window.
        SW_RESTORE (9) un-minimizes; SW_SHOW (5) ensures visibility.
        """
        safe = name_hint.replace("'", "''")
        proc_filter = (
            f"$_.Name -like '*{safe}*' -and "
            "$_.MainWindowHandle -ne [IntPtr]::Zero -and "
            "$_.MainWindowTitle -ne ''"
        )
        script = (
            self._WIN_FOCUS_TYPE +
            "$ErrorActionPreference = 'SilentlyContinue'\n"
            f"$w = Get-Process | Where-Object {{ {proc_filter} }} | Select-Object -First 1\n"
            "if (-not $w) {\n"
            "    $t = (Get-Date).AddSeconds(4)\n"
            "    do {\n"
            "        Start-Sleep -Milliseconds 300\n"
            f"        $w = Get-Process | Where-Object {{ {proc_filter} }} | Select-Object -First 1\n"
            "    } while (-not $w -and (Get-Date) -lt $t)\n"
            "}\n"
            "if (-not $w) { Write-Output 'NOT_FOUND'; exit 0 }\n"
            "[WinFocus]::ShowWindowAsync($w.MainWindowHandle, 9) | Out-Null\n"
            "Start-Sleep -Milliseconds 100\n"
            "[WinFocus]::SetForegroundWindow($w.MainWindowHandle) | Out-Null\n"
            "Write-Output \"ok pid=$($w.Id)\"\n"
        )
        try:
            returncode, stdout, _ = await self._ps(script, timeout=12)
            return {"window_activated": "NOT_FOUND" not in stdout}
        except Exception:
            logger.debug("[wsl:_activate_after_launch] %s", traceback.format_exc())
            return {"window_activated": False}

    async def activate_window(
        self,
        title: str = "",
        pid: int = 0,
        app: str = "",
        window_id: str = "",
    ) -> dict:
        """
        Bring a window to the foreground.  Match priority:
          1. window_id (MainWindowHandle string from desktop_list_windows — most precise)
          2. pid
          3. title (case-insensitive substring of MainWindowTitle)
          4. app (case-insensitive substring of ProcessName)

        Uses ShowWindowAsync + SetForegroundWindow with timing delays, then verifies
        via GetForegroundWindow.  If the OS does not confirm focus, falls back to
        clicking the title-bar area of the window.
        """
        if not any([title, pid, app, window_id]):
            return {"error": "Provide at least one of: title, pid, app, window_id"}
        if self._screen_observer is not None and title:
            result = await self._screen_observer.bring_to_foreground(window_title=title)
            if result is not None and result.get("success"):
                return {"success": True, "method": "screen_observer",
                        "window": result.get("window", title)}

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
            returncode, stdout, _ = await self._ps(script, timeout=10)
            if not stdout:
                return {"error": f"activate_window got no output (rc={returncode})"}

            result = json.loads(stdout)
            if not result.get("found"):
                criteria = " ".join(filter(None, [
                    f"id={window_id!r}" if window_id else "",
                    f"pid={pid}" if pid else "",
                    f"title={title!r}" if title else "",
                    f"app={app!r}" if app else "",
                ]))
                return {"error": f"No window found ({criteria})"}

            # OS did not confirm focus — click the title-bar area as a fallback.
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
        """Return info about the currently focused window."""
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
            returncode, stdout, _ = await self._ps(script, timeout=10)
            if not stdout:
                return {"found": False}
            return json.loads(stdout)
        except Exception as e:
            logger.debug("[wsl:get_active_window] %s", traceback.format_exc())
            return {"found": False, "error": str(e)}
