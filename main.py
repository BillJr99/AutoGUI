"""
main.py — Entry point for the OpenWebUI Desktop Agent.

Usage modes
-----------

1. Single-command (non-interactive):
   python main.py "Open a terminal and list the files in my home directory"

   The agent processes the task, prints all events to stdout, and exits.
   Suitable for scripting and cron-style automation.

2. Interactive TUI:
   python main.py

   Launches the Textual-based TUI for a full interactive session.

3. Flags:
   --config PATH        Path to config.json (default: config.json in CWD)
   --model MODEL        Override the model name from config
   --no-desktop         Disable desktop tools for this session
   --no-shell           Disable shell tools for this session (safer)
   --verbose            Set log level to DEBUG
   --check              Run a connectivity health check and exit

Configuration
-------------
All runtime parameters are externalized in config.json.  Command-line flags
override config values for the current session only; they do not write back
to the config file.

Logging
-------
Logs are written to the file path specified in config["logging"]["file"]
(default: logs/agent.log) and to stderr at WARNING level or above, unless
--verbose is specified (DEBUG level).
"""

import argparse
import asyncio
import json
import platform
import sys
import traceback

# Suppress the noisy "Exception ignored in: BaseSubprocessTransport.__del__"
# traceback that asyncio emits on Ctrl+C when the event loop closes while
# subprocess transports are still alive.  The process exits correctly; this
# is purely cosmetic noise.
_orig_unraisablehook = sys.unraisablehook


def _quiet_unraisablehook(unraisable):
    if (
        isinstance(unraisable.exc_value, RuntimeError)
        and "Event loop is closed" in str(unraisable.exc_value)
    ):
        return
    _orig_unraisablehook(unraisable)


sys.unraisablehook = _quiet_unraisablehook

# These imports intentionally come after the unraisablehook install above so
# any import-time subprocess/asyncio noise is already suppressed.
from agent import Agent  # noqa: E402
from bootstrap import (  # noqa: E402
    _start_api_background,
    _suppress_windows_proactor_resource_warnings,
    build_components,
    run_health_check,
    setup_logging,
)
from config_load import (  # noqa: E402
    apply_cli_overrides,
    load_config,
    validate_and_configure,
)

# ---------------------------------------------------------------------------
# Single-command (non-interactive) mode
# ---------------------------------------------------------------------------

async def _escape_watcher(target_task: asyncio.Task) -> None:
    """
    Watch stdin for an Escape key press and cancel target_task if found.
    Uses platform-native non-blocking key detection; silently exits on any error
    or when the target task finishes on its own.
    """
    if not sys.stdin.isatty():
        return
    try:
        if platform.system() == "Windows":
            import msvcrt
            while not target_task.done():
                if msvcrt.kbhit():  # type: ignore[attr-defined]
                    ch = msvcrt.getch()  # type: ignore[attr-defined]
                    if ch == b"\x1b":
                        target_task.cancel()
                        return
                await asyncio.sleep(0.05)
        else:
            import select
            import termios
            import tty
            fd = sys.stdin.fileno()
            old_settings = termios.tcgetattr(fd)
            try:
                tty.setcbreak(fd)
                while not target_task.done():
                    r, _, _ = select.select([sys.stdin], [], [], 0.05)
                    if r:
                        ch = sys.stdin.read(1)
                        if ch == "\x1b":
                            target_task.cancel()
                            return
            finally:
                termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
    except Exception:
        pass


async def _consume_agent_events(
    agent: Agent,
    command: str,
    verbose_tools: bool,
    use_color: bool,
) -> None:
    """Inner coroutine: iterate agent events and print them."""
    cyan   = "\033[96m"  if use_color else ""
    white  = "\033[97m"  if use_color else ""
    yellow = "\033[93m"  if use_color else ""
    green  = "\033[92m"  if use_color else ""
    red    = "\033[91m"  if use_color else ""
    dim    = "\033[2m"   if use_color else ""
    reset  = "\033[0m"   if use_color else ""

    _last_was_countdown = False

    async for event in agent.run(command):
        if event.kind == "plan":
            print(f"{cyan}Plan:{reset}\n{event.content}\n")

        elif event.kind == "text":
            if _last_was_countdown:
                print()
                _last_was_countdown = False
            print(f"{white}Agent:{reset} {event.content}\n")

        elif event.kind == "tool_call" and verbose_tools:
            print(f"{yellow}  ⚙ TOOL: {event.content}{reset}")

        elif event.kind == "confirm_countdown" and verbose_tools:
            remaining = event.data.get("remaining", 0)
            total = event.data.get("total", remaining)
            tool = event.data.get("tool_name", "tool")
            # Print countdown on a single overwritten line.
            bar = "█" * (total - remaining) + "░" * remaining
            print(
                f"\r{yellow}  ⏳ [{bar}] {tool}: executing in {remaining}s"
                f"  (Esc / Ctrl+C to cancel){reset}  ",
                end="",
                flush=True,
            )
            _last_was_countdown = True
            if remaining == 1:
                print()
                _last_was_countdown = False

        elif event.kind == "tool_result" and verbose_tools:
            print(f"{dim}{green}  ✓ {event.content}{reset}")

        elif event.kind == "error":
            if _last_was_countdown:
                print()
                _last_was_countdown = False
            print(f"{red}  ✗ ERROR: {event.content}{reset}", file=sys.stderr)

        elif event.kind == "done":
            if _last_was_countdown:
                print()
                _last_was_countdown = False
            iters = event.data.get("iterations", "?")
            reason = event.data.get("finish_reason", "done")
            print(f"{dim}─── done ({reason}, {iters} iteration(s)) ───{reset}")


async def run_single_command(
    agent: Agent,
    command: str,
    verbose_tools: bool = True,
    confirm_delay: int = 0,
) -> None:
    """
    Execute a single agent task and print all events to stdout.

    When confirm_delay > 0, the agent pauses before each tool execution and an
    Escape-key watcher runs concurrently so the user can abort the pending call.

    Parameters
    ----------
    agent : Agent
        Initialized agent.
    command : str
        Task string supplied on the command line.
    verbose_tools : bool
        Whether to print tool_call / tool_result / countdown events.
    confirm_delay : int
        Seconds the agent waits before each tool dispatch (from config).
    """
    import os
    use_color = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None
    cyan  = "\033[96m" if use_color else ""
    dim   = "\033[2m"  if use_color else ""
    reset = "\033[0m"  if use_color else ""

    print(f"{cyan}You:{reset} {command}\n")

    agent_task = asyncio.create_task(
        _consume_agent_events(agent, command, verbose_tools, use_color)
    )

    # Only watch for Escape when there is a countdown to interrupt.
    escape_task: asyncio.Task | None = None
    if confirm_delay > 0:
        escape_task = asyncio.create_task(_escape_watcher(agent_task))

    try:
        await agent_task
    except asyncio.CancelledError:
        print(f"\n{dim}Cancelled.{reset}")
    except Exception as e:
        print(f"[main.py:run_single_command] Fatal error: {e}", file=sys.stderr)
        traceback.print_exc()
        sys.exit(1)
    finally:
        if escape_task and not escape_task.done():
            escape_task.cancel()
            try:
                await escape_task
            except asyncio.CancelledError:
                pass


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="owui-agent",
        description=(
            "OpenWebUI Desktop Agent: an agentic CLI/TUI powered by any "
            "OpenWebUI-hosted LLM with shell, filesystem, and desktop control."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples
--------
  # Interactive TUI session
  python main.py

  # Single command
  python main.py "list all Python files in ~/projects"

  # Single command without desktop tools, verbose
  python main.py --no-desktop --verbose "show disk usage for /var"

  # Health check
  python main.py --check

  # Use a different config file
  python main.py --config /path/to/my_config.json
        """,
    )
    parser.add_argument(
        "command",
        nargs="?",
        default=None,
        help="Task to execute in non-interactive mode. Omit to launch the TUI.",
    )
    parser.add_argument(
        "--config",
        default="config.json",
        metavar="PATH",
        help="Path to config.json (default: config.json in current directory).",
    )
    parser.add_argument(
        "--model",
        default=None,
        metavar="MODEL",
        help="Override the model name from config (e.g. mistral:7b).",
    )
    parser.add_argument(
        "--no-desktop",
        action="store_true",
        help="Disable desktop (mouse/keyboard/screenshot) tools for this session.",
    )
    parser.add_argument(
        "--no-shell",
        action="store_true",
        help="Disable shell execution tools for this session.",
    )
    parser.add_argument(
        "--no-tools",
        action="store_true",
        help="Disable both shell and desktop tools (pure chat mode).",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable DEBUG-level logging to stderr and log file.",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress tool_call and tool_result output in single-command mode.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Run a connectivity health check and exit.",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    _suppress_windows_proactor_resource_warnings()

    # -- Load and patch configuration -----------------------------------
    try:
        cfg = load_config(args.config)
    except FileNotFoundError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)
    except json.JSONDecodeError as e:
        print(f"Invalid JSON in config file: {e}", file=sys.stderr)
        sys.exit(1)

    if args.no_tools:
        args.no_desktop = True
        args.no_shell = True

    apply_cli_overrides(cfg, args)
    setup_logging(cfg, verbose=args.verbose)

    # -- Start background REST API server (after logging is configured) ---
    _start_api_background()

    asyncio.run(validate_and_configure(cfg, args.config))

    # -- Build components -----------------------------------------------
    try:
        client, registry, agent = build_components(cfg)
    except Exception as e:
        print(f"[main.py:main] Failed to initialize components: {e}", file=sys.stderr)
        traceback.print_exc()
        sys.exit(1)

    # -- Health check ---------------------------------------------------
    if args.check:
        asyncio.run(run_health_check(client, registry))
        return                          # run_health_check calls sys.exit internally

    # -- Single-command mode --------------------------------------------
    if args.command:
        confirm_delay = int(cfg.get("safety", {}).get("command_confirm_delay_seconds", 0))
        asyncio.run(run_single_command(
            agent, args.command,
            verbose_tools=not args.quiet,
            confirm_delay=confirm_delay,
        ))
        return

    # -- TUI mode -------------------------------------------------------
    try:
        from tui import AgentTUI
        app = AgentTUI(
            agent=agent,
            client=client,
            cfg=cfg,
            tool_names=registry.list_tools(),
            config_path=args.config,
        )
        app.run()
    except ImportError as e:
        print(
            f"[main.py:main] Failed to import TUI dependencies: {e}\n"
            "Install with: pip install textual",
            file=sys.stderr,
        )
        traceback.print_exc()
        sys.exit(1)
    except Exception as e:
        print(f"[main.py:main] TUI crashed: {e}", file=sys.stderr)
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
