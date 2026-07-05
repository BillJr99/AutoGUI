"""
agent.prompts — OS detection and OS-specific system-prompt assembly.
"""

import platform as _platform

from prompt_loader import PromptLoader

# ---------------------------------------------------------------------------
# OS detection
# ---------------------------------------------------------------------------

def _proc_version_has_microsoft() -> bool:
    try:
        from pathlib import Path
        return "microsoft" in Path("/proc/version").read_text().lower()
    except OSError:
        return False


def _build_os_instructions(loader: PromptLoader) -> str:
    """Return OS-specific instructions by loading the appropriate prompt file."""
    system = _platform.system()
    release = _platform.release().lower()
    is_wsl = system == "Linux" and (
        "microsoft" in release or "wsl" in release or _proc_version_has_microsoft()
    )
    if is_wsl:
        name = "system_os_wsl"
    elif system == "Windows":
        name = "system_os_windows"
    elif system == "Darwin":
        name = "system_os_macos"
    else:
        name = "system_os_linux"
    return loader.text(name)
