"""
prompt_loader.py — Loads and caches prompt text files from the prompts/ directory.

Files use Python string.Template syntax for variable substitution:
  $variable or ${variable}  — replaced with the supplied value
  $$                        — literal dollar sign

Curly braces { } are treated as plain text, so JSON examples in prompt files
do not need escaping.

Usage
-----
    loader = PromptLoader("prompts")
    text   = loader.text("system_base")           # raw text, no substitution
    msg    = loader.render("runtime_error_retry",  # substituted
                           count=3, tools="shell_run, desktop_launch")
"""

import logging
import string
from pathlib import Path

logger = logging.getLogger(__name__)


class PromptLoader:
    """
    Reads .txt files from a prompt directory and optionally substitutes
    $variable placeholders using string.Template.safe_substitute.

    Results are cached in memory so each file is read at most once per
    process lifetime.
    """

    def __init__(self, prompts_dir: str = "prompts"):
        self._dir = Path(prompts_dir)
        self._cache: dict[str, str] = {}

    def _read(self, name: str) -> str:
        if name not in self._cache:
            path = self._dir / f"{name}.txt"
            try:
                self._cache[name] = path.read_text(encoding="utf-8").strip()
            except FileNotFoundError:
                logger.warning("[PromptLoader] Missing prompt file: %s", path)
                self._cache[name] = ""
            except OSError as e:
                logger.warning("[PromptLoader] Could not read %s: %s", path, e)
                self._cache[name] = ""
        return self._cache[name]

    def text(self, name: str) -> str:
        """Return the raw prompt text with no substitution."""
        return self._read(name)

    def render(self, name: str, **kwargs) -> str:
        """Return the prompt text with $variable placeholders substituted.

        Uses safe_substitute so unrecognised placeholders are left intact
        rather than raising a KeyError.
        """
        return string.Template(self._read(name)).safe_substitute(**kwargs)
