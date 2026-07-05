"""
tui.actions — key-binding and command-palette actions of the AgentTUI.

The action_* handlers (quit/reset/save/toggles/model picker/task cancel)
plus the config get/set helpers they persist through.  Split out of the
original tui.py as a mixin of ``AgentTUI``.
"""

import json
import logging
import traceback
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from textual.reactive import reactive
from textual.widgets import RichLog
from textual.worker import Worker, WorkerState

from .screens import HelpScreen, ModelPickerScreen, _InputModal

if TYPE_CHECKING:
    from textual.app import App

    _Base = App
else:
    _Base = object

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


class AgentTUIActionsMixin(_Base):
    """Action methods of ``AgentTUI`` (key bindings + command palette)."""

    if TYPE_CHECKING:
        # Typing-only declarations of AgentTUI state used by these methods.
        status_text = reactive("Ready")
        show_tools = reactive(True)
        _active_task: Worker | None
        _agent: Any
        _client: Any
        _cfg: dict
        _config_path: str
        _history_file: Path
        _tool_names: list[str]

        # Provided by AgentTUIRenderMixin / AgentTUI.
        def _update_status(self, text: str) -> None: ...
        def watch_status_text(self, value: str) -> None: ...
        def _uninstall_log_handler(self) -> None: ...

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
        node: Any = self._cfg
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
            new_val: Any
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
