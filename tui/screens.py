"""
tui.screens — modal overlay screens.

The help overlay, the model picker and the generic single-value input modal
used by command-palette config editing.  Split out of the original tui.py.
"""

import logging

from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Input, Label, ListItem, ListView, Static

logger = logging.getLogger(__name__)


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
