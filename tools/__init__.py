"""
tools — tool registry and implementations, decomposed from tools.py.

The public import surface is unchanged: ``from tools import ToolRegistry``
keeps working exactly as before.  The implementation now lives in:

  registry.py  ToolRegistry — schema catalog, dispatch, arg aliasing,
               action-scope safety gate
  shell_fs.py  shell_run / fs_read / fs_write / fs_list implementations,
               destructive-command guard, LLM-arg coercers + registration
  desktop.py   desktop_* tool registration (incl. extended a11y tools and
               desktop_wait_for)
  browser.py   browser_* tool registration (Playwright)
"""

from .registry import ToolRegistry
from .shell_fs import fs_list, fs_read, fs_write, shell_run

__all__ = ["ToolRegistry", "fs_list", "fs_read", "fs_write", "shell_run"]
