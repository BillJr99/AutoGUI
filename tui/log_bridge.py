"""
tui.log_bridge — logging/stderr bridge into the conversation pane.

A stderr proxy and a logging.Handler that route library warnings into the
TUI's conversation pane instead of painting over the layout.  Split out of
the original tui.py.
"""

import io
import logging
from typing import TYPE_CHECKING

from textual.widgets import RichLog

if TYPE_CHECKING:
    from .app import AgentTUI

# ---------------------------------------------------------------------------
# Logging bridge
# ---------------------------------------------------------------------------

class _StderrProxy(io.StringIO):
    """Replaces sys.stderr while the TUI is active.

    Any code that writes directly to sys.stderr (bypassing the logging
    framework) would otherwise paint raw characters over the Textual
    layout.  This proxy buffers by line and forwards each line as a
    WARNING-level log record so it lands in the TUI conversation pane
    instead.
    """

    def __init__(self) -> None:
        super().__init__()
        self._log = logging.getLogger("autogui.stderr")
        self._buf = ""

    def write(self, s: str) -> int:
        self._buf += s
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            if line.strip():
                self._log.warning("%s", line)
        return len(s)

    def flush(self) -> None:
        if self._buf.strip():
            self._log.warning("%s", self._buf)
            self._buf = ""


class _TUILogHandler(logging.Handler):
    """Logging handler that writes records into the TUI's conversation
    pane via Textual's thread-safe ``call_from_thread``.

    main.py installs a stderr StreamHandler at WARNING; under the TUI
    that paints raw ``[WARNING] …`` lines over the layout.  We attach
    this handler on mount and detach the stderr handler so records
    coming from our own code or from libraries (urllib3 retry, asyncio,
    pyautogui fail-safe, etc.) land in the visible conversation log
    instead.

    The handler is constructed at ``INFO`` so INFO-and-above records are
    forwarded; ``emit`` colours them by severity (dim for INFO, yellow
    for WARNING, red for ERROR/CRITICAL).  What actually reaches the
    conversation pane is still gated by the effective level of the
    emitting logger.
    """

    def __init__(self, app: "AgentTUI") -> None:
        super().__init__(level=logging.INFO)
        self._app = app
        self.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
        except Exception:
            return
        # Color the line by severity so a stray WARNING in the middle of
        # a conversation is easy to spot but not alarming, while ERROR/
        # CRITICAL stand out as red.
        colour = (
            "red" if record.levelno >= logging.ERROR
            else "yellow" if record.levelno >= logging.WARNING
            else "dim"
        )
        # Our log messages routinely contain `[backend:screenshot]`,
        # `[agent.py:run]`, etc. — RichLog with markup=True would parse
        # those bracketed strings as Rich markup tags and either swallow
        # the line or apply unintended styles.  Escape the formatted
        # record before wrapping it in our own colour tags.
        from rich.markup import escape as _rich_escape
        line = f"[{colour}]{_rich_escape(msg)}[/{colour}]"
        try:
            # call_from_thread is safe whether emit() runs on the
            # event-loop thread or a worker thread.
            self._app.call_from_thread(self._write_to_log, line)
        except Exception:
            # Late teardown / app already exiting — drop the record.
            pass

    def _write_to_log(self, line: str) -> None:
        try:
            log = self._app.query_one("#conversation", RichLog)
            log.write(line)
        except Exception:
            pass
