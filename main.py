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
import logging
import logging.handlers
import platform
import sys
import traceback
from pathlib import Path

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

from agent import Agent
from client import OpenWebUIClient
from tools import ToolRegistry


# ---------------------------------------------------------------------------
# Configuration loading
# ---------------------------------------------------------------------------

def load_config(path: str) -> dict:
    """
    Load and return the JSON configuration file.

    If the file does not exist, tries to bootstrap it from config.json.example
    in the same directory.  Raises FileNotFoundError only when neither file
    can be found.
    """
    import shutil

    p = Path(path)
    if not p.exists():
        example = p.parent / "config.json.example"
        if example.exists():
            shutil.copy(example, p)
            print(f"Created {path} from config.json.example — update it with your API key.")
        else:
            raise FileNotFoundError(
                f"Configuration file not found: {path}\n"
                "Create config.json with your OpenWebUI base_url and api_key, "
                "or copy config.json.example as a starting point."
            )
    with p.open() as f:
        cfg = json.load(f)
    return cfg


def apply_cli_overrides(cfg: dict, args: argparse.Namespace) -> dict:
    """
    Apply command-line flag overrides to the loaded configuration dict.
    Mutates cfg in place and returns it.
    """
    if args.model:
        cfg.setdefault("openwebui", {})["model"] = args.model
    if args.no_desktop:
        cfg.setdefault("tools", {})["allowed_desktop"] = False
    if args.no_shell:
        cfg.setdefault("tools", {})["allowed_shell"] = False
    return cfg


# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

def setup_logging(cfg: dict, verbose: bool = False) -> None:
    """
    Configure root logger with:
      - A rotating file handler at the configured level.
      - A stderr handler at WARNING (or DEBUG if verbose).
    """
    log_cfg = cfg.get("logging", {})
    log_file = log_cfg.get("file", "logs/agent.log")
    log_level_str = "DEBUG" if verbose else log_cfg.get("level", "INFO")
    log_level = getattr(logging, log_level_str.upper(), logging.INFO)
    max_bytes = log_cfg.get("max_bytes", 10 * 1024 * 1024)
    backup_count = log_cfg.get("backup_count", 3)

    Path(log_file).parent.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)               # capture everything at root

    # File handler: configurable level, rotating
    fh = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
    )
    fh.setLevel(log_level)
    fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    root.addHandler(fh)

    # Stderr handler: WARNING unless verbose
    sh = logging.StreamHandler(sys.stderr)
    sh.setLevel(logging.DEBUG if verbose else logging.WARNING)
    sh.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
    root.addHandler(sh)


# ---------------------------------------------------------------------------
# Component initialization
# ---------------------------------------------------------------------------

def build_components(cfg: dict):
    """
    Construct and return the (client, registry, agent) triple.

    When `install_dependencies` is true at the top level of the config,
    the appropriate `scripts/install-dependencies.*` script is invoked
    BEFORE the registry is built so any deps it provides (e.g. tesseract,
    playwright, pyatspi) are available when tools register.

    Returns
    -------
    tuple[OpenWebUIClient, ToolRegistry, Agent]
    """
    if cfg.get("install_dependencies", False):
        try:
            from install_runner import run_installer
            from pathlib import Path as _Path
            rc = run_installer(_Path(__file__).resolve().parent)
            if rc != 0:
                print(f"[main] install-dependencies script returned exit code {rc} — continuing anyway.")
        except Exception as e:
            print(f"[main] install-dependencies invocation failed: {e}")

    ow_cfg = cfg.get("openwebui", {})
    client = OpenWebUIClient(
        base_url=ow_cfg.get("base_url", "http://localhost:3000"),
        api_key=ow_cfg.get("api_key", ""),
        model=ow_cfg.get("model", "llama3.1:70b"),
        temperature=ow_cfg.get("temperature", 0.2),
        max_tokens=ow_cfg.get("max_tokens", 4096),
        timeout_seconds=ow_cfg.get("timeout_seconds", 120),
    )
    registry = ToolRegistry(cfg)
    agent = Agent(client, registry, cfg)
    return client, registry, agent


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
                if msvcrt.kbhit():
                    ch = msvcrt.getch()
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
# Startup validation: API key + model selection
# ---------------------------------------------------------------------------

_PLACEHOLDER_KEYS = {
    "",
    "sk-your-openwebui-api-key-here",
    "sk-your-key-from-openwebui-settings",
}

_SEP = "─" * 62


def _save_config_fields(config_path: str, section: str, fields: dict) -> bool:
    """
    Deep-merge *fields* into cfg[section] inside config.json.
    Returns True on success, False on any write error.
    """
    try:
        p = Path(config_path)
        existing = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
        existing.setdefault(section, {}).update(fields)
        p.write_text(json.dumps(existing, indent=2), encoding="utf-8")
        return True
    except Exception as e:
        print(f"  Warning: could not write {config_path}: {e}")
        return False


def _prompt_save(label: str, config_path: str, section: str, fields: dict) -> None:
    """Ask the user whether to persist *fields* to config.json."""
    try:
        answer = input(f"  Save {label} to config.json? [y/N]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return
    if answer == "y":
        if _save_config_fields(config_path, section, fields):
            print(f"  Saved to {config_path}")


async def validate_and_configure(cfg: dict, config_path: str) -> None:
    """
    Interactive startup validation — runs before components are built.

    Steps
    -----
    1. If the API key is unset / a placeholder, prompt for one.
    2. Connect to OpenWebUI and fetch the model list.
       - On HTTP 401: re-prompt for the key (up to 3 attempts total).
       - On connection error: warn and offer to continue anyway.
       Save-to-config is only offered after the key is confirmed working.
    3. If the configured model is absent from the server's list, show a
       numbered menu.  Save-to-config is offered after selection.
    """
    import getpass

    ow_cfg = cfg.setdefault("openwebui", {})
    original_key = ow_cfg.get("api_key", "").strip()

    # ── 1. Prompt for a key if none is configured ──────────────────────────
    if original_key in _PLACEHOLDER_KEYS:
        print(_SEP)
        print("No API key configured — OpenWebUI requires one.")
        print("Find yours: OpenWebUI → Settings → Account → API Keys")
        print(_SEP)
        try:
            key = getpass.getpass("  API key (input hidden): ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n  Skipping — requests will likely fail without a key.")
            return
        if not key:
            print("  No key entered — requests will likely fail.\n")
        else:
            ow_cfg["api_key"] = key
            # Don't offer to save yet — we'll do that only after it works.
            print()

    # ── 2. Connect and fetch model list (up to 3 key attempts) ────────────
    original_base_url = ow_cfg.get("base_url", "http://localhost:3000")
    base_url = original_base_url
    models: list[str] = []
    key_changed = ow_cfg.get("api_key", "").strip() != original_key
    url_changed = False

    for attempt in range(3):
        tmp = OpenWebUIClient(
            base_url=base_url,
            api_key=ow_cfg.get("api_key", ""),
            model=ow_cfg.get("model", ""),
            timeout_seconds=10,
        )
        print(f"Connecting to {base_url} … ", end="", flush=True)
        try:
            models = await tmp.fetch_models()
            n = len(models)
            print(f"OK  ({n} model{'s' if n != 1 else ''} available)\n")

            # Values confirmed working — offer to save anything that changed.
            saves: dict[str, str] = {}
            if key_changed:
                saves["api_key"] = ow_cfg["api_key"]
            if url_changed:
                saves["base_url"] = base_url
            if saves:
                labels = " and ".join(
                    k.replace("_", " ") for k in saves
                )
                _prompt_save(labels, config_path, "openwebui", saves)
                print()
            break

        except PermissionError:
            print("FAILED\n")
            print("  The API key was rejected (HTTP 401).")
            print("  Check your key: OpenWebUI → Settings → Account → API Keys.")
            if attempt == 2:
                print("  Too many failed attempts — continuing without a verified key.\n")
                break
            try:
                key = getpass.getpass("  New API key (or Enter to skip): ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not key:
                print("  Skipping key retry.\n")
                break
            ow_cfg["api_key"] = key
            key_changed = True
            print()

        except ConnectionError:
            print("FAILED\n")
            print(f"  Could not reach OpenWebUI at {base_url}.")
            print("  Check that the server is running and the address is correct.")
            if attempt == 2:
                print("  Continuing without a verified connection.\n")
                break
            try:
                new_url = input("  New base URL (or Enter to skip): ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not new_url:
                print("  Continuing without a verified connection.\n")
                break
            base_url = new_url.rstrip("/")
            ow_cfg["base_url"] = base_url
            url_changed = True
            print()

        except Exception as e:
            print("FAILED\n")
            print(f"  Unexpected error: {e}")
            print("  Continuing — tool calls may fail.\n")
            break

    # ── 3. Model selection ─────────────────────────────────────────────────
    if not models:
        return  # couldn't reach the server; nothing to validate against

    configured_model = ow_cfg.get("model", "")
    if configured_model in models:
        return  # configured model is available — nothing to do

    print()
    if configured_model:
        print(f"  Configured model '{configured_model}' is not in the server's model list.")
    else:
        print("  No model is configured.")

    print(f"\n  Available models ({len(models)}):\n")
    col_w = len(str(len(models)))
    for i, m in enumerate(models, 1):
        print(f"    {i:{col_w}}. {m}")
    print()

    while True:
        keep_hint = f", or Enter to keep '{configured_model}'" if configured_model else ""
        try:
            choice = input(f"  Select [1–{len(models)}]{keep_hint}: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not choice and configured_model:
            print(f"  Keeping '{configured_model}'.\n")
            break
        try:
            idx = int(choice) - 1
            if 0 <= idx < len(models):
                selected = models[idx]
                ow_cfg["model"] = selected
                print(f"\n  Model set to: {selected}\n")
                _prompt_save(f"model '{selected}'", config_path, "openwebui",
                             {"model": selected})
                print()
                break
            print(f"  Enter a number between 1 and {len(models)}.")
        except ValueError:
            print("  Please enter a number.")


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

async def run_health_check(client: OpenWebUIClient, registry: ToolRegistry) -> None:
    """Print connectivity status and tool list, then exit."""
    base = client.base_url
    reachable = await client.health_check()
    status = "✓ reachable" if reachable else "✗ unreachable"
    print(f"OpenWebUI instance: {base}  [{status}]")
    print(f"Model configured:   {client.model}")
    print(f"Registered tools ({len(registry.list_tools())}):")
    for name in registry.list_tools():
        print(f"  • {name}")
    sys.exit(0 if reachable else 1)


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


def _suppress_windows_proactor_resource_warnings() -> None:
    """Silence the noisy "I/O operation on closed pipe" /
    "unclosed transport" ResourceWarnings that the Python 3.13 Windows
    ProactorEventLoop emits at process exit when subprocesses we spawned
    (PowerShell helpers for desktop_launch, screenshot, etc.) get torn
    down by the GC after the loop has already closed.

    The transports ARE closed cleanly at runtime — this is a known
    cosmetic race in CPython's __del__ that fires during interpreter
    shutdown.  The warnings flood the user's terminal on Ctrl+C and add
    no diagnostic value, so suppress them.  Only fires on Windows; on
    Linux/macOS asyncio uses the SelectorEventLoop which doesn't have
    this issue.
    """
    if sys.platform != "win32":
        return
    import warnings
    warnings.filterwarnings(
        "ignore",
        message=r"unclosed transport.*",
        category=ResourceWarning,
    )


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
