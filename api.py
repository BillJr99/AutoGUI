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
    OPENWEBUI_BASE_URL=http://localhost:3000 \
    OPENWEBUI_API_KEY=sk-... \
    OPENWEBUI_MODEL=llama3.1:70b \
    python api.py

    # Test without a real desktop or OpenWebUI instance:
    AUTOGUI_DRY_RUN=true python api.py

Environment variables
---------------------
AUTOGUI_CONFIG      Path to config.json (default: ``config.json``).
AUTOGUI_DRY_RUN     ``true`` forces all tasks through DryRunAgent.
AUTOGUI_API_PORT    Listening port (default: ``8002``).
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
from typing import Any, AsyncIterator

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
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

CONFIG_PATH = Path(os.environ.get("AUTOGUI_CONFIG", "config.json"))
DRY_RUN: bool = os.environ.get("AUTOGUI_DRY_RUN", "false").lower() == "true"
API_VERSION = "1.0.0"
_START_TIME = time.monotonic()


def _load_config() -> dict:
    """Load config.json if it exists, otherwise build defaults from env vars."""
    if CONFIG_PATH.exists():
        try:
            with CONFIG_PATH.open() as fh:
                cfg = json.load(fh)
            logger.info("Loaded config from %s", CONFIG_PATH)
            return cfg
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to parse %s: %s — using env-var defaults", CONFIG_PATH, exc)

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
            "allowed_browser": True,
        },
    }


BASE_CONFIG: dict = _load_config()

# ---------------------------------------------------------------------------
# In-memory task store
# ---------------------------------------------------------------------------
# TASKS maps task_id -> task dict.
# task dict shape:
#   task_id   : str
#   task      : str          — original task string
#   status    : str          — "pending", "running", "done", "error", "cancelled"
#   steps     : list[dict]   — accumulated events
#   started_at: str | None   — ISO-8601
#   finished_at: str | None  — ISO-8601
#   _queue    : asyncio.Queue — per-task SSE queue (not serialised)

TASKS: dict[str, dict] = {}

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
        client, registry = build_components(cfg)
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
    """Return a new config dict with tools.allowed_* flags overridden."""
    if not allow:
        return cfg
    import copy
    cfg2 = copy.deepcopy(cfg)
    tools_cfg = cfg2.setdefault("tools", {})
    if "desktop" in allow:
        tools_cfg["allowed_desktop"] = bool(allow["desktop"])
    if "shell" in allow:
        tools_cfg["allowed_shell"] = bool(allow["shell"])
    if "browser" in allow:
        tools_cfg["allowed_browser"] = bool(allow["browser"])
    if allow.get("model"):
        cfg2.setdefault("openwebui", {})["model"] = allow["model"]
    return cfg2


# ---------------------------------------------------------------------------
# Background task runner
# ---------------------------------------------------------------------------

async def _run_task_async(task_id: str, task_str: str, cfg: dict, dry_run: bool) -> None:
    """
    Drive the agent loop for a single task and persist events to TASKS.

    This coroutine is scheduled as an asyncio background task by POST /api/task.
    It writes to TASKS[task_id] and to the per-task asyncio.Queue so SSE
    subscribers receive events in real-time.
    """
    task = TASKS[task_id]
    queue: asyncio.Queue = task["_queue"]

    task["status"] = "running"
    task["started_at"] = datetime.now(timezone.utc).isoformat()
    seq = 0

    try:
        agent = _build_agent(cfg, dry_run)

        async for event in agent.run(task_str):
            step = {
                "seq": seq,
                "kind": event.kind,
                "content": event.content,
                "data": event.data if isinstance(event.data, dict) else {},
            }
            task["steps"].append(step)
            await queue.put(step)
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
        await queue.put(err_step)
        task["status"] = "error"

    finally:
        task["finished_at"] = datetime.now(timezone.utc).isoformat()
        # Sentinel so SSE consumer knows the stream is done.
        await queue.put(None)


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
            for schema in registry.schemas()
        ]
        return {"ok": True, "tools": tools}
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not build ToolRegistry for /api/tools: %s", exc)
        return {"ok": True, "tools": [], "warning": str(exc)}


@app.post("/api/task", status_code=202)
async def create_task(req: TaskRequest, background_tasks: BackgroundTasks):
    """Submit a new task.  Returns immediately with a task_id."""
    if not req.task.strip():
        return _err("empty_task", "The 'task' field must not be empty.")

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
        "_queue": asyncio.Queue(),
        "_asyncio_task": None,
    }

    # Schedule the agent loop as a background coroutine.
    loop = asyncio.get_event_loop()
    async_task = loop.create_task(
        _run_task_async(task_id, req.task, cfg, effective_dry_run)
    )
    TASKS[task_id]["_asyncio_task"] = async_task

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

    Each event is a JSON object on a ``data:`` line::

        data: {"seq": 0, "kind": "plan", "content": "...", "data": {}}\n\n

    The stream closes after a ``{"kind": "done", "finished": true}`` sentinel.

    Connect with ``Accept: text/event-stream`` or any SSE client library.
    """
    task = _task_or_404(task_id)
    queue: asyncio.Queue = task["_queue"]

    async def _event_gen() -> AsyncIterator[str]:
        # First, replay events that already arrived before the client connected.
        for step in list(task["steps"]):
            payload = json.dumps(step, default=str)
            yield f"data: {payload}\n\n"

        # Then stream live events until the sentinel (None) arrives.
        if task["status"] in ("done", "error", "cancelled"):
            # Task already finished — send final sentinel and close.
            yield f'data: {{"kind": "done", "finished": true}}\n\n'
            return

        while True:
            try:
                item = await asyncio.wait_for(queue.get(), timeout=30.0)
            except asyncio.TimeoutError:
                # Send a keep-alive comment so the connection doesn't time out.
                yield ": keepalive\n\n"
                continue

            if item is None:
                # Sentinel from _run_task_async — stream is over.
                yield f'data: {{"kind": "done", "finished": true}}\n\n'
                return

            payload = json.dumps(item, default=str)
            yield f"data: {payload}\n\n"

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
    asyncio_task = task.get("_asyncio_task")
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
# Global exception handler
# ---------------------------------------------------------------------------

@app.exception_handler(Exception)
async def _global_handler(request: Request, exc: Exception):
    logger.exception("Unhandled exception on %s", request.url)
    return JSONResponse(
        status_code=500,
        content={
            "ok": False,
            "error": {"code": "internal_error", "message": str(exc)},
        },
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("AUTOGUI_API_PORT", "8002"))
    logger.info("Starting AutoGUI REST API on port %d (dry_run=%s)", port, DRY_RUN)
    uvicorn.run(app, host="0.0.0.0", port=port)
