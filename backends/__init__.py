"""
backends/__init__.py — Backend factory.

get_backend(platform_info) selects and instantiates the correct DesktopBackend
subclass for the detected runtime environment.
"""

from backends.base import DesktopBackend
from platform_detect import PlatformInfo


def get_backend(platform_info: PlatformInfo) -> DesktopBackend:
    """
    Return the best available DesktopBackend for the current platform.

    Selection order
    ---------------
    1. WSL   — Linux with a Microsoft kernel (uses Windows interop for windows/launch)
    2. Windows native
    3. macOS
    4. Linux/Wayland (WAYLAND_DISPLAY set, not WSL)
    5. Linux/X11    (DISPLAY set, not WSL)
    6. Headless     — base class; display tools will return informative errors
    """
    system = platform_info["system"]

    if platform_info["is_wsl"]:
        from backends.wsl import WSLBackend
        return WSLBackend()

    if system == "Windows":
        from backends.windows import WindowsBackend
        return WindowsBackend()

    if system == "Darwin":
        from backends.macos import MacOSBackend
        return MacOSBackend()

    if platform_info["is_wayland"]:
        from backends.linux_wayland import WaylandBackend
        return WaylandBackend()

    if platform_info["is_x11"]:
        from backends.linux_x11 import X11Backend
        return X11Backend()

    # Headless Linux or unknown — use base; pyautogui calls will fail gracefully.
    return DesktopBackend()
