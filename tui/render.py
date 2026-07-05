"""
tui.render — agent-event rendering into the conversation pane.

The background worker that runs the agent and renders its AgentEvent stream
(including streamed text deltas), session-log appends, the reactive status
bar watcher and the REST-API task event display.  Split out of the original
tui.py as a mixin of ``AgentTUI``.
"""

import asyncio
import logging
import traceback
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from textual import work
from textual.css.query import NoMatches
from textual.reactive import reactive
from textual.widgets import Input, RichLog, Static
from textual.worker import Worker

if TYPE_CHECKING:
    from textual.app import App

    _Base = App
else:
    _Base = object

logger = logging.getLogger(__name__)


class AgentTUIRenderMixin(_Base):
    """Agent-event rendering / status methods of ``AgentTUI``."""

    if TYPE_CHECKING:
        # Typing-only declarations of AgentTUI state used by these methods.
        status_text = reactive("Ready")
        show_tools = reactive(True)
        _active_task: Worker | None
        _agent: Any
        _cfg: dict
        _session_log: Path | None
        _stream_buffer: str
        _streamed_this_turn: bool

    # ------------------------------------------------------------------
    # Agent execution — runs as a background worker, never blocks the UI
    # ------------------------------------------------------------------

    @work(exclusive=True, exit_on_error=False)
    async def _run_agent_work(self, user_input: str) -> None:
        log = self.query_one("#conversation", RichLog)
        input_widget = self.query_one("#input-bar", Input)

        self._log_session(f"USER: {user_input}")

        # Reset streamed-text state so leftovers from an aborted run never
        # bleed into this one.
        self._stream_buffer = ""
        self._streamed_this_turn = False

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

                if event.kind == "text_delta":
                    # Live streamed fragment — append to the output pane at
                    # line granularity (RichLog is append-only, so a partial
                    # line stays buffered until its newline arrives or the
                    # final "text" event flushes it).
                    if not self._streamed_this_turn:
                        self._streamed_this_turn = True
                        log.write("\n[bold white]Agent:[/bold white]")
                    self._stream_buffer += event.content
                    while "\n" in self._stream_buffer:
                        line, self._stream_buffer = self._stream_buffer.split("\n", 1)
                        log.write(line)
                    continue

                iteration = event.data.get("iteration", "?")
                self._update_status(f"Running… iteration {iteration}")

                if event.kind == "text":
                    if self._streamed_this_turn:
                        # The text already streamed into the pane via
                        # text_delta events — just flush any partial line
                        # instead of repeating the full message.
                        if self._stream_buffer:
                            log.write(self._stream_buffer)
                        self._stream_buffer = ""
                        self._streamed_this_turn = False
                    else:
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

            if kind == "text_delta":
                # Streamed fragments are too granular for the API bridge's
                # per-line rendering; the final "text" event carries the
                # complete message.
                return
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
