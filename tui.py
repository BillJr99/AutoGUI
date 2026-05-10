"""
tui.py — Textual-based TUI for the OpenWebUI desktop agent.

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

import asyncio
import json
import logging
import traceback
from datetime import datetime
from pathlib import Path

from textual import on, work
from textual.worker import Worker, WorkerState
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.command import Hit, Hits, Provider
from textual.containers import Container, Horizontal
from textual.css.query import NoMatches
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Checkbox,
    Footer,
    Header,
    Input,
    Label,
    ListItem,
    ListView,
    RichLog,
    Static,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config persistence helper (local copy avoids circular import with main.py)
# ---------------------------------------------------------------------------

def _tui_save_config(config_path: str, section: str, fields: dict) -> bool:
    """Merge *fields* into cfg[section] inside config_path. Returns True on success."""
    try:
        p = Path(config_path)
        existing = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
        existing.setdefault(section, {}).update(fields)
        p.write_text(json.dumps(existing, indent=2), encoding="utf-8")
        return True
    except Exception as e:
        logger.warning("[tui.py:_tui_save_config] %s", e)
        return False


# ---------------------------------------------------------------------------
# Help overlay
# ---------------------------------------------------------------------------

class HelpScreen(ModalScreen):
    """Modal overlay showing key bindings and tool list."""

    BINDINGS = [Binding("escape,f1", "dismiss", "Close")]

    def __init__(self, tool_names: list[str]):
        super().__init__()
        self._tools = tool_names

    def compose(self) -> ComposeResult:
        yield Container(
            Static(
                "[bold cyan]OpenWebUI Desktop Agent — Help[/bold cyan]\n\n"
                "[bold]Key Bindings[/bold]\n"
                "  Enter       Submit input\n"
                "  Ctrl+P      Command palette → type 'model' → Change Model\n"
                "  Ctrl+R      Reset conversation\n"
                "  Ctrl+S      Save conversation history\n"
                "  Ctrl+T      Toggle tool output\n"
                "  Escape      Cancel current task\n"
                "  F1          This help screen\n"
                "  Ctrl+C      Exit\n\n"
                f"[bold]Available Tools ({len(self._tools)})[/bold]\n"
                + "\n".join(f"  • {t}" for t in self._tools),
                id="help-content",
            ),
            id="help-container",
        )

    DEFAULT_CSS = """
    HelpScreen {
        align: center middle;
    }
    #help-container {
        background: $surface;
        border: solid $accent;
        padding: 2 4;
        width: 60;
        height: auto;
        max-height: 40;
    }
    #help-content {
        width: 100%;
    }
    """


# ---------------------------------------------------------------------------
# Model picker overlay
# ---------------------------------------------------------------------------

class ModelPickerScreen(ModalScreen):
    """
    Modal for selecting a model from the live API list.

    Dismisses with (model_name: str, save: bool) on selection, or None on cancel.
    """

    BINDINGS = [Binding("escape", "cancel_picker", "Cancel")]

    def __init__(self, client, current_model: str):
        super().__init__()
        self._client = client
        self._current_model = current_model
        self._models: list[str] = []

    def compose(self) -> ComposeResult:
        yield Container(
            Static("[bold cyan]Select Model[/bold cyan]", id="picker-title"),
            Static("[dim]Fetching models…[/dim]", id="picker-status"),
            ListView(id="model-list"),
            Checkbox("Save selection to config.json", id="save-checkbox"),
            Horizontal(
                Button("Select", variant="primary", id="btn-select"),
                Button("Cancel", variant="default", id="btn-cancel"),
                id="picker-buttons",
            ),
            id="picker-container",
        )

    def on_mount(self) -> None:
        self._load_models()

    @work(exclusive=True)
    async def _load_models(self) -> None:
        """Fetch models from the API and populate the list."""
        try:
            self._models = await self._client.fetch_models()
        except Exception as e:
            self.query_one("#picker-status", Static).update(f"[red]Error: {e}[/red]")
            return

        lv = self.query_one("#model-list", ListView)
        lv.clear()
        for m in self._models:
            marker = " [green]●[/green]" if m == self._current_model else ""
            lv.append(ListItem(Label(f"{m}{marker}", markup=True)))

        n = len(self._models)
        self.query_one("#picker-status", Static).update(
            f"[dim]{n} model{'s' if n != 1 else ''}  ·  ↑↓ to navigate[/dim]"
        )

        if self._current_model in self._models:
            lv.index = self._models.index(self._current_model)

    @on(Button.Pressed, "#btn-select")
    def handle_select(self) -> None:
        lv = self.query_one("#model-list", ListView)
        idx = lv.index
        if idx is None or not self._models or idx >= len(self._models):
            return
        self.dismiss((self._models[idx], self.query_one("#save-checkbox", Checkbox).value))

    @on(Button.Pressed, "#btn-cancel")
    def handle_cancel(self) -> None:
        self.dismiss(None)

    def action_cancel_picker(self) -> None:
        self.dismiss(None)

    DEFAULT_CSS = """
    ModelPickerScreen {
        align: center middle;
    }
    #picker-container {
        background: $surface;
        border: solid $accent;
        padding: 2 4;
        width: 72;
        height: auto;
        max-height: 32;
    }
    #picker-title {
        margin-bottom: 1;
    }
    #picker-status {
        margin-bottom: 1;
    }
    #model-list {
        height: 14;
        border: solid $primary 50%;
        margin-bottom: 1;
    }
    #save-checkbox {
        margin-bottom: 1;
    }
    #picker-buttons {
        height: auto;
        align: right middle;
    }
    #btn-select {
        margin-right: 1;
    }
    """


# ---------------------------------------------------------------------------
# Command palette provider — all agent commands for Ctrl+P
# ---------------------------------------------------------------------------

class _AgentCommands(Provider):
    """
    Surfaces all agent-specific commands in the Ctrl+P command palette.

    Items appear immediately when the palette is opened (empty query) and are
    filtered by fuzzy-match as the user types.
    """

    _ITEMS = [
        ("Change Model",       "action_pick_model",      "Switch to a different model (live list from API)"),
        ("Toggle Vision",      "action_toggle_vision",   "Turn screenshot vision on/off for vision-capable models"),
        ("Reset Conversation", "action_reset",            "Clear conversation history and start fresh"),
        ("Save History",       "action_save",             "Append conversation to logs/history.jsonl"),
        ("Toggle Tool Output", "action_toggle_tools",     "Show or hide tool call / result lines"),
        ("Help",               "action_help",             "Key bindings and registered tool list"),
    ]

    async def discover(self) -> Hits:
        """Show all commands when the palette opens with no query."""
        for label, action_name, help_text in self._ITEMS:
            yield Hit(1.0, label, getattr(self.app, action_name), help_text)

    async def search(self, query: str) -> Hits:
        """Filter commands by fuzzy match as the user types."""
        matcher = self.matcher(query)
        for label, action_name, help_text in self._ITEMS:
            score = matcher.match(label)
            if score > 0:
                yield Hit(score, matcher.highlight(label), getattr(self.app, action_name), help_text)


# ---------------------------------------------------------------------------
# Main TUI Application
# ---------------------------------------------------------------------------

class AgentTUI(App):
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
    # Agent execution — runs as a background worker, never blocks the UI
    # ------------------------------------------------------------------

    @work(exclusive=True, exit_on_error=False)
    async def _run_agent_work(self, user_input: str) -> None:
        log = self.query_one("#conversation", RichLog)
        input_widget = self.query_one("#input-bar", Input)

        self._log_session(f"USER: {user_input}")

        try:
            async for event in self._agent.run(user_input):
                if event.kind == "confirm_countdown":
                    remaining = event.data.get("remaining", 0)
                    total = event.data.get("total", remaining)
                    tool = event.data.get("tool_name", "tool")
                    bar = "█" * (total - remaining) + "░" * remaining
                    self._update_status(
                        f"[{bar}] {tool}: executing in {remaining}s… (Esc to cancel)"
                    )
                    continue

                if event.kind == "plan":
                    self._update_status("Planning…")
                    log.write(f"\n[bold cyan]Plan:[/bold cyan]\n[cyan]{event.content}[/cyan]")
                    self._log_session(f"PLAN: {event.content}")
                    continue

                iteration = event.data.get("iteration", "?")
                self._update_status(f"Running… iteration {iteration}")

                if event.kind == "text":
                    log.write(f"\n[bold white]Agent:[/bold white] {event.content}")
                    self._log_session(f"AGENT [{iteration}]: {event.content}")

                elif event.kind == "tool_call":
                    if self.show_tools:
                        log.write(f"[yellow]  ⚙ TOOL: {event.content}[/yellow]")
                    self._log_session(f"TOOL_CALL [{iteration}]: {event.content}")

                elif event.kind == "validation":
                    verdict = event.data.get("verdict", "")
                    if verdict.startswith("REJECTED"):
                        color, icon = "red", "✗"
                    elif verdict.startswith("CORRECTED"):
                        color, icon = "yellow", "⚡"
                    else:
                        color, icon = "dim green", "✓"
                    if self.show_tools:
                        log.write(f"[{color}]  {icon} VALIDATE: {event.content}[/{color}]")
                    self._log_session(f"VALIDATION [{iteration}]: {event.content}")

                elif event.kind == "tool_result":
                    if self.show_tools:
                        log.write(f"[dim green]  ✓ {event.content}[/dim green]")
                    self._log_session(f"TOOL_RESULT [{iteration}]: {event.content}")

                elif event.kind == "error":
                    log.write(f"[bold red]  ✗ ERROR: {event.content}[/bold red]")
                    self._log_session(f"ERROR [{iteration}]: {event.content}")

                elif event.kind == "done":
                    iters = event.data.get("iterations", "?")
                    reason = event.data.get("finish_reason", "done")
                    log.write(
                        f"\n[dim]─── done ({reason}, "
                        f"{iters} iteration{'s' if iters != 1 else ''}) ───[/dim]"
                    )
                    self._log_session(f"DONE: reason={reason} iterations={iters}")

        except asyncio.CancelledError:
            log.write("[dim]Task cancelled.[/dim]")
            self._log_session("CANCELLED by user")
            raise
        except Exception as e:
            print(f"[tui.py:_run_agent_work] {e}")
            traceback.print_exc()
            log.write(f"[bold red]Internal error: {e}[/bold red]")
            self._log_session(f"INTERNAL_ERROR: {e}")
        finally:
            self._active_task = None
            input_widget.disabled = False
            input_widget.focus()
            self._update_status("Ready")

    # ------------------------------------------------------------------
    # Actions (key bindings)
    # ------------------------------------------------------------------

    async def action_quit(self) -> None:
        if self._active_task and self._active_task.state in (WorkerState.PENDING, WorkerState.RUNNING):
            self._active_task.cancel()
        self.exit()

    async def action_reset(self) -> None:
        if self._active_task and self._active_task.state in (WorkerState.PENDING, WorkerState.RUNNING):
            self.query_one("#conversation", RichLog).write(
                "[yellow]Cannot reset while a task is running — press Escape first.[/yellow]"
            )
            return
        self._agent.reset()
        log = self.query_one("#conversation", RichLog)
        log.clear()
        log.write("[dim]Conversation reset.[/dim]")
        self._update_status("Ready — history cleared")

    async def action_save(self) -> None:
        try:
            self._history_file.parent.mkdir(parents=True, exist_ok=True)
            entry = {
                "timestamp": datetime.now().isoformat(),
                "messages": self._agent.history,
            }
            with self._history_file.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
            self.query_one("#conversation", RichLog).write(
                f"[dim]History saved to {self._history_file}[/dim]"
            )
        except Exception as e:
            print(f"[tui.py:action_save] {e}")
            traceback.print_exc()
            self.query_one("#conversation", RichLog).write(f"[red]Save failed: {e}[/red]")

    async def action_toggle_tools(self) -> None:
        self.show_tools = not self.show_tools
        state = "shown" if self.show_tools else "hidden"
        self.query_one("#conversation", RichLog).write(f"[dim]Tool output {state}.[/dim]")
        self.watch_status_text(self.status_text)

    async def action_toggle_vision(self) -> None:
        self._agent._vision_screenshots = not self._agent._vision_screenshots
        vision_on = self._agent._vision_screenshots
        state = "on" if vision_on else "off"
        log = self.query_one("#conversation", RichLog)
        log.write(
            f"[dim]Vision {state}. "
            + ("Model will receive screenshots as images." if vision_on
               else "Screenshots saved to disk only — not shown to model.")
            + "[/dim]"
        )
        ok = _tui_save_config(self._config_path, "agent", {"vision_screenshots": vision_on})
        if ok:
            log.write(f"[dim]Saved vision={state} to {self._config_path}.[/dim]")
        self.watch_status_text(self.status_text)

    async def action_cancel_task(self) -> None:
        if self._active_task and self._active_task.state in (WorkerState.PENDING, WorkerState.RUNNING):
            self._active_task.cancel()
            self.query_one("#conversation", RichLog).write("[dim]Cancelling…[/dim]")

    async def action_help(self) -> None:
        await self.push_screen(HelpScreen(self._tool_names))

    async def action_pick_model(self) -> None:
        """Open the model picker modal and apply the selection.

        Uses push_screen with a dismiss callback rather than push_screen_wait so
        that it can be triggered from any context (including the command palette),
        which does not run inside a Textual worker.
        """
        if self._active_task and self._active_task.state in (WorkerState.PENDING, WorkerState.RUNNING):
            self.query_one("#conversation", RichLog).write(
                "[yellow]Cannot change model while a task is running — press Escape first.[/yellow]"
            )
            return

        current_model = self._cfg.get("openwebui", {}).get("model", "")

        def _on_dismiss(result) -> None:
            if result is None:
                return
            model, save = result
            self._cfg.setdefault("openwebui", {})["model"] = model
            self._client.model = model
            log = self.query_one("#conversation", RichLog)
            log.write(f"[dim]Model changed to [green]{model}[/green].[/dim]")
            # Force-refresh status bar: reactive won't fire if status_text was
            # already "Ready" (value didn't change), so call the watcher directly.
            self.watch_status_text(self.status_text)
            if save:
                ok = _tui_save_config(self._config_path, "openwebui", {"model": model})
                msg = f"Saved to {self._config_path}" if ok else f"Could not save to {self._config_path}"
                color = "dim" if ok else "red"
                log.write(f"[{color}]{msg}[/{color}]")

        self.push_screen(
            ModelPickerScreen(client=self._client, current_model=current_model),
            _on_dismiss,
        )

    # ------------------------------------------------------------------
    # Session logging
    # ------------------------------------------------------------------

    def _log_session(self, line: str) -> None:
        if not self._session_log:
            return
        try:
            ts = datetime.now().strftime("%H:%M:%S")
            with self._session_log.open("a", encoding="utf-8") as f:
                f.write(f"[{ts}] {line}\n")
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Reactive watchers and helpers
    # ------------------------------------------------------------------

    def watch_status_text(self, value: str) -> None:
        try:
            bar = self.query_one("#status-bar", Static)
            model = self._cfg.get("openwebui", {}).get("model", "unknown")
            history_len = len(self._agent.history)
            vision = self._agent._vision_screenshots
            bar.update(
                f"{value}  │  model: [green]{model}[/green]  │  "
                f"history: {history_len}  │  "
                f"tools: {'on' if self.show_tools else 'off'}  │  "
                f"vision: {'[green]on[/green]' if vision else '[yellow]off[/yellow]'}"
            )
        except NoMatches:
            pass

    def _update_status(self, text: str) -> None:
        self.status_text = text
