"""
tui.app — Textual-based TUI for the OpenWebUI desktop agent.

Layout
------
  ┌─────────────────────────────────────────────────────┐
  │  [OWUI Agent]  model: llama3.1:70b   tools: 11      │  ← Header
  ├─────────────────────────────────────────────────────┤
  │                                                     │
  │  Conversation and tool output (scrollable)          │  ← ConversationView
  │                                                     │
  ├─────────────────────────────────────────────────────┤
  │  Ready  │  model: llama3.2  │  history: 12  │  … │  ← StatusBar
  ├─────────────────────────────────────────────────────┤
  │  > _                                                │  ← Input
  └─────────────────────────────────────────────────────┘

Key bindings
------------
  Enter       — Submit input
  Ctrl+C      — Exit
  Ctrl+P      — Command palette (type "model" → Change Model to switch models)
  Ctrl+R      — Reset conversation history
  Ctrl+S      — Save conversation to JSONL history file
  Ctrl+T      — Toggle tool output visibility
  Escape      — Cancel ongoing agent task (best-effort)
  F1          — Show help overlay
"""

import logging
import sys
from datetime import datetime
from pathlib import Path

from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.reactive import reactive
from textual.widgets import (
    Footer,
    Header,
    Input,
    RichLog,
    Static,
)
from textual.worker import Worker

from .actions import AgentTUIActionsMixin
from .commands import _AgentCommands
from .log_bridge import _StderrProxy, _TUILogHandler
from .render import AgentTUIRenderMixin

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Main TUI Application
# ---------------------------------------------------------------------------

class AgentTUI(
    AgentTUIRenderMixin,
    AgentTUIActionsMixin,
    App,
):
    """
    Textual application driving the interactive TUI session.

    Parameters
    ----------
    agent : Agent
        Initialized agent instance (from agent.py).
    client : OpenWebUIClient
        Initialized API client (used by the model picker).
    cfg : dict
        Full configuration dict (mutated in-place when model is changed).
    tool_names : list[str]
        Names of registered tools, for display in header and help.
    config_path : str
        Path to config.json; used to persist model selection when requested.
    """

    TITLE = "OpenWebUI Desktop Agent"
    # Replace the default SystemCommands (toggle theme, quit, maximize) with
    # our own command set so the palette shows useful agent actions immediately.
    COMMANDS = {_AgentCommands}
    BINDINGS = [
        Binding("ctrl+c", "quit", "Exit", show=True),
        Binding("ctrl+r", "reset", "Reset", show=True),
        Binding("ctrl+s", "save", "Save", show=True),
        Binding("ctrl+t", "toggle_tools", "Tools", show=True),
        Binding("f1", "help", "Help", show=True),
        Binding("escape", "cancel_task", "Cancel", show=False),
    ]

    status_text = reactive("Ready")
    show_tools = reactive(True)

    DEFAULT_CSS = """
    AgentTUI {
        background: $background;
    }
    #conversation {
        border: solid $primary 50%;
        height: 1fr;
        padding: 0 1;
    }
    #status-bar {
        background: $surface;
        height: 1;
        padding: 0 1;
        color: $text-muted;
    }
    #input-bar {
        height: 3;
        border: solid $accent;
        margin: 0 0;
    }
    Input {
        background: $surface;
    }
    """

    def __init__(
        self,
        agent,
        client,
        cfg: dict,
        tool_names: list[str],
        config_path: str = "config.json",
    ):
        super().__init__()
        self._agent = agent
        self._client = client
        self._cfg = cfg
        self._config_path = config_path
        self._tui_cfg = cfg.get("tui", {})
        self._tool_names = tool_names
        self._history_file = Path(self._tui_cfg.get("history_file", "logs/history.jsonl"))
        self._active_task: Worker | None = None
        self.show_tools = self._tui_cfg.get("show_tool_calls", True)
        # Per-session log file — created on mount, one file per TUI invocation.
        self._session_log: Path | None = None
        # Logging handler that routes INFO+ records into the
        # conversation pane so library warnings (urllib3, asyncio, etc.)
        # don't paint over the TUI layout.  Installed on mount, removed
        # on unmount.  See _install_log_handler for details.
        self._log_handler: logging.Handler | None = None
        self._old_stderr: object | None = None  # restored on uninstall
        # stderr/stdout StreamHandlers that were attached to the root
        # logger before mount; we detach them on install (so they don't
        # paint the terminal) and re-attach them on uninstall so the
        # process's logging state is restored when the TUI exits.
        self._displaced_handlers: list[tuple[logging.Logger, logging.Handler]] = []
        # Streamed text_delta accumulation (openwebui.stream=true): deltas
        # are buffered and flushed to the conversation pane one completed
        # line at a time (RichLog is append-only, so partial lines wait for
        # their newline or for the final "text" event).
        self._stream_buffer: str = ""
        self._streamed_this_turn: bool = False

    # ------------------------------------------------------------------
    # Composition
    # ------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield RichLog(id="conversation", highlight=True, markup=True, wrap=True)
        yield Static(id="status-bar")
        yield Input(placeholder="Enter a task or command...", id="input-bar")
        yield Footer()

    def on_mount(self) -> None:
        # Install the logging bridge BEFORE anything that might warn —
        # the agent has already initialized at this point but any
        # subsequent library warning (urllib3 retry, pyautogui fail-safe,
        # etc.) should land in the conversation pane, not stderr.
        self._install_log_handler()

        # Register TUI callback so REST API tasks display progress here.
        try:
            from api import register_tui_callback
            register_tui_callback(self._on_api_task_event)
        except ImportError:
            pass

        log = self.query_one("#conversation", RichLog)
        model = self._cfg.get("openwebui", {}).get("model", "unknown")
        vision = self._agent._vision_screenshots
        vision_str = "[green]on[/green]" if vision else "[yellow]off[/yellow]"
        log.write(
            f"[bold cyan]OpenWebUI Desktop Agent[/bold cyan]  "
            f"model=[green]{model}[/green]  "
            f"tools=[yellow]{len(self._tool_names)}[/yellow]  "
            f"vision={vision_str}\n"
            f"[dim]Type a task and press Enter.  "
            f"Ctrl+P → commands (Change Model, Toggle Vision, …).  F1 for help.  Ctrl+C to exit.[/dim]\n"
        )
        # Reactive watcher fires during init before the DOM exists (NoMatches
        # caught silently), and then _update_status("Ready") below is a no-op
        # because the reactive value is already "Ready".  Force a real render.
        self._update_status("Initializing…")
        self._update_status("Ready")
        self.query_one("#input-bar", Input).focus()

        # Open per-session log file (one per TUI launch, timestamped).
        try:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            log_dir = Path(self._tui_cfg.get("log_dir", "logs"))
            log_dir.mkdir(parents=True, exist_ok=True)
            self._session_log = log_dir / f"session_{ts}.log"
            with self._session_log.open("w", encoding="utf-8") as f:
                f.write(
                    f"=== AutoGUI Session: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ===\n"
                    f"Model: {model}\n"
                    f"Tools: {', '.join(self._tool_names)}\n"
                    f"{'=' * 60}\n\n"
                )
        except Exception as e:
            logger.warning("[tui.py:on_mount] Could not open session log: %s", e)
            self._session_log = None

    # ------------------------------------------------------------------
    # Input handling
    # ------------------------------------------------------------------

    @on(Input.Submitted)
    async def handle_input(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        if not text:
            return

        input_widget = self.query_one("#input-bar", Input)
        input_widget.value = ""
        input_widget.disabled = True

        log = self.query_one("#conversation", RichLog)
        log.write(f"\n[bold cyan]You:[/bold cyan] {text}")

        # Run the agent as a Textual worker so the event loop stays live and
        # keyboard bindings (Escape, Ctrl+C) continue to work while it runs.
        self._active_task = self._run_agent_work(text)

    # ------------------------------------------------------------------
    # Logging bridge — installed on mount so warnings land in the
    # conversation pane instead of corrupting the TUI layout.
    # ------------------------------------------------------------------

    def _install_log_handler(self) -> None:
        if self._log_handler is not None:
            return
        # Replace sys.stderr with a proxy that forwards lines as WARNING
        # log records.  This catches everything — logging StreamHandlers,
        # bare print(..., file=sys.stderr) calls, uvicorn startup lines,
        # or any library that writes directly to sys.stderr — so nothing
        # can paint raw characters over the Textual layout.
        self._old_stderr = sys.stderr
        sys.stderr = _StderrProxy()
        # Also displace any StreamHandlers on the root logger (and common
        # named loggers) that still hold a reference to the old stderr fd
        # so they don't double-emit once sys.stderr is the proxy.
        _named = ["uvicorn", "uvicorn.access", "uvicorn.error"]
        for lgr in [logging.getLogger()] + [logging.getLogger(n) for n in _named]:
            for h in list(lgr.handlers):
                if isinstance(h, logging.StreamHandler) and not isinstance(
                    h, logging.FileHandler,
                ):
                    lgr.removeHandler(h)
                    self._displaced_handlers.append((lgr, h))
        handler = _TUILogHandler(self)
        logging.getLogger().addHandler(handler)
        self._log_handler = handler

    def _uninstall_log_handler(self) -> None:
        """Detach the TUI log handler and re-attach any stderr/stdout
        handlers we displaced during install.  Called from action_quit
        AND on_unmount so the process's root-logger state is restored
        regardless of how the TUI exits."""
        root = logging.getLogger()
        if self._log_handler is not None:
            try:
                root.removeHandler(self._log_handler)
            except Exception:
                pass
            self._log_handler = None
        for lgr, h in self._displaced_handlers:
            try:
                if h not in lgr.handlers:
                    lgr.addHandler(h)
            except Exception:
                pass
        self._displaced_handlers.clear()
        if self._old_stderr is not None:
            sys.stderr = self._old_stderr  # type: ignore[assignment]
            self._old_stderr = None

    def on_unmount(self) -> None:
        """Textual lifecycle hook — fires for every shutdown path
        (Ctrl+C, action_quit, parent app exit, exception during teardown).
        Belt-and-suspenders cleanup so we never leave the process with
        a TUI log handler that points at a destroyed RichLog widget."""
        try:
            from api import unregister_tui_callback
            unregister_tui_callback()
        except ImportError:
            pass
        self._uninstall_log_handler()
