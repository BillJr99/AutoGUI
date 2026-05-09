"""
backends/windows.py — Desktop backend for Windows native.

Display operations use pyautogui.
Window management and launching use PowerShell.
If uiautomation is installed, find_element and get_window_tree are enabled.
"""

import asyncio
import base64
import json
import logging
import traceback

from backends.base import DesktopBackend

logger = logging.getLogger(__name__)


class WindowsBackend(DesktopBackend):

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
            return {"find_element": True, "get_window_tree": True}
        except ImportError:
            return {"find_element": False, "get_window_tree": False}

    async def list_windows(self) -> dict:
        """List visible windows with titles, pids, and bounding boxes."""
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
