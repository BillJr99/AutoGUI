"""
bootstrap.py — startup wiring for the OpenWebUI Desktop Agent.

Split out of main.py: logging setup, component construction (client /
registry / agent), the background REST API launcher, the connectivity
health check, and the Windows proactor unraisable-hook suppression.
main.py re-exports these so ``from main import build_components`` (used by
api.py) keeps working.
"""

import logging
import logging.handlers
import sys
from pathlib import Path

from agent import Agent
from client import OpenWebUIClient
from tools import ToolRegistry

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
            from pathlib import Path as _Path

            from install_runner import run_installer
            rc = run_installer(_Path(__file__).resolve().parent)
            if rc != 0:
                print(f"[main] install-dependencies script returned exit code {rc} — continuing anyway.")
        except Exception as e:
            print(f"[main] install-dependencies invocation failed: {e}")

    # Use `or {}` so a JSON null value is treated the same as a missing key.
    ow_cfg = cfg.get("openwebui") or {}
    client = OpenWebUIClient(
        base_url=ow_cfg.get("base_url") or "http://localhost:3000",
        api_key=ow_cfg.get("api_key") or "",
        model=ow_cfg.get("model") or "",
        api_path=ow_cfg.get("api_path") or "/api/chat/completions",
        temperature=ow_cfg.get("temperature", 0.2),
        max_tokens=ow_cfg.get("max_tokens", 4096),
        timeout_seconds=ow_cfg.get("timeout_seconds", 120),
    )
    registry = ToolRegistry(cfg)
    agent = Agent(client, registry, cfg)
    return client, registry, agent


# ---------------------------------------------------------------------------
# Background REST API launcher
# ---------------------------------------------------------------------------

def _start_api_background():
    """
    Start the FastAPI REST API server on a background daemon thread.

    The server is launched only when fastapi and uvicorn are installed.
    Set AUTOGUI_DISABLE_API=1 to suppress the server entirely.
    The bind host and port can be overridden via AUTOGUI_API_HOST and
    AUTOGUI_API_PORT (defaults: 127.0.0.1 and 8002).

    The default host binds to loopback only.  Setting
    AUTOGUI_API_HOST=0.0.0.0 is the explicit opt-in to exposing the
    unauthenticated API on ALL network interfaces (e.g. for Docker
    deployments where the container boundary provides isolation) — a
    prominent warning is logged in that case.  Set
    AUTOGUI_DISABLE_API=1 to disable the API entirely.
    """
    import os
    import threading

    if os.environ.get("AUTOGUI_DISABLE_API", "").lower() in ("1", "true", "yes"):
        return
    try:
        import uvicorn

        from api import app, get_api_host, get_api_port, warn_if_nonloopback_host
        host = get_api_host()
        port = get_api_port()

        _api_log = logging.getLogger("autogui.main")

        def _run():
            # log_config=None prevents uvicorn from installing its own
            # StreamHandlers on the uvicorn/uvicorn.access/uvicorn.error
            # loggers; those loggers then propagate to the root logger and
            # are captured by the TUI log handler (or the file handler)
            # instead of painting raw lines over the terminal layout.
            uvicorn.run(app, host=host, port=port, log_config=None, access_log=False)

        t = threading.Thread(target=_run, name="autogui-api", daemon=True)
        t.start()
        _api_log.info("[autogui] REST API starting on http://%s:%d", host, port)
        warn_if_nonloopback_host(host)
    except ImportError:
        logging.getLogger("autogui.main").warning(
            "[autogui] REST API disabled: fastapi/uvicorn not installed."
        )
    except Exception as e:
        logging.getLogger("autogui.main").error(
            "[autogui] REST API failed to start: %s", e
        )


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
    print(f"Registered tools ({len(registry.list_tools()):}):")
    for name in registry.list_tools():
        print(f"  • {name}")
    sys.exit(0 if reachable else 1)


def _suppress_windows_proactor_resource_warnings() -> None:
    """Silence the noisy ``ValueError: I/O operation on closed pipe``
    + ``unclosed transport`` exceptions that Python 3.13's Windows
    ProactorEventLoop emits at process exit when subprocesses we spawned
    (PowerShell helpers for desktop_launch, screenshot, etc.) get torn
    down by the GC after the loop has already closed.

    The transports ARE closed cleanly at runtime — this is a CPython
    cosmetic bug:
      _ProactorBasePipeTransport.__del__ -> _warn(f"... {self!r}", ...)
      where __repr__ calls self._sock.fileno() which raises
      ValueError on the already-closed pipe.  The exception escapes
      __del__ and Python prints it via sys.unraisablehook, NOT
      through the warnings module — which is why a previous attempt
      that only installed warnings.filterwarnings("ignore", ...) for
      the ResourceWarning category did nothing.

    Install a custom unraisablehook that swallows JUST these specific
    failures (ValueError from the proactor transport classes during
    teardown, and the ResourceWarning that would have followed) while
    leaving every other unraisable exception visible — those are real
    bugs we want to see.

    Only fires on Windows; on Linux/macOS asyncio uses the
    SelectorEventLoop which doesn't have this issue.
    """
    if sys.platform != "win32":
        return

    _previous_hook = sys.unraisablehook
    _NOISY_OBJ_NAMES = (
        "_ProactorBasePipeTransport.__del__",
        "BaseSubprocessTransport.__del__",
        "_ProactorReadPipeTransport.__del__",
        "_ProactorWritePipeTransport.__del__",
        "_ProactorSocketTransport.__del__",
    )

    def _hook(args: "sys.UnraisableHookArgs") -> None:  # type: ignore[name-defined]
        # The unraisable's `object` is the bound __del__ method whose
        # repr is e.g. "<function _ProactorBasePipeTransport.__del__
        # at 0x...>".  Match by string so we don't have to import the
        # private asyncio classes (which are platform-specific).
        try:
            obj_repr = repr(args.object)
        except Exception:
            obj_repr = ""
        is_noisy_source = any(name in obj_repr for name in _NOISY_OBJ_NAMES)

        # Also catch the exact "unclosed transport" ResourceWarning if
        # it ever does reach this hook directly.
        is_unclosed_transport_warning = (
            isinstance(args.exc_value, ResourceWarning)
            and "unclosed transport" in str(args.exc_value)
        )

        # And the ValueError raised when __repr__ tries to read fileno
        # on the already-closed pipe.
        is_closed_pipe_error = (
            isinstance(args.exc_value, ValueError)
            and "closed pipe" in str(args.exc_value)
        )

        if is_noisy_source and (is_unclosed_transport_warning or is_closed_pipe_error):
            return  # silently swallow

        # Anything else is a real unraisable exception — defer to the
        # previous hook (usually CPython's default which prints to
        # stderr) so we don't accidentally hide a bug.
        _previous_hook(args)

    sys.unraisablehook = _hook
