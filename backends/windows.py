"""
backends/windows.py — Desktop backend for Windows native.

Display operations:
  click / type_text / hotkey use SendInput directly via ctypes when
  available (real INPUT events, correct DPI behaviour, proper Unicode
  via KEYEVENTF_UNICODE).  Falls back to pyautogui only if SendInput
  setup fails.
Screenshot uses pyautogui + Pillow (base class).
Window management and launching use PowerShell.
If uiautomation is installed, find_element and get_window_tree are enabled.
"""

import asyncio
import base64
import ctypes
import json
import logging
import platform as _platform
import traceback

from backends.base import DesktopBackend

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# SendInput plumbing (Windows-only)
# ---------------------------------------------------------------------------
# Defined unconditionally so the module imports cleanly off-Windows; the
# actual ctypes calls inside the helpers are guarded by an availability flag.

_SENDINPUT_AVAILABLE = False
_user32 = None

if _platform.system() == "Windows":
    try:
        _user32 = ctypes.WinDLL("user32", use_last_error=True)
        _SENDINPUT_AVAILABLE = True
    except (OSError, AttributeError):
        _SENDINPUT_AVAILABLE = False

# struct definitions are cheap to declare even when not on Windows.

INPUT_MOUSE = 0
INPUT_KEYBOARD = 1

KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
KEYEVENTF_SCANCODE = 0x0008

MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_ABSOLUTE = 0x8000


# Common virtual-key codes for hotkeys.  Names are lowercased; modifiers and
# common navigation keys are listed here.  Anything missing falls through to
# the pyautogui fallback so we keep coverage for niche keys.
_VK_CODES: dict[str, int] = {
    "ctrl": 0x11, "control": 0x11,
    "alt": 0x12, "shift": 0x10,
    "win": 0x5B, "windows": 0x5B, "command": 0x5B, "cmd": 0x5B, "super": 0x5B,
    "enter": 0x0D, "return": 0x0D,
    "escape": 0x1B, "esc": 0x1B,
    "tab": 0x09, "space": 0x20, "backspace": 0x08, "delete": 0x2E, "del": 0x2E,
    "insert": 0x2D, "home": 0x24, "end": 0x23,
    "pageup": 0x21, "pgup": 0x21, "pagedown": 0x22, "pgdn": 0x22,
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "capslock": 0x14, "numlock": 0x90, "scrolllock": 0x91,
    "printscreen": 0x2C, "prtsc": 0x2C,
}
for _i in range(1, 13):
    _VK_CODES[f"f{_i}"] = 0x70 + _i - 1
for _c in "abcdefghijklmnopqrstuvwxyz":
    _VK_CODES[_c] = ord(_c.upper())
for _d in "0123456789":
    _VK_CODES[_d] = ord(_d)


# ctypes structs for SendInput.

ULONG_PTR = ctypes.c_size_t

class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", ctypes.c_long),
        ("dy", ctypes.c_long),
        ("mouseData", ctypes.c_uint32),
        ("dwFlags", ctypes.c_uint32),
        ("time", ctypes.c_uint32),
        ("dwExtraInfo", ULONG_PTR),
    ]

class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", ctypes.c_uint16),
        ("wScan", ctypes.c_uint16),
        ("dwFlags", ctypes.c_uint32),
        ("time", ctypes.c_uint32),
        ("dwExtraInfo", ULONG_PTR),
    ]

class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", ctypes.c_uint32),
        ("wParamL", ctypes.c_uint16),
        ("wParamH", ctypes.c_uint16),
    ]

class _INPUTunion(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]

class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", ctypes.c_uint32), ("u", _INPUTunion)]


def _send_inputs(inputs: list[INPUT]) -> bool:
    """ctypes wrapper around user32.SendInput.  Returns True on success."""
    if not _SENDINPUT_AVAILABLE or _user32 is None:
        return False
    n = len(inputs)
    arr_t = INPUT * n
    arr = arr_t(*inputs)
    sent = _user32.SendInput(n, arr, ctypes.sizeof(INPUT))
    return sent == n


def _make_mouse_input(flags: int, dx: int = 0, dy: int = 0) -> INPUT:
    inp = INPUT()
    inp.type = INPUT_MOUSE
    inp.mi = MOUSEINPUT(dx=dx, dy=dy, mouseData=0, dwFlags=flags, time=0, dwExtraInfo=0)
    return inp


def _make_key_input(vk: int = 0, scan: int = 0, flags: int = 0) -> INPUT:
    inp = INPUT()
    inp.type = INPUT_KEYBOARD
    inp.ki = KEYBDINPUT(wVk=vk, wScan=scan, dwFlags=flags, time=0, dwExtraInfo=0)
    return inp


def _abs_mouse_coord(x: int, y: int) -> tuple[int, int]:
    """Convert screen pixels to the [0, 65535] absolute-mouse range."""
    if _user32 is None:
        return x, y
    SM_CXSCREEN = 0
    SM_CYSCREEN = 1
    sw = _user32.GetSystemMetrics(SM_CXSCREEN) or 1
    sh = _user32.GetSystemMetrics(SM_CYSCREEN) or 1
    return (
        int((x * 65535) / sw),
        int((y * 65535) / sh),
    )


class WindowsBackend(DesktopBackend):

    # Identical to WSLBackend._WIN_FOCUS_TYPE — duplicated to avoid cross-import.
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

    async def _ps(self, script: str, timeout: int = 15) -> tuple[int, str, str]:
        """Execute *script* via powershell -EncodedCommand. Returns (returncode, stdout, stderr)."""
        encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        try:
            proc = await asyncio.create_subprocess_exec(
                "powershell",
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
            return (-1, "", "powershell not found")
        except asyncio.TimeoutError:
            return (-2, "", f"PowerShell timed out after {timeout}s")

    def capabilities(self) -> dict:
        try:
            import uiautomation  # noqa: F401
            return {"find_element": True, "get_window_tree": True, "activate_window": True, "get_active_window": True, "get_window_text": True}
        except ImportError:
            return {"find_element": False, "get_window_tree": False, "activate_window": True, "get_active_window": True, "get_window_text": True}

    # ------------------------------------------------------------------
    # Native input via SendInput (Phase 10)
    # ------------------------------------------------------------------

    async def click(
        self,
        x: int,
        y: int,
        button: str = "left",
        clicks: int = 1,
    ) -> dict:
        """SendInput-based click.  Falls back to the pyautogui base impl
        if SendInput plumbing isn't available (e.g. unit-test environment)."""
        if not _SENDINPUT_AVAILABLE:
            return await super().click(x, y, button=button, clicks=clicks)
        loop = asyncio.get_event_loop()
        try:
            ax, ay = _abs_mouse_coord(int(x), int(y))
            down = {
                "left": MOUSEEVENTF_LEFTDOWN,
                "right": MOUSEEVENTF_RIGHTDOWN,
                "middle": MOUSEEVENTF_MIDDLEDOWN,
            }.get((button or "left").lower(), MOUSEEVENTF_LEFTDOWN)
            up = {
                "left": MOUSEEVENTF_LEFTUP,
                "right": MOUSEEVENTF_RIGHTUP,
                "middle": MOUSEEVENTF_MIDDLEUP,
            }.get((button or "left").lower(), MOUSEEVENTF_LEFTUP)

            def _do():
                # Move first, then click N times.  Combining MOVE with the
                # first DOWN gives a more natural single SendInput batch.
                _send_inputs([
                    _make_mouse_input(MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE, ax, ay)
                ])
                ok = True
                for _ in range(max(1, int(clicks))):
                    ok &= _send_inputs([_make_mouse_input(down)])
                    ok &= _send_inputs([_make_mouse_input(up)])
                return ok

            ok = await loop.run_in_executor(None, _do)
            if not ok:
                return await super().click(x, y, button=button, clicks=clicks)
            return {
                "success": True, "x": int(x), "y": int(y),
                "button": button, "clicks": int(clicks), "method": "sendinput",
            }
        except Exception as e:
            logger.debug("[windows:click] SendInput failed: %s", traceback.format_exc())
            return await super().click(x, y, button=button, clicks=clicks)

    async def type_text(self, text: str) -> dict:
        """SendInput KEYEVENTF_UNICODE for full Unicode coverage."""
        if not _SENDINPUT_AVAILABLE or not text:
            return await super().type_text(text)
        loop = asyncio.get_event_loop()
        try:
            def _do():
                ok = True
                for ch in text:
                    code = ord(ch)
                    # Surrogate pairs (chars > U+FFFF) need to be sent as
                    # two 16-bit code units.  We emit per-codepoint here
                    # using utf-16 to be safe.
                    units = ch.encode("utf-16-le")
                    for i in range(0, len(units), 2):
                        wScan = int.from_bytes(units[i:i+2], "little")
                        ok &= _send_inputs([
                            _make_key_input(scan=wScan, flags=KEYEVENTF_UNICODE),
                            _make_key_input(scan=wScan, flags=KEYEVENTF_UNICODE | KEYEVENTF_KEYUP),
                        ])
                return ok

            ok = await loop.run_in_executor(None, _do)
            if not ok:
                return await super().type_text(text)
            return {"success": True, "length": len(text), "method": "sendinput"}
        except Exception:
            logger.debug("[windows:type_text] SendInput failed: %s", traceback.format_exc())
            return await super().type_text(text)

    async def hotkey(self, keys: list[str]) -> dict:
        """Press a chord via SendInput; falls back to pyautogui for any
        key whose virtual-key code isn't in the lookup table."""
        if not _SENDINPUT_AVAILABLE or not keys:
            return await super().hotkey(keys)
        try:
            vks: list[int] = []
            for k in keys:
                key = str(k).lower().strip()
                vk = _VK_CODES.get(key)
                if vk is None:
                    return await super().hotkey(keys)
                vks.append(vk)

            loop = asyncio.get_event_loop()

            def _do():
                ok = True
                for vk in vks:
                    ok &= _send_inputs([_make_key_input(vk=vk)])
                # Release in reverse — modifiers go up last so the chord
                # is held atomically.
                for vk in reversed(vks):
                    ok &= _send_inputs([_make_key_input(vk=vk, flags=KEYEVENTF_KEYUP)])
                return ok

            ok = await loop.run_in_executor(None, _do)
            if not ok:
                return await super().hotkey(keys)
            return {"success": True, "keys": list(keys), "method": "sendinput"}
        except Exception:
            logger.debug("[windows:hotkey] SendInput failed: %s", traceback.format_exc())
            return await super().hotkey(keys)

    async def list_windows(self) -> dict:
        """List visible windows with titles, pids, bounding boxes, and active status."""
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
            logger.debug("[windows:list_windows] %s", traceback.format_exc())
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
            parts = [application] + args
            # DETACHED_PROCESS (0x8) prevents the child from inheriting our console.
            proc = await asyncio.create_subprocess_exec(
                *parts,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
                creationflags=0x00000008,
            )
            try:
                _, stderr = await asyncio.wait_for(proc.communicate(), timeout=3)
                if proc.returncode not in (0, None):
                    raise RuntimeError(stderr.decode(errors="replace").strip())
            except asyncio.TimeoutError:
                pass
            return {"success": True, "application": application, "args": args}
        except Exception as e:
            logger.debug("[windows:launch] %s", traceback.format_exc())
            return {"error": str(e)}

    async def activate_window(
        self,
        title: str = "",
        pid: int = 0,
        app: str = "",
        window_id: str = "",
    ) -> dict:
        """Bring a window to the foreground — same logic as WSLBackend."""
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
            logger.debug("[windows:activate_window] %s", traceback.format_exc())
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
            _, stdout, _ = await self._ps(script, timeout=10)
            if not stdout:
                return {"found": False}
            return json.loads(stdout)
        except Exception as e:
            logger.debug("[windows:get_active_window] %s", traceback.format_exc())
            return {"found": False, "error": str(e)}

    async def get_window_text(self, max_chars: int = 50000) -> dict:
        """Select all + copy in the focused window, return text via clipboard."""
        script = (
            "$ErrorActionPreference = 'SilentlyContinue'\n"
            "Add-Type -AssemblyName System.Windows.Forms\n"
            "$old = Get-Clipboard -Raw\n"
            "[System.Windows.Forms.SendKeys]::SendWait('^a')\n"
            "Start-Sleep -Milliseconds 300\n"
            "[System.Windows.Forms.SendKeys]::SendWait('^c')\n"
            "Start-Sleep -Milliseconds 500\n"
            "$text = Get-Clipboard -Raw\n"
            "try { if ($null -ne $old -and $old -ne '') { Set-Clipboard -Value $old } else { Set-Clipboard -Value '' } } catch {}\n"
            f"$maxLen = {int(max_chars)}\n"
            "$truncated = $false\n"
            "if ($null -eq $text) { $text = '' }\n"
            "if ($text.Length -gt $maxLen) { $text = $text.Substring(0, $maxLen); $truncated = $true }\n"
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
            logger.debug("[windows:get_window_text] %s", traceback.format_exc())
            return {"error": str(e)}

    async def find_element(
        self,
        name: str | None = None,
        control_type: str | None = None,
        window_title: str | None = None,
        index: int = 0,
    ) -> dict:
        try:
            import uiautomation as auto
            loop = asyncio.get_event_loop()

            def _find():
                kwargs = {}
                if name:
                    kwargs["Name"] = name
                if control_type:
                    kwargs["ControlType"] = getattr(auto.ControlType, control_type, None)
                root = (
                    auto.WindowControl(searchDepth=1, Name=window_title)
                    if window_title
                    else auto.GetRootControl()
                )
                results = list(root.GetChildren()) if not kwargs else [
                    root.Control(**kwargs)
                ]
                if index >= len(results):
                    return {"error": f"No element at index {index} (found {len(results)})"}
                el = results[index]
                rect = el.BoundingRectangle
                return {
                    "name": el.Name,
                    "control_type": el.ControlTypeName,
                    "rect": {"x": rect.left, "y": rect.top, "width": rect.width(), "height": rect.height()},
                }

            return await loop.run_in_executor(None, _find)
        except ImportError:
            return {"error": "uiautomation not installed — pip install uiautomation"}
        except Exception as e:
            logger.debug("[windows:find_element] %s", traceback.format_exc())
            return {"error": str(e)}

    async def get_window_tree(
        self,
        window_title: str | None = None,
        depth: int = 3,
    ) -> dict:
        try:
            import uiautomation as auto
            loop = asyncio.get_event_loop()

            def _tree():
                root = (
                    auto.WindowControl(searchDepth=1, Name=window_title)
                    if window_title
                    else auto.GetRootControl()
                )

                def _walk(ctrl, d):
                    if d < 0:
                        return None
                    rect = ctrl.BoundingRectangle
                    node = {
                        "name": ctrl.Name,
                        "type": ctrl.ControlTypeName,
                        "rect": {"x": rect.left, "y": rect.top,
                                 "w": rect.width(), "h": rect.height()},
                    }
                    children = [_walk(c, d - 1) for c in ctrl.GetChildren()]
                    children = [c for c in children if c is not None]
                    if children:
                        node["children"] = children
                    return node

                return _walk(root, depth)

            tree = await loop.run_in_executor(None, _tree)
            return {"tree": tree}
        except ImportError:
            return {"error": "uiautomation not installed — pip install uiautomation"}
        except Exception as e:
            logger.debug("[windows:get_window_tree] %s", traceback.format_exc())
            return {"error": str(e)}
