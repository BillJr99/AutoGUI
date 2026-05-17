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
import sys
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
# Config persistence helpers (local copy avoids circular import with main.py)
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


def _tui_set_nested_config(config_path: str, dot_path: str, value) -> bool:
    """Set a single value at *dot_path* (e.g. "agent.controller.enabled") in config_path."""
    try:
        p = Path(config_path)
        data = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
        parts = dot_path.split(".")
        node = data
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value
        p.write_text(json.dumps(data, indent=2), encoding="utf-8")
        return True
    except Exception as e:
        logger.warning("[tui.py:_tui_set_nested_config] %s", e)
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
# Generic single-value input modal (used by command palette config editing)
# ---------------------------------------------------------------------------

class _InputModal(ModalScreen):
    """Prompt the user to edit a single config value."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, title: str, label: str, current_value: str):
        super().__init__()
        self._title = title
        self._label = label
        self._current = current_value

    def compose(self) -> ComposeResult:
        yield Container(
            Static(f"[bold cyan]{self._title}[/bold cyan]", id="im-title"),
            Static(self._label, id="im-label"),
            Input(value=self._current, id="im-input"),
            Horizontal(
                Button("Save", variant="primary", id="im-save"),
                Button("Cancel", variant="default", id="im-cancel"),
                id="im-buttons",
            ),
            id="im-container",
        )

    def on_mount(self) -> None:
        self.query_one("#im-input", Input).focus()

    @on(Button.Pressed, "#im-save")
    def handle_save(self) -> None:
        self.dismiss(self.query_one("#im-input", Input).value)

    @on(Button.Pressed, "#im-cancel")
    def handle_cancel(self) -> None:
        self.dismiss(None)

    @on(Input.Submitted)
    def handle_submit(self) -> None:
        self.dismiss(self.query_one("#im-input", Input).value)

    def action_cancel(self) -> None:
        self.dismiss(None)

    DEFAULT_CSS = """
    _InputModal {
        align: center middle;
    }
    #im-container {
        background: $surface;
        border: solid $accent;
        padding: 2 4;
        width: 70;
        height: auto;
    }
    #im-title {
        margin-bottom: 1;
    }
    #im-label {
        margin-bottom: 1;
        color: $text-muted;
    }
    #im-input {
        margin-bottom: 1;
    }
    #im-buttons {
        height: auto;
        align: right middle;
    }
    #im-save {
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
    filtered by fuzzy-match as the user types.  All config.json settings are
    exposed here so every tuneable parameter can be changed without editing
    the file by hand.
    """

    def _build_items(self):
        """Build the full command list at call time so lambdas capture live app state."""
        app = self.app

        def toggle(dot_path, label, help_text):
            return (label, lambda: app._action_toggle_cfg(dot_path, label), help_text)

        def edit(dot_path, label, help_text):
            return (label, lambda: app._action_edit_cfg(dot_path, label), help_text)

        def pick(dot_path, label, choices, help_text):
            return (label, lambda: app._action_pick_cfg(dot_path, label, choices), help_text)

        return [
            # ── Core session actions ──────────────────────────────────────
            ("Change Model",       app.action_pick_model,    "Switch to a different model (live list from API)"),
            ("Toggle Vision",      app.action_toggle_vision, "Turn screenshot vision on/off for vision-capable models"),
            ("Reset Conversation", app.action_reset,          "Clear conversation history and start fresh"),
            ("Save History",       app.action_save,           "Append conversation to logs/history.jsonl"),
            ("Toggle Tool Output", app.action_toggle_tools,   "Show or hide tool call / result lines"),
            ("Help",               app.action_help,           "Key bindings and registered tool list"),
            # ── openwebui ────────────────────────────────────────────────
            edit("openwebui.temperature",    "Set Temperature",       "LLM sampling temperature (0.0–2.0)"),
            edit("openwebui.max_tokens",     "Set Max Tokens",        "Maximum tokens per response"),
            edit("openwebui.timeout_seconds","Set Request Timeout",   "API request timeout in seconds"),
            edit("openwebui.base_url",       "Set API Base URL",      "OpenWebUI server base URL"),
            edit("openwebui.api_path",       "Set API Path",          "Completions endpoint path"),
            # ── agent (top-level) ────────────────────────────────────────
            edit("agent.max_iterations",    "Set Max Iterations",     "Maximum agent iterations per task"),
            toggle("agent.skills_enabled",  "Toggle Skills Recording","Allow the agent to save new skills to disk"),
            toggle("agent.suggest_skills",  "Toggle Suggest Skills",  "Show skill suggestions at task start"),
            toggle("agent.record_trace",    "Toggle Trace Recording", "Record step-by-step execution traces"),
            edit("agent.trace_dir",         "Set Trace Directory",    "Directory where traces are written"),
            edit("agent.skills_path",       "Set Skills Path",        "Path to the skills JSONL file"),
            # ── agent.planner ────────────────────────────────────────────
            toggle("agent.planner.enabled", "Toggle Planner",         "Enable pre-execution planning pass"),
            # ── agent.controller ────────────────────────────────────────
            toggle("agent.controller.enabled",              "Toggle Controller",             "Step-by-step executor (off = legacy ReAct loop)"),
            edit("agent.controller.step_max_iterations",    "Set Step Max Iterations",       "Max iterations allowed per controller step"),
            edit("agent.controller.step_max_retries",       "Set Step Max Retries",          "Max retries before a step is marked failed"),
            toggle("agent.controller.auto_resume",          "Toggle Controller Auto-Resume", "Automatically resume after a step failure"),
            toggle("agent.controller.replan_on_block",      "Toggle Replan on Block",        "Replan when the controller is stuck"),
            toggle("agent.controller.critique_enabled",     "Toggle Plan Critique",          "LLM reviews the plan before execution"),
            toggle("agent.controller.preflight_enabled",    "Toggle Preflight Checks",       "Verify resource availability before UI actions"),
            toggle("agent.controller.predicate_check_enabled", "Toggle Predicate Checks",    "Verify typed post-conditions after each step"),
            toggle("agent.controller.visual_diff_enabled",  "Toggle Visual Diff",            "Flag steps whose screen pixels barely changed"),
            edit("agent.controller.watchdog_stall_threshold","Set Watchdog Stall Threshold", "Iterations before flagging a step as stuck (0=off)"),
            # ── agent.bon ────────────────────────────────────────────────
            toggle("agent.bon.enabled",                          "Toggle Best-of-N Sampling",          "Sample N completions and pick the best on uncertain steps"),
            edit("agent.bon.n",                                  "Set BoN Sample Count",               "Number of completions to sample for best-of-N"),
            edit("agent.bon.temperature",                        "Set BoN Temperature",                "Temperature used for best-of-N sampling"),
            toggle("agent.bon.trigger_on_recent_failure",        "Toggle BoN on Recent Failure",        "Trigger best-of-N after a recent step failure"),
            toggle("agent.bon.trigger_on_validator_disagreement","Toggle BoN on Validator Disagreement","Trigger best-of-N when the validator disagrees"),
            # ── agent.budget ─────────────────────────────────────────────
            edit("agent.budget.max_tool_calls",  "Set Budget: Max Tool Calls",  "Hard ceiling on tool calls per task (0=unlimited)"),
            edit("agent.budget.max_chat_calls",  "Set Budget: Max Chat Calls",  "Hard ceiling on LLM calls per task (0=unlimited)"),
            edit("agent.budget.max_total_tokens","Set Budget: Max Total Tokens","Hard ceiling on tokens per task (0=unlimited)"),
            edit("agent.budget.max_seconds",     "Set Budget: Max Seconds",     "Hard ceiling on wall-clock seconds per task (0=unlimited)"),
            # ── agent.memory ─────────────────────────────────────────────
            toggle("agent.memory.enabled", "Toggle Memory Recording", "Allow recording app failure/success memory"),
            edit("agent.memory.dir",       "Set Memory Directory",    "Directory for app memory files"),
            # ── agent.subagent ───────────────────────────────────────────
            toggle("agent.subagent.enabled",       "Toggle Sub-agent",              "Enable read-only subagent for lookup questions"),
            edit("agent.subagent.max_tool_calls",  "Set Sub-agent Max Tool Calls",  "Maximum tool calls the subagent may make"),
            # ── agent.drift_anchor ───────────────────────────────────────
            toggle("agent.drift_anchor.enabled",      "Toggle Drift Anchor",       "Capture world snapshot after each step"),
            toggle("agent.drift_anchor.capture_phash","Toggle Drift Anchor pHash", "Include screenshot perceptual hash in drift anchor"),
            # ── agent.screen_record ──────────────────────────────────────
            toggle("agent.screen_record.enabled",  "Toggle Screen Recording",      "Record rolling screen buffer; flush GIF on failure"),
            edit("agent.screen_record.fps",         "Set Screen Record FPS",        "Frame rate for the screen recording buffer"),
            edit("agent.screen_record.buffer_seconds","Set Screen Record Buffer",   "Rolling buffer duration in seconds"),
            edit("agent.screen_record.max_width",   "Set Screen Record Max Width",  "Maximum pixel width of recorded frames"),
            edit("agent.screen_record.out_dir",     "Set Screen Record Output Dir", "Directory for failure GIFs"),
            # ── agent.artifacts / progress ──────────────────────────────
            edit("agent.artifacts.dir", "Set Artifacts Directory", "Directory for large-observation artifact store"),
            edit("agent.progress.dir",  "Set Progress Directory",  "Directory for task progress markers"),
            # ── tools ────────────────────────────────────────────────────
            toggle("tools.allowed_shell",      "Toggle Shell Tools",        "Enable shell command execution tools"),
            toggle("tools.allowed_filesystem", "Toggle Filesystem Tools",   "Enable filesystem read/write tools"),
            toggle("tools.allowed_desktop",    "Toggle Desktop Tools",      "Enable mouse/keyboard/screenshot tools"),
            toggle("tools.allowed_browser",    "Toggle Browser Tools",      "Enable Playwright browser automation tools"),
            edit("tools.shell_timeout_seconds","Set Shell Timeout",         "Timeout in seconds for shell commands"),
            edit("tools.max_screenshot_width", "Set Max Screenshot Width",  "Resize screenshots to this pixel width before sending"),
            edit("tools.screenshot_dir",       "Set Screenshot Directory",  "Directory where screenshots are saved"),
            edit("tools.perception_cache_ttl_seconds","Set Perception Cache TTL","Seconds to cache OCR/SOM perception results"),
            # ── browser ──────────────────────────────────────────────────
            toggle("browser.headless",        "Toggle Headless Browser",   "Run the browser without a visible window"),
            edit("browser.screenshot_dir",    "Set Browser Screenshot Dir","Directory for browser screenshots"),
            edit("browser.user_data_dir",     "Set Browser User Data Dir", "Browser profile/user-data directory"),
            # ── logging ──────────────────────────────────────────────────
            pick("logging.level",  "Set Log Level",        ["DEBUG", "INFO", "WARNING", "ERROR"], "Logging verbosity level"),
            edit("logging.file",   "Set Log File",         "Path to the rotating log file"),
            edit("logging.max_bytes",    "Set Log Max Bytes",   "Maximum size of the log file before rotation"),
            edit("logging.backup_count", "Set Log Backup Count","Number of rotated log files to keep"),
            # ── tui ──────────────────────────────────────────────────────
            edit("tui.history_file",     "Set History File",      "Path for the conversation history JSONL"),
            edit("tui.theme",            "Set TUI Theme",         "Textual theme name (e.g. dark, light, nord)"),
            # ── safety ───────────────────────────────────────────────────
            edit("safety.command_confirm_delay_seconds","Set Command Confirm Delay","Seconds to pause before executing each tool (0=off)"),
            toggle("safety.dry_run",       "Toggle Dry Run",          "Simulate tool calls without actually executing them"),
            edit("safety.fs_write_snapshot_dir","Set FS Snapshot Dir","Directory for filesystem write snapshots (empty=off)"),
            # ── screen_observer ──────────────────────────────────────────
            toggle("screen_observer.enabled",         "Toggle Screen Observer",        "Enable OSScreenObserver integration"),
            edit("screen_observer.base_url",          "Set Screen Observer URL",       "OSScreenObserver server base URL"),
            edit("screen_observer.timeout_seconds",   "Set Screen Observer Timeout",   "Screen Observer request timeout in seconds"),
            # ── top-level ────────────────────────────────────────────────
            toggle("install_dependencies", "Toggle Install Dependencies on Startup","Run the install script at each startup"),
            edit("prompts_dir",            "Set Prompts Directory",                 "Directory containing prompt template files"),
        ]

    async def discover(self) -> Hits:
        """Show all commands when the palette opens with no query."""
        for label, action_fn, help_text in self._build_items():
            yield Hit(1.0, label, action_fn, help_text)

    async def search(self, query: str) -> Hits:
        """Filter commands by fuzzy match as the user types."""
        matcher = self.matcher(query)
        for label, action_fn, help_text in self._build_items():
            score = matcher.match(label)
            if score > 0:
                yield Hit(score, matcher.highlight(label), action_fn, help_text)


# ---------------------------------------------------------------------------
# Logging bridge
# ---------------------------------------------------------------------------

class _TUILogHandler(logging.Handler):
    """Logging handler that writes records into the TUI's conversation
    pane via Textual's thread-safe ``call_from_thread``.

    main.py installs a stderr StreamHandler at WARNING; under the TUI
    that paints raw ``[WARNING] …`` lines over the layout.  We attach
    this handler on mount and detach the stderr handler so warnings
    coming from our own code or from libraries (urllib3 retry, asyncio,
    pyautogui fail-safe, etc.) land in the visible conversation log
    instead.
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
        # Logging handler that routes WARNING+ records into the
        # conversation pane so library warnings (urllib3, asyncio, etc.)
        # don't paint over the TUI layout.  Installed on mount, removed
        # on unmount.  See _install_log_handler for details.
        self._log_handler: logging.Handler | None = None
        # stderr/stdout StreamHandlers that were attached to the root
        # logger before mount; we detach them on install (so they don't
        # paint the terminal) and re-attach them on uninstall so the
        # process's logging state is restored when the TUI exits.
        self._displaced_handlers: list[tuple[logging.Logger, logging.Handler]] = []

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

                # ---- Controller-specific events ------------------------
                # Without these handlers the controller path appears to
                # do nothing after the plan is shown — the step loop is
                # actually running but its events drop into the void
                # because the if/elif chain below only knew about the
                # legacy executor's events.  Each branch logs to the
                # session file too so the trace stays informative.
                if event.kind == "preflight":
                    failures = event.data.get("results") or []
                    if not event.data.get("all_passed", True):
                        log.write(f"[bold red]  ✗ PREFLIGHT FAILED:[/bold red] {event.content}")
                        # PreflightReport.to_dict flattens kind/target/ok/
                        # detail onto each result entry — they are NOT
                        # nested under a "check" key, so reading
                        # `r.get("check", {}).get(...)` would always
                        # produce "?=?" placeholders instead of the
                        # actual "tool=foo" diagnosis.
                        for r in failures:
                            if not r.get("ok", True):
                                log.write(
                                    f"[red]      - {r.get('kind','?')}="
                                    f"{r.get('target','?')}: "
                                    f"{r.get('detail','')}[/red]"
                                )
                    else:
                        log.write(f"[dim green]  ✓ Preflight: {event.content}[/dim green]")
                    self._log_session(f"PREFLIGHT: {event.content}")
                    continue

                if event.kind == "plan_critique":
                    log.write(f"[yellow]  ⚠ Critique: {event.content}[/yellow]")
                    self._log_session(f"CRITIQUE: {event.content}")
                    continue

                if event.kind == "plan_revised":
                    log.write(f"\n[bold cyan]Plan revised:[/bold cyan]\n[cyan]{event.content}[/cyan]")
                    self._log_session(f"PLAN_REVISED: {event.content}")
                    continue

                if event.kind == "step_start":
                    step_id = (event.data.get("step") or {}).get("id", "?")
                    self._update_status(f"Running step {step_id}…")
                    log.write(f"\n[bold magenta]{event.content}[/bold magenta]")
                    self._log_session(f"STEP_START: {event.content}")
                    continue

                if event.kind == "step_done":
                    log.write(f"[green]  ✓ {event.content}[/green]")
                    self._log_session(f"STEP_DONE: {event.content}")
                    continue

                if event.kind == "predicate":
                    ok = event.data.get("ok", True)
                    colour = "dim green" if ok else "yellow"
                    icon = "✓" if ok else "✗"
                    log.write(f"[{colour}]  {icon} predicate: {event.content}[/{colour}]")
                    self._log_session(f"PREDICATE: {event.content}")
                    continue

                if event.kind == "step_failure":
                    log.write(f"[yellow]  ✗ {event.content}[/yellow]")
                    self._log_session(f"STEP_FAIL: {event.content}")
                    continue

                if event.kind == "step_escalate":
                    log.write(f"[bold red]  ⚠ Step escalated to user: {event.content}[/bold red]")
                    self._log_session(f"STEP_ESCALATE: {event.content}")
                    continue

                if event.kind == "budget_exceeded":
                    log.write(f"[bold red]  ⚠ Budget exceeded: {event.content}[/bold red]")
                    self._log_session(f"BUDGET_EXCEEDED: {event.content}")
                    continue

                if event.kind == "failure_recording" or event.kind == "state_diff":
                    # Diagnostic-level; only show when the user has tools
                    # turned on, mirroring tool_call / tool_result.
                    if self.show_tools:
                        log.write(f"[dim]  • {event.kind}: {event.content}[/dim]")
                    self._log_session(f"{event.kind.upper()}: {event.content}")
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
        self._uninstall_log_handler()
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

    # ------------------------------------------------------------------
    # Generic config helpers used by the command palette
    # ------------------------------------------------------------------

    def _cfg_get(self, dot_path: str):
        """Return the value at *dot_path* from the in-memory config dict."""
        parts = dot_path.split(".")
        node = self._cfg
        for part in parts:
            if not isinstance(node, dict):
                return None
            node = node.get(part)
        return node

    def _cfg_set(self, dot_path: str, value) -> bool:
        """Set *value* at *dot_path* in the in-memory config and persist to disk."""
        parts = dot_path.split(".")
        node = self._cfg
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value
        return _tui_set_nested_config(self._config_path, dot_path, value)

    async def _action_toggle_cfg(self, dot_path: str, name: str) -> None:
        """Toggle a boolean config value and persist it."""
        current = self._cfg_get(dot_path)
        new_val = not bool(current)
        ok = self._cfg_set(dot_path, new_val)
        state = "on" if new_val else "off"
        log = self.query_one("#conversation", RichLog)
        msg = f"Saved to {self._config_path}" if ok else f"Could not save to {self._config_path}"
        log.write(f"[dim]{name}: {state}. {msg}.[/dim]")
        self.watch_status_text(self.status_text)

    async def _action_edit_cfg(self, dot_path: str, name: str) -> None:
        """Open an input modal to change a config value and persist it."""
        current = self._cfg_get(dot_path)
        current_str = "" if current is None else str(current)
        original_type = type(current) if current is not None else str

        def _on_dismiss(result) -> None:
            if result is None:
                return
            # Coerce back to the original type so ints stay ints, etc.
            if original_type is bool:
                new_val = result.strip().lower() in ("true", "1", "yes", "on")
            elif original_type is int:
                try:
                    new_val = int(result)
                except ValueError:
                    self.query_one("#conversation", RichLog).write(
                        f"[red]{name}: '{result}' is not a valid integer.[/red]"
                    )
                    return
            elif original_type is float:
                try:
                    new_val = float(result)
                except ValueError:
                    self.query_one("#conversation", RichLog).write(
                        f"[red]{name}: '{result}' is not a valid number.[/red]"
                    )
                    return
            else:
                new_val = result

            ok = self._cfg_set(dot_path, new_val)
            log = self.query_one("#conversation", RichLog)
            msg = f"Saved to {self._config_path}" if ok else f"Could not save to {self._config_path}"
            log.write(f"[dim]{name} → [green]{new_val}[/green]. {msg}.[/dim]")
            self.watch_status_text(self.status_text)

        self.push_screen(
            _InputModal(title=name, label=f"Current value: {current_str}", current_value=current_str),
            _on_dismiss,
        )

    async def _action_pick_cfg(self, dot_path: str, name: str, choices: list) -> None:
        """Open an input modal that lists fixed choices for a config value."""
        current = self._cfg_get(dot_path)
        current_str = "" if current is None else str(current)
        choice_hint = "  ".join(f"[{c}]" if c == current_str else c for c in choices)

        def _on_dismiss(result) -> None:
            if result is None:
                return
            ok = self._cfg_set(dot_path, result)
            log = self.query_one("#conversation", RichLog)
            msg = f"Saved to {self._config_path}" if ok else f"Could not save to {self._config_path}"
            log.write(f"[dim]{name} → [green]{result}[/green]. {msg}.[/dim]")
            self.watch_status_text(self.status_text)

        self.push_screen(
            _InputModal(
                title=name,
                label=f"Choices: {choice_hint}\nCurrent value: {current_str}",
                current_value=current_str,
            ),
            _on_dismiss,
        )

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
    # Logging bridge — installed on mount so warnings land in the
    # conversation pane instead of corrupting the TUI layout.
    # ------------------------------------------------------------------

    def _install_log_handler(self) -> None:
        if self._log_handler is not None:
            return
        # Drop stderr/stdout StreamHandlers from the root logger AND from
        # uvicorn's own named loggers — those write raw lines straight to
        # the terminal and paint over the Textual layout.  File handlers
        # stay so logs are persisted to disk.  Each removed handler is
        # remembered so _uninstall_log_handler can restore them on exit.
        _named = ["uvicorn", "uvicorn.access", "uvicorn.error"]
        for lgr in [logging.getLogger()] + [logging.getLogger(n) for n in _named]:
            for h in list(lgr.handlers):
                if isinstance(h, logging.StreamHandler) and not isinstance(
                    h, logging.FileHandler,
                ) and h.stream in (sys.stderr, sys.stdout):
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

    # ------------------------------------------------------------------
    # REST API task event display
    # ------------------------------------------------------------------

    def _on_api_task_event(self, task_id: str, step: dict) -> None:
        """Called from the REST API thread when a background task emits an event."""
        try:
            self.call_from_thread(self._display_api_task_event, task_id, step)
        except Exception:
            pass

    def _display_api_task_event(self, task_id: str, step: dict) -> None:
        """Render a REST API task event in the conversation pane (runs on TUI thread)."""
        try:
            log = self.query_one("#conversation", RichLog)
            kind = step.get("kind", "")
            content = step.get("content", "")
            seq = step.get("seq", -1)
            short_id = task_id[:8]

            if seq == 0:
                log.write(f"\n[bold cyan]⟳ API task [{short_id}…]:[/bold cyan]")

            if kind == "plan":
                log.write(f"[cyan]  Plan: {content}[/cyan]")
            elif kind == "text":
                log.write(f"[white]  Agent: {content}[/white]")
            elif kind == "tool_call" and self.show_tools:
                log.write(f"[yellow]  ⚙ {content}[/yellow]")
            elif kind == "tool_result" and self.show_tools:
                log.write(f"[dim green]  ✓ {content}[/dim green]")
            elif kind == "error":
                log.write(f"[red]  ✗ ERROR: {content}[/red]")
                self._update_status("API task error")
            elif kind == "done":
                data = step.get("data") or {}
                iters = data.get("iterations", "?")
                reason = data.get("finish_reason", "done")
                log.write(f"[dim]  ─── API task done ({reason}, {iters} iterations) ───[/dim]")
                self._update_status("Ready")
        except Exception:
            pass
