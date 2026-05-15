# AutoGUI REST API Reference

The AutoGUI REST API wraps the desktop automation agent in an HTTP interface,
enabling programmatic task submission, live event streaming via Server-Sent
Events (SSE), and task history inspection — without coupling to the Textual TUI
or the CLI entry point.

---

## Table of Contents

1. [Quick Start](#quick-start)
2. [Environment Variables](#environment-variables)
3. [Starting the Server](#starting-the-server)
4. [Endpoints](#endpoints)
   - [GET /api/healthz](#get-apihealthz)
   - [GET /api/capabilities](#get-apicapabilities)
   - [GET /api/tools](#get-apitools)
   - [POST /api/task](#post-apitask)
   - [GET /api/task/{task_id}](#get-apitasktask_id)
   - [GET /api/task/{task_id}/stream](#get-apitasktask_idstream)
   - [POST /api/task/{task_id}/cancel](#post-apitasktask_idcancel)
   - [POST /api/task/{task_id}/approve](#post-apitasktask_idapprove)
5. [SSE Event Format](#sse-event-format)
6. [Event Kinds Reference](#event-kinds-reference)
7. [Error Shape](#error-shape)
8. [Dry-Run Mode](#dry-run-mode)
9. [Security Notes](#security-notes)

---

## Quick Start

```bash
# 1. Install API dependencies
pip install fastapi "uvicorn[standard]"

# 2. Copy and fill in config (or use env vars — see below)
cp config.json.example config.json

# 3. Start the server
python api.py
# Starting on http://0.0.0.0:8002

# 4. Submit a task
curl -s -X POST http://localhost:8002/api/task \
  -H 'Content-Type: application/json' \
  -d '{"task": "Take a screenshot of the desktop"}'
# {"ok": true, "task_id": "3fa85f64-..."}

# 5. Stream live events
curl -N http://localhost:8002/api/task/3fa85f64-.../stream
# data: {"seq": 0, "kind": "plan", "content": "...", "data": {...}}
# data: {"seq": 1, "kind": "tool_call", "content": "...", "data": {...}}
# ...
# data: {"kind": "done", "finished": true}

# 6. Or poll for the finished task
curl -s http://localhost:8002/api/task/3fa85f64-...
```

---

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `AUTOGUI_CONFIG` | `config.json` | Path to the configuration file. If the file does not exist, the agent is configured from the `OPENWEBUI_*` variables below. An empty string means "no config file" and skips file loading entirely. |
| `AUTOGUI_DRY_RUN` | `false` | Set to `true` to force all tasks through `DryRunAgent` — no desktop is touched, no OpenWebUI call is made. Useful for testing. |
| `AUTOGUI_API_PORT` | `8002` | TCP port the API server listens on. |
| `AUTOGUI_API_HOST` | `0.0.0.0` | Bind address for the API server. Binds to all interfaces by default (intended for sandbox/container use). Set to `127.0.0.1` to restrict to loopback — the API has no authentication. |
| `OPENWEBUI_BASE_URL` | `http://localhost:3000` | OpenWebUI base URL (used when `config.json` is absent). |
| `OPENWEBUI_API_KEY` | _(empty)_ | API key for OpenWebUI (used when `config.json` is absent). |
| `OPENWEBUI_MODEL` | _(empty)_ | Model ID to use (used when `config.json` is absent). |

---

## Starting the Server

### Directly

```bash
python api.py
```

### With environment variables (no config file)

```bash
OPENWEBUI_BASE_URL=http://openwebui.example.com \
OPENWEBUI_API_KEY=sk-my-key \
OPENWEBUI_MODEL=llama3.1:70b \
AUTOGUI_API_PORT=8080 \
python api.py
```

### In dry-run mode (no display or OpenWebUI needed)

```bash
AUTOGUI_DRY_RUN=true python api.py
```

### With Docker

```bash
# Build
docker build -t autogui .

# Run in API mode (real agent)
docker run -p 8002:8002 \
  -e DISPLAY=$DISPLAY \
  -v /tmp/.X11-unix:/tmp/.X11-unix \
  -e OPENWEBUI_BASE_URL=http://host.docker.internal:3000 \
  -e OPENWEBUI_API_KEY=sk-my-key \
  -e OPENWEBUI_MODEL=llama3.1:70b \
  autogui python api.py

# Run in dry-run mode (no display needed)
docker run -p 8002:8002 \
  -e AUTOGUI_DRY_RUN=true \
  autogui python api.py
```

### Interactive API docs

Once the server is running, open:

- Swagger UI: `http://localhost:8002/docs`
- ReDoc: `http://localhost:8002/redoc`

---

## Endpoints

All responses include `"ok": true` on success or `"ok": false` with an `error` object on failure.

---

### GET /api/healthz

Liveness probe. Always returns `200 OK` if the server is running.

**Response**

```json
{
  "ok": true,
  "version": "1.0.0",
  "uptime_s": 42.3
}
```

---

### GET /api/capabilities

Return what the server is configured to allow.

**Response**

```json
{
  "ok": true,
  "dry_run": false,
  "surfaces": {
    "desktop": true,
    "shell": true,
    "browser": false
  }
}
```

| Field | Type | Description |
|-------|------|-------------|
| `dry_run` | bool | `true` if the server is in global dry-run mode. |
| `surfaces.desktop` | bool | Desktop screenshot / click / type tools enabled. |
| `surfaces.shell` | bool | Shell execution tool enabled. |
| `surfaces.browser` | bool | Playwright browser tools enabled. |

---

### GET /api/tools

Return the list of registered tools with their names and descriptions.

**Response**

```json
{
  "ok": true,
  "tools": [
    {"name": "desktop_screenshot", "description": "Take a screenshot of the current screen."},
    {"name": "shell_run", "description": "Run a shell command with a timeout."}
  ]
}
```

Returns an empty list in global dry-run mode (no ToolRegistry is built).

---

### POST /api/task

Submit a new automation task. Returns immediately with a `task_id`; the agent runs in the background.

**Request body**

```json
{
  "task": "Open Firefox and navigate to https://example.com",
  "model": "llama3.1:70b",
  "allow": {
    "desktop": true,
    "shell": false,
    "browser": true
  },
  "dry_run": false
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `task` | string | Yes | Natural-language task description. |
| `model` | string | No | Override the model for this task only. |
| `allow` | object | No | Per-surface capability overrides for this task. |
| `allow.desktop` | bool | No | Override desktop tool availability. |
| `allow.shell` | bool | No | Override shell tool availability. |
| `allow.browser` | bool | No | Override browser tool availability. |
| `dry_run` | bool | No | Force dry-run for this task only (default `false`). |

**Response** — `202 Accepted`

```json
{"ok": true, "task_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6"}
```

---

### GET /api/task/{task_id}

Return the current state and all accumulated steps for a task.

**Response**

```json
{
  "ok": true,
  "task_id": "3fa85f64-...",
  "task": "Open Firefox and navigate to https://example.com",
  "status": "done",
  "steps": [
    {"seq": 0, "kind": "plan",        "content": "...", "data": {}},
    {"seq": 1, "kind": "tool_call",   "content": "browser_navigate(...)", "data": {...}},
    {"seq": 2, "kind": "tool_result", "content": "...", "data": {...}},
    {"seq": 3, "kind": "done",        "content": "...", "data": {"iterations": 2}}
  ],
  "started_at":  "2024-01-15T12:00:00+00:00",
  "finished_at": "2024-01-15T12:00:15+00:00"
}
```

| Field | Description |
|-------|-------------|
| `status` | One of `pending`, `running`, `done`, `error`, `cancelled`. |
| `steps` | All events emitted so far. Each step is an [SSE event object](#sse-event-format). |

---

### GET /api/task/{task_id}/stream

Stream task events as Server-Sent Events (SSE). Connect before or after the task starts — events already emitted are replayed, then live events follow.

**Response** — `text/event-stream`

```
data: {"seq": 0, "kind": "plan", "content": "Step 1: ...", "data": {}}

data: {"seq": 1, "kind": "tool_call", "content": "desktop_screenshot()", "data": {"tool": "desktop_screenshot", "args": {}}}

data: {"seq": 2, "kind": "tool_result", "content": "Screenshot saved.", "data": {"path": "screenshots/screen.png"}}

data: {"kind": "done", "finished": true}

```

The connection closes after the `{"kind": "done", "finished": true}` sentinel.

**curl example**

```bash
curl -N -H 'Accept: text/event-stream' \
  http://localhost:8002/api/task/3fa85f64-.../stream
```

**JavaScript example**

```js
const es = new EventSource('http://localhost:8002/api/task/TASK_ID/stream');
es.onmessage = (e) => {
  const event = JSON.parse(e.data);
  console.log(event.kind, event.content);
  if (event.finished) es.close();
};
```

---

### POST /api/task/{task_id}/cancel

Cancel a running task (best-effort). The background coroutine receives an `asyncio.CancelledError`.

**Response**

```json
{"ok": true}
```

or, if the task was not running:

```json
{"ok": true, "note": "Task was not running"}
```

---

### POST /api/task/{task_id}/approve

**Stub** — reserved for future human-in-the-loop support. Currently always returns `ok: true`.

When `confirm_countdown` events are extended to pause the agent and wait for external approval, this endpoint will resume execution. For now it is a no-op placeholder.

**Response**

```json
{
  "ok": true,
  "note": "approve is a stub — human-in-the-loop not yet implemented"
}
```

---

## SSE Event Format

Each event in the stream (and in the `steps` array from `GET /api/task/{task_id}`) has this shape:

```json
{
  "seq":     0,
  "kind":    "plan",
  "content": "Human-readable description of this event.",
  "data":    {}
}
```

| Field | Type | Description |
|-------|------|-------------|
| `seq` | int | Zero-based sequence number within the task. |
| `kind` | string | Event kind — see [Event Kinds Reference](#event-kinds-reference). |
| `content` | string | Human-readable summary of the event. |
| `data` | object | Structured payload; shape varies by kind. |

The final sentinel event has no `seq` field:

```json
{"kind": "done", "finished": true}
```

---

## Event Kinds Reference

| Kind | Description | Notable `data` fields |
|------|-------------|----------------------|
| `plan` | High-level plan produced by the planner before execution. | `steps: list[str]` — plan steps |
| `text` | A text segment from the assistant (reasoning, commentary). | _(none)_ |
| `tool_call` | The model is about to invoke a tool. | `tool: str`, `args: dict` |
| `confirm_countdown` | Safety countdown before a tool is dispatched. | `seconds: int`, `tool: str` |
| `tool_result` | Result of a tool call. | `result: any`, `error: str` |
| `error` | An error occurred in the agent loop. | `exception: str` |
| `done` | The agent loop has ended. | `iterations: int`, `finish_reason: str` |

---

## Error Shape

All error responses use HTTP status codes in the 4xx/5xx range and share this body shape:

```json
{
  "ok": false,
  "error": {
    "code":    "not_found",
    "message": "Task '3fa85f64-...' not found"
  }
}
```

Common error codes:

| Code | HTTP Status | Description |
|------|-------------|-------------|
| `empty_task` | 400 | The `task` field was empty or whitespace. |
| `not_found` | 404 | The requested `task_id` does not exist. |
| `server_busy` | 503 | Task store is full (500 tasks) and all are still running. |
| `internal_error` | 500 | Unhandled server-side exception. |

---

## Dry-Run Mode

When `AUTOGUI_DRY_RUN=true` (server-wide) or `dry_run: true` (per-task request), the agent is replaced with `DryRunAgent` which emits canned events without touching the desktop, running shell commands, or calling OpenWebUI.

This mode is useful for:

- Testing the API server in CI environments without a display.
- Verifying SSE streaming without an OpenWebUI instance.
- Demonstrating the event stream format.

Dry-run events are prefixed with `[DRY RUN]` in their `content` field and include `"dry_run": true` in their `data`.

```bash
# Enable globally
AUTOGUI_DRY_RUN=true python api.py

# Enable per-task
curl -s -X POST http://127.0.0.1:8002/api/task \
  -H 'Content-Type: application/json' \
  -d '{"task": "Do something", "dry_run": true}'
```

---

## Security Notes

- **No authentication** is enforced. The API is designed to run inside a trusted network boundary (localhost or private LAN). Do not expose it to the public internet without adding your own auth layer (e.g. a reverse proxy with bearer tokens).
- The default bind address is `0.0.0.0` (all interfaces), intended for sandbox/container use. Set `AUTOGUI_API_HOST=127.0.0.1` to restrict to loopback for local development — the API has no authentication.
- The agent operates at OS level: it can click anywhere, run shell commands, and read/write files. Only run it on machines where you accept this capability.
- Set `allowed_shell: false` in `config.json` (or `allow.shell: false` per request) if you want to restrict shell access.
- Restrict `config.json` permissions: `chmod 600 config.json` — it contains your API key.
