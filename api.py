"""
api.py — FastAPI REST API server for AutoGUI.

Exposes the AutoGUI Agent as a network-accessible API so that external
processes, web UIs, and automation scripts can submit desktop tasks,
stream live events, and inspect task history — without coupling to the
Textual TUI or the CLI entry point.

Quick start::

    # With config.json already present:
    python api.py

    # Without a config file — use environment variables:
    OPENWEBUI_BASE_URL=http://localhost:3000 \\
    OPENWEBUI_API_KEY=sk-... \\
    OPENWEBUI_MODEL=llama3.1:70b \\
    python api.py

    # Test without a real desktop or OpenWebUI instance:
    AUTOGUI_DRY_RUN=true python api.py

Environment variables
---------------------
AUTOGUI_CONFIG      Path to config.json (default: ``config.json``).
                    An empty string is treated as "no config file".
AUTOGUI_DRY_RUN     ``true`` forces all tasks through DryRunAgent.
AUTOGUI_API_PORT    Listening port (default: ``8002``).
AUTOGUI_API_HOST    Bind address (default: ``0.0.0.0``).
                    The default binds on all interfaces for sandbox/
                    container testing — set ``AUTOGUI_API_HOST=127.0.0.1``
                    for local-only use.  The API has no authentication.
OPENWEBUI_BASE_URL  OpenWebUI base URL when no config file is present.
OPENWEBUI_API_KEY   API key when no config file is present.
OPENWEBUI_MODEL     Model name when no config file is present.

All HTTP responses follow the shape ``{ok: true|false, ...}``.
Errors follow ``{ok: false, error: {code: str, message: str}}``.

No authentication is enforced — this API is designed to run inside a
trusted network boundary (e.g. localhost or a private LAN).  Do not
expose it to untrusted networks without adding your own auth layer.
"""

import asyncio
import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("autogui.api")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Treat an empty AUTOGUI_CONFIG as "no config file" so that Path("") (which
# resolves to the current directory) never triggers a spurious open() call.
# CONFIG_PATH is None when the env var is absent or empty — _load_config()
# skips file loading in that case and falls back to env-var defaults.
_config_env = os.environ.get("AUTOGUI_CONFIG", "config.json").strip()
CONFIG_PATH: Optional[Path] = Path(_config_env) if _config_env else None

DRY_RUN: bool = os.environ.get("AUTOGUI_DRY_RUN", "false").lower() == "true"
API_VERSION = "1.0.0"
_START_TIME = time.monotonic()

# ---------------------------------------------------------------------------
# Default bind host/port — shared with main.py's background launcher.
# Environment overrides take precedence; main.py imports these names so the
# CLI launcher does not duplicate the defaults.
# ---------------------------------------------------------------------------

DEFAULT_API_HOST = "0.0.0.0"
DEFAULT_API_PORT = 8002


def get_api_host() -> str:
    """Return the effective bind host, honouring ``AUTOGUI_API_HOST``."""
    return os.environ.get("AUTOGUI_API_HOST", DEFAULT_API_HOST)


def get_api_port() -> int:
    """Return the effective listening port, honouring ``AUTOGUI_API_PORT``."""
    return int(os.environ.get("AUTOGUI_API_PORT", str(DEFAULT_API_PORT)))


def _load_config() -> dict:
    """Load config.json if it exists as a file; otherwise build defaults from env vars."""
    if CONFIG_PATH is not None and CONFIG_PATH.exists() and CONFIG_PATH.is_file():
        try:
            with CONFIG_PATH.open() as fh:
                cfg = json.load(fh)
            logger.info("Loaded config from %s", CONFIG_PATH)
            return cfg
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to parse %s: %s — using env-var defaults", CONFIG_PATH, exc)

    if CONFIG_PATH is None:
        logger.info("AUTOGUI_CONFIG is empty — building config from environment variables")
    else:
        logger.info(
            "Config file %s not found — building config from environment variables",
            CONFIG_PATH,
        )
    return {
        "openwebui": {
            "base_url": os.environ.get("OPENWEBUI_BASE_URL", "http://localhost:3000"),
            "api_key": os.environ.get("OPENWEBUI_API_KEY", ""),
            "model": os.environ.get("OPENWEBUI_MODEL", ""),
        },
        "tools": {
            "allowed_desktop": True,
            "allowed_shell": True,
            "allowed_browser": False,  # off by default; opt-in per config or per request
        },
    }


BASE_CONFIG: dict = _load_config()

# ---------------------------------------------------------------------------
# In-memory task store
# ---------------------------------------------------------------------------
# TASKS maps task_id -> task dict.
# task dict shape:
#   task_id               : str
#   task                  : str          — original task string
#   status                : str          — "pending", "running", "done", "error", "cancelled"
#   steps                 : list[dict]   — accumulated events (append-only; safe to read at any offset)
#   started_at            : str | None   — ISO-8601
#   finished_at           : str | None   — ISO-8601
#   cancellation_requested: bool         — set by cancel_task; checked in _run_task_async

TASKS: dict[str, dict] = {}

# Module-level dict that maps task_id -> asyncio.Task handle so that
# cancel_task can cancel the handle and test teardown can await it.
_TASK_HANDLES: dict[str, asyncio.Task] = {}

# Maximum number of tasks to keep in TASKS before evicting completed ones.
MAX_TASKS = 500

# ---------------------------------------------------------------------------
# FastAPI application
# ---------------------------------------------------------------------------

app = FastAPI(
    title="AutoGUI REST API",
    description=(
        "Programmatic access to the AutoGUI desktop automation agent. "
        "Submit tasks, stream live events via SSE, and inspect history."
    ),
    version=API_VERSION,
    docs_url="/docs",
    redoc_url="/redoc",
)


# ---------------------------------------------------------------------------
# Exception handlers — ensure all errors use the {ok, error} envelope
# ---------------------------------------------------------------------------

@app.exception_handler(HTTPException)
async def _http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    """Normalise HTTPException so the body is always {ok:false, error:{code,message}}."""
    if isinstance(exc.detail, dict):
        return JSONResponse(status_code=exc.status_code, content=exc.detail)
    return JSONResponse(
        status_code=exc.status_code,
        content={"ok": False, "error": {"code": str(exc.status_code), "message": exc.detail}},
    )


@app.exception_handler(RequestValidationError)
async def _validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={"ok": False, "error": {"code": "validation_error", "message": str(exc)}},
    )


@app.exception_handler(Exception)
async def _global_handler(request: Request, exc: Exception) -> JSONResponse:
    # Log full details server-side; return a generic message to the client to
    # avoid leaking internal paths, config hints, or stack-adjacent information.
    logger.exception("Unhandled exception on %s", request.url)
    return JSONResponse(
        status_code=500,
        content={
            "ok": False,
            "error": {"code": "internal_error", "message": "An internal server error occurred."},
        },
    )


# ---------------------------------------------------------------------------
# Error helpers
# ---------------------------------------------------------------------------

def _err(code: str, message: str, status: int = 400) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"ok": False, "error": {"code": code, "message": message}},
    )


def _task_or_404(task_id: str) -> dict:
    task = TASKS.get(task_id)
    if task is None:
        raise HTTPException(
            status_code=404,
            detail={"ok": False, "error": {"code": "not_found", "message": f"Task {task_id!r} not found"}},
        )
    return task


# ---------------------------------------------------------------------------
# Agent / DryRunAgent factory
# ---------------------------------------------------------------------------

def _build_agent(cfg: dict, dry_run: bool):
    """
    Return either a real Agent or a DryRunAgent.

    For DryRunAgent we skip the heavy imports (client, tools, agent modules
    which require pyautogui, textual, etc. to be installed).
    """
    if dry_run:
        from dry_run import DryRunAgent
        return DryRunAgent()

    # Lazy-import the real agent stack only when needed.
    from client import OpenWebUIClient
    from tools import ToolRegistry
    from agent import Agent
    from main import build_components

    try:
        # build_components returns (client, registry, agent)
        client, registry, agent = build_components(cfg)
        return agent
    except Exception as exc:  # noqa: BLE001
        logger.warning("build_components failed (%s); falling back to env defaults", exc)
        ow = cfg.get("openwebui", {})
        client = OpenWebUIClient(
            base_url=ow.get("base_url", "http://localhost:3000"),
            api_key=ow.get("api_key", ""),
            model=ow.get("model", ""),
        )
        registry = ToolRegistry(cfg)
        return Agent(client=client, registry=registry, cfg=cfg)


def _merge_allow_overrides(cfg: dict, allow: dict | None) -> dict:
    """Return a new config dict with tools.allowed_* flags restricted by overrides.

    Per-request overrides can only *restrict* capabilities — they are ANDed
    with the server's base configuration.  A request cannot enable a surface
    (e.g. browser) that is disabled in the server config, preventing
    privilege escalation through the request body.
    """
    if not allow:
        return cfg
    import copy
    cfg2 = copy.deepcopy(cfg)
    tools_cfg = cfg2.setdefault("tools", {})
    if "desktop" in allow:
        tools_cfg["allowed_desktop"] = tools_cfg.get("allowed_desktop", True) and bool(allow["desktop"])
    if "shell" in allow:
        tools_cfg["allowed_shell"] = tools_cfg.get("allowed_shell", True) and bool(allow["shell"])
    if "browser" in allow:
        tools_cfg["allowed_browser"] = tools_cfg.get("allowed_browser", False) and bool(allow["browser"])
    if allow.get("model"):
        cfg2.setdefault("openwebui", {})["model"] = allow["model"]
    return cfg2


# ---------------------------------------------------------------------------
# Background task runner
# ---------------------------------------------------------------------------

async def _run_task_async(task_id: str, task_str: str, cfg: dict, dry_run: bool) -> None:
    """
    Drive the agent loop for a single task and persist events to TASKS.

    This coroutine is scheduled as an asyncio.Task by POST /api/task.
    It appends steps to task["steps"] so SSE subscribers can read by index
    without sharing a queue.
    """
    task = TASKS.get(task_id)
    if task is None:
        return  # Task was cleared (e.g. test teardown) before we started

    # Check if cancellation was requested before we even set status=running.
    if task.get("cancellation_requested"):
        task["status"] = "cancelled"
        task["finished_at"] = datetime.now(timezone.utc).isoformat()
        return

    task["status"] = "running"
    task["started_at"] = datetime.now(timezone.utc).isoformat()
    seq = 0

    try:
        # Coerce to str defensively — Pydantic should guarantee this, but
        # guard against null config values that could propagate as None.
        safe_task_str = str(task_str) if task_str is not None else ""
        if not safe_task_str.strip():
            raise ValueError("task string is empty or None")

        agent = _build_agent(cfg, dry_run)

        async for event in agent.run(safe_task_str):
            step = {
                "seq": seq,
                "kind": event.kind,
                "content": event.content,
                "data": event.data if isinstance(event.data, dict) else {},
            }
            task["steps"].append(step)
            seq += 1

            if event.kind == "done":
                break

        task["status"] = "done"

    except asyncio.CancelledError:
        task["status"] = "cancelled"
        logger.info("Task %s was cancelled", task_id)

    except Exception as exc:  # noqa: BLE001
        logger.exception("Task %s raised an exception", task_id)
        err_step = {
            "seq": seq,
            "kind": "error",
            "content": str(exc),
            "data": {"exception": type(exc).__name__},
        }
        task["steps"].append(err_step)
        task["status"] = "error"

    finally:
        # Guard against task being cleared between the early-return check above
        # and here (e.g. rapid test teardown).
        task = TASKS.get(task_id)
        if task is not None:
            task["finished_at"] = datetime.now(timezone.utc).isoformat()
        # Remove the handle from _TASK_HANDLES once the coroutine is done.
        _TASK_HANDLES.pop(task_id, None)


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class AllowOverrides(BaseModel):
    desktop: bool | None = None
    shell: bool | None = None
    browser: bool | None = None


class TaskRequest(BaseModel):
    task: str
    model: str | None = None
    allow: AllowOverrides | None = None
    dry_run: bool = False


# ---------------------------------------------------------------------------
# Helper: serialise task for API responses (strip private keys)
# ---------------------------------------------------------------------------

def _task_view(task: dict) -> dict:
    return {
        "task_id": task["task_id"],
        "task": task["task"],
        "status": task["status"],
        "steps": task["steps"],
        "started_at": task["started_at"],
        "finished_at": task["finished_at"],
    }


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/api/healthz")
async def healthz():
    """Liveness probe."""
    return {
        "ok": True,
        "version": API_VERSION,
        "uptime_s": round(time.monotonic() - _START_TIME, 2),
    }


@app.get("/api/capabilities")
async def capabilities():
    """Return what the server is configured to allow."""
    tools_cfg = BASE_CONFIG.get("tools", {})
    return {
        "ok": True,
        "dry_run": DRY_RUN,
        "surfaces": {
            "desktop": bool(tools_cfg.get("allowed_desktop", True)),
            "shell": bool(tools_cfg.get("allowed_shell", True)),
            "browser": bool(tools_cfg.get("allowed_browser", False)),
        },
    }


@app.get("/api/tools")
async def list_tools():
    """
    Return the registered tool list.

    In dry-run-only mode the tool list is empty because no real ToolRegistry
    is built.  When a real Agent can be constructed, return the actual schemas.
    """
    if DRY_RUN:
        return {"ok": True, "tools": []}

    try:
        from tools import ToolRegistry
        registry = ToolRegistry(BASE_CONFIG)
        tools = [
            {
                "name": schema["function"]["name"],
                "description": schema["function"].get("description", ""),
            }
            for schema in registry.schemas  # schemas is a property, not a callable
        ]
        return {"ok": True, "tools": tools}
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not build ToolRegistry for /api/tools: %s", exc)
        return {"ok": True, "tools": [], "warning": "Tool registry unavailable."}


@app.post("/api/task", status_code=202)
async def create_task(req: TaskRequest):
    """Submit a new task.  Returns immediately with a task_id."""
    if not req.task.strip():
        return _err("empty_task", "The 'task' field must not be empty.")

    # Enforce task store cap: evict the oldest completed task if at capacity.
    if len(TASKS) >= MAX_TASKS:
        terminal_statuses = {"done", "error", "cancelled"}
        oldest_completed_id = next(
            (tid for tid, t in TASKS.items() if t["status"] in terminal_statuses),
            None,
        )
        if oldest_completed_id is None:
            return _err("server_busy", "Task store is full; all tasks are still running.", status=503)
        del TASKS[oldest_completed_id]
        _TASK_HANDLES.pop(oldest_completed_id, None)

    task_id = str(uuid.uuid4())
    effective_dry_run = DRY_RUN or req.dry_run

    # Build per-request config with any overrides.
    allow_dict = req.allow.model_dump(exclude_none=True) if req.allow else {}
    if req.model:
        allow_dict["model"] = req.model
    cfg = _merge_allow_overrides(BASE_CONFIG, allow_dict)

    TASKS[task_id] = {
        "task_id": task_id,
        "task": req.task,
        "status": "pending",
        "steps": [],
        "started_at": None,
        "finished_at": None,
        "cancellation_requested": False,
    }

    # Schedule the agent loop as an asyncio background task.
    async_task = asyncio.create_task(
        _run_task_async(task_id, req.task, cfg, effective_dry_run)
    )
    _TASK_HANDLES[task_id] = async_task

    logger.info("Created task %s: %r (dry_run=%s)", task_id, req.task[:80], effective_dry_run)
    return {"ok": True, "task_id": task_id}


@app.get("/api/task/{task_id}")
async def get_task(task_id: str):
    """Return the current state of a task including all accumulated steps."""
    task = _task_or_404(task_id)
    return {"ok": True, **_task_view(task)}


@app.get("/api/task/{task_id}/stream")
async def stream_task(task_id: str):
    """
    Stream task events as Server-Sent Events (SSE).

    Events are delivered by polling task["steps"] by index so that every
    subscriber gets the full event history independently — no shared queue
    to contend with.  Events already emitted are replayed first, then live
    events follow until the task finishes.

    Each event is a JSON object on a ``data:`` line::

        data: {"seq": 0, "kind": "plan", "content": "...", "data": {}}\n\n

    The stream closes after a ``{"kind": "done", "finished": true}`` sentinel.
    """
    task = _task_or_404(task_id)

    async def _event_gen() -> AsyncIterator[str]:
        sent = 0
        while True:
            # Drain any steps that have arrived since last iteration.
            steps = task["steps"]
            while sent < len(steps):
                payload = json.dumps(steps[sent], default=str)
                yield f"data: {payload}\n\n"
                sent += 1

            # Task finished — flush any trailing steps and close stream.
            if task["status"] in ("done", "error", "cancelled"):
                steps = task["steps"]
                while sent < len(steps):
                    payload = json.dumps(steps[sent], default=str)
                    yield f"data: {payload}\n\n"
                    sent += 1
                yield f'data: {{"kind": "done", "finished": true}}\n\n'
                return

            # Not done yet — brief poll interval (also acts as a keep-alive).
            await asyncio.sleep(0.1)

    return StreamingResponse(
        _event_gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # disable Nginx buffering when behind a proxy
        },
    )


@app.post("/api/task/{task_id}/cancel")
async def cancel_task(task_id: str):
    """Cancel a running task (best-effort)."""
    task = _task_or_404(task_id)

    # Set the flag so _run_task_async won't overwrite a cancelled status.
    task["cancellation_requested"] = True

    asyncio_task = _TASK_HANDLES.get(task_id)
    if asyncio_task and not asyncio_task.done():
        asyncio_task.cancel()
        task["status"] = "cancelled"
        task["finished_at"] = datetime.now(timezone.utc).isoformat()
        logger.info("Cancelled task %s", task_id)
        return {"ok": True}
    return {"ok": True, "note": "Task was not running"}


@app.post("/api/task/{task_id}/approve")
async def approve_task(task_id: str):
    """
    Approve a paused task (stub — human-in-the-loop not yet implemented).

    This endpoint is reserved for future use: when confirm_countdown events
    are extended to actually pause the agent and wait for external approval,
    calling this will resume execution.  For now it always returns ok.
    """
    _task_or_404(task_id)  # validate task exists
    return {"ok": True, "note": "approve is a stub — human-in-the-loop not yet implemented"}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn

    port = get_api_port()
    # Default binds on all interfaces (0.0.0.0) for sandbox/container testing.
    # Set AUTOGUI_API_HOST=127.0.0.1 for local-only use — the API has no
    # authentication and should not be exposed to untrusted networks.
    host = get_api_host()
    logger.info("Starting AutoGUI REST API on %s:%d (dry_run=%s)", host, port, DRY_RUN)
    uvicorn.run(app, host=host, port=port)
