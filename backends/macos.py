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
