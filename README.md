# AutoGUI Desktop Agent

AutoGUI provides desktop automation in two forms:

1. A standalone Python CLI/TUI agent that connects any [OpenWebUI](https://openwebui.com/) instance to your desktop.
2. A native TypeScript Pi Coding Agent extension in `pi-extension/` that lets Pi own the agent workflow while AutoGUI supplies desktop tools.

The standalone agent drives a ReAct-style loop (Reason → Act → Observe → repeat)
and can run shell commands, read/write files, take screenshots, click, type,
launch programs, and inspect accessibility trees — all via function-calling with
any model available in your OpenWebUI instance.

The standalone Python agent architecture follows [UFO](https://github.com/microsoft/UFO) and
[open-interpreter](https://github.com/OpenInterpreter/open-interpreter) but is vendor-neutral:
any model that supports OpenAI-compatible tool calling and is registered in your OpenWebUI
works out of the box.

The Pi extension is decoupled from OpenWebUI. It uses whatever model/provider Pi
is configured to use and exposes desktop tools plus `/autogui`.

> **⚠ Experimental Software — Use in a Sandbox**
>
> AutoGUI is a research prototype. It is **not** intended for, nor evaluated or deemed
> suitable for, any particular production use or critical workload. No warranty is
> provided, express or implied.
>
> The agent operates at OS level: it can run shell commands, click anything, type
> anywhere, read and write files, and take screenshots. **Run AutoGUI only in a
> sandbox, VM, or container that you are willing to reset.** Restrict the REST API
> to loopback (`AUTOGUI_API_HOST=127.0.0.1`) and consider disabling shell access
> (`"allowed_shell": false`) if you do not fully trust the task or the model driving
> it. See the [Security Notes](#security-notes) section for further guidance.

---

## Features

| Category | What it does |
|----------|-------------|
| **Planner** | One LLM call up front produces a numbered plan; executor follows it across the ReAct loop. Off-switchable, defaults on |
| **ReAct loop** | Reason → tool call → observe result → repeat, up to configurable iteration limit |
| **Shell** | Run any shell command with timeout, destructive-pattern guard, and confirmation delay |
| **Filesystem** | Read, write (or append), and list files/directories; optional pre-overwrite snapshots |
| **Desktop (pixel)** | Screenshot, click, double-click, type text, hotkeys, scroll, launch apps, list windows |
| **Desktop (a11y-first)** | `desktop_click_element(name, …)` clicks real UI controls via UIAutomation (Windows) and AT-SPI (Linux) — no pixel guessing; macOS AX supported in the Pi extension only |
| **Set-of-Mark grounding** | Numbered overlay on detected elements; the model clicks by id (`desktop_click_mark`) instead of pixel coords |
| **Click-by-text** | OCR-anchored click (`desktop_click_text`); install Tesseract via `scripts/install-dependencies.*` |
| **Browser (Playwright)** | First-class Chromium driver: real DOM/ARIA selectors, `browser_click`, `browser_fill`, `browser_eval` — opt-in via `allowed_browser` |
| **Native input (Windows)** | `click`/`type_text`/`hotkey` go through `user32.SendInput` directly (real INPUT events, correct DPI, full Unicode) |
| **Best-of-N sampling** | On uncertain steps (recent failure or non-APPROVED validator), sample N candidates and pick via self-consistency or a verifier model |
| **Skill library** | `skill_save`/`skill_list`/`skill_run`: persist successful tool sequences and retrieve them by keyword on the next task. `skill_list` / `skill_run` are always available, so existing libraries are readable; `skill_save` (creation) is gated by `agent.skills_enabled` (default false). Each side keeps its own library: standalone agent uses `./skills/`, Pi extension uses `pi-extension/runtime/skills/`. |
| **Trajectory replay** | Per-session JSONL trace + `replay.py` re-runs any saved skill or trace deterministically (no LLM) |
| **Failure recording** | Rolling 5-second screen buffer dumps to an animated GIF on tool failure |
| **State diff & modal flag** | Pre/post-action window-set diff with an `[UNEXPECTED MODAL: …]` banner when an error/permission/confirm dialog appears |
| **Dry-run mode** | Stub all state-changing tools while keeping observation tools live |
| **Action scoping** | `allowed_apps` + `blocked_window_titles` enforced before every GUI action |
| **Platform-aware prompts** | Auto-injects OS-specific instructions (WSL `.exe`, `where.exe`, `which`, etc.) |
| **Startup validation** | Checks API key and model against the live server; prompts to fix or save |
| **Live model picker** | Ctrl+P → "Change Model" fetches the live model list; select and optionally persist to config |
| **Safety countdown** | N-second delay before each tool call; Escape cancels during the window |
| **Hallucination guard** | Detects when the model narrates actions without calling tools; re-prompts |
| **Error retry** | Failed tool calls inject a mandatory-retry message into the history |
| **Step verification** | System prompt instructs the model to verify each result and self-continue |
| **TUI** | Textual-based interactive session with status bar, tool visibility toggle, history save |
| **CLI** | Single-command non-interactive mode for scripting and automation |
| **REST API** | FastAPI HTTP server for programmatic task submission and live event streaming |

---

## Architecture

```
main.py             Entry point — argparse, validation, component wiring, TUI/CLI dispatch
│
├── config.json     Runtime configuration (URL, model, safety, logging, TUI settings)
│
├── client.py       Async OpenWebUI API client (aiohttp, OpenAI-compatible)
│   ├─ chat()           POST /api/chat/completions (or custom api_path)
│   ├─ fetch_models()   GET /api/models — used for validation and model picker
│   └─ health_check()   Connectivity probe
│
├── platform_detect.py  Detect OS/display environment (WSL, Wayland, X11, macOS, Windows)
│
├── backends/           Platform-specific desktop automation backends
│   ├─ base.py          pyautogui baseline (screenshot, click, type, hotkey, scroll)
│   ├─ wsl.py           WSLg display + PowerShell for window list and launch
│   ├─ windows.py       PowerShell + optional uiautomation (accessibility tree)
│   ├─ macos.py         screencapture, osascript, open -a
│   ├─ linux_x11.py     xdotool type override, wmctrl for windows
│   └─ linux_wayland.py grim, ydotool, swaymsg
│
├── tools.py        Tool registry and shell/filesystem implementations
│   ├─ shell_run             Shell command with timeout and destructive guard
│   ├─ fs_read / fs_write / fs_list
│   ├─ desktop_screenshot / click / type / hotkey / scroll / launch / list_windows
│   ├─ desktop_find_element  (Windows UIAutomation, Linux AT-SPI, WSL)
│   ├─ desktop_click_element (a11y-first click — same backends as find_element)
│   ├─ desktop_click_text    (OCR / a11y text match)
│   ├─ desktop_screenshot_marked / desktop_click_mark  (Set-of-Mark)
│   ├─ skill_save / skill_list / skill_run  (persistent macros)
│   ├─ browser_navigate / click / fill / press / get_text / screenshot / eval
│   │   (Playwright; registered when allowed_browser=true)
│   ├─ desktop_get_window_tree (Windows)
│   └─ ToolRegistry          JSON Schema catalog + async dispatch
│
├── agent.py        Agentic loop
│   ├─ Agent.run(input)      Async generator → AgentEvent stream
│   ├─ Agent.reset()         Clear conversation history
│   └─ Guardrails: hallucination detection, error-retry injection, step-continue
│
├── api.py          REST API server (FastAPI)
│   ├─ POST /api/task        Submit task → task_id
│   ├─ GET  /api/task/{id}   Poll task state + steps
│   ├─ GET  /api/task/{id}/stream  SSE live event stream
│   ├─ POST /api/task/{id}/cancel  Cancel running task
│   └─ GET  /api/healthz     Liveness probe
│
├── dry_run.py      DryRunAgent — canned events, no desktop needed
│
├── tui.py          Textual TUI
│   ├─ AgentTUI              Main app (status bar, conversation log, input)
│   ├─ HelpScreen            F1 modal — key bindings + tool list
│   ├─ _ModelPickerCommand   Ctrl+P palette → "Change Model"
│   └─ ModelPickerScreen     Modal — live model list with optional config save
│
└── logs/
    agent.log       Rotating log file
    history.jsonl   Saved conversation history (Ctrl+S in TUI)
```

### Agentic Loop

```
User input
    │
    ▼
Append to message history
    │
    ▼
POST history + tool schemas → OpenWebUI
    │
    ├─ finish_reason == "stop"
    │       └─ Check for narrated actions (hallucination guard)
    │          ├─ Narration detected → inject correction, continue loop
    │          └─ Genuine stop → emit "done"
    │
    ├─ finish_reason == "tool_calls"
    │       └─ For each tool call:
    │            ├─ Safety countdown (N seconds, Escape to cancel)
    │            ├─ dispatch(tool_name, args) → result_json
    │            ├─ If error → append [AGENT POLICY] retry message to history
    │            └─ Append role="tool" result
    │          Loop (up to max_iterations)
    │
    └─ finish_reason == "length" → emit warning + "done"
```

---

## Setup

### Prerequisites

**Python 3.10+**

```bash
python --version
```

**System packages (Linux/WSL only)**

```bash
# X11 desktop tools
sudo apt install python3-tk python3-dev wmctrl xdotool

# Wayland desktop tools
sudo apt install ydotool grim swaymsg
sudo ydotoold &          # start ydotool daemon
```

macOS and Windows require no additional system packages.

**Python packages**

```bash
pip install -r requirements.txt
```

Optional platform-specific packages:

```bash
# Windows: accessibility tree (find elements by name, not pixel position)
pip install uiautomation pywin32

# macOS: richer window metadata
pip install pyobjc-framework-Quartz pyobjc-framework-AppKit
```

### Optional dependencies — install scripts

Optional features (Tesseract for click-by-text, Playwright + Chromium for
the browser tools, Linux AT-SPI for `desktop_click_element`, ImageMagick
for Set-of-Mark overlays + failure GIFs, plus a few platform-specific pip
packages) are installed by **one script per OS** under `scripts/`:

| OS                   | Script                                            |
|----------------------|---------------------------------------------------|
| Linux / macOS / WSL  | `bash scripts/install-dependencies.sh`            |
| Windows              | `scripts\install-dependencies.cmd` (cmd shim)     |
| Windows (PowerShell) | `powershell -ExecutionPolicy Bypass -File scripts\install-dependencies.ps1` |

Each script:
- detects its OS, package manager (apt/dnf/pacman/zypper/brew/winget), and display server (X11 vs Wayland on Linux);
- skips dependencies that are already installed (idempotent);
- echoes every command before running it (loud by design);
- installs the Python deps from `requirements.txt`, plus the optional ones (`pyperclip`, `pytesseract`, `playwright`, `pyobjc-framework-Quartz` on macOS, `uiautomation` + `pywin32` on Windows);
- runs `python -m playwright install chromium`;
- if `pi-extension/` exists, also runs `npm install` (which picks up `playwright` from `optionalDependencies`) and `npx playwright install chromium` inside it.

Either run the script manually before launch, or set this config flag and
AutoGUI will invoke it once at startup before initialising the registry:

```json
{ "install_dependencies": true }
```

The flag is at the top level of `config.json` (not under `agent`/`tools`).
Default is `false` so unmodified setups don't install anything.

**Manual single-package install** if you only want one of the optional
deps:

```bash
# Tesseract (for desktop_click_text / desktop_find_text)
sudo apt install tesseract-ocr   # Debian/Ubuntu/WSL
sudo dnf install tesseract       # Fedora
brew install tesseract           # macOS
winget install UB-Mannheim.TesseractOCR   # Windows
pip install pytesseract

# Playwright + Chromium (for browser_* tools)
pip install playwright
python -m playwright install chromium

# Linux a11y for desktop_click_element
sudo apt install python3-pyatspi gir1.2-atspi-2.0

# ImageMagick (for Set-of-Mark overlay + failure GIFs)
sudo apt install imagemagick     # Linux
brew install imagemagick         # macOS
winget install ImageMagick.ImageMagick   # Windows
```

### Planner (vs. dry-run — they're different things)

**Planner** = one extra LLM call at the start of each task that
produces a numbered, high-level plan (3–8 steps describing goals,
not specific clicks). The plan is injected as a `[PLAN]` block into
the executor's context so every subsequent decision has the full
trajectory in mind. Configured under `agent.planner.enabled`,
defaults on. Same OpenWebUI client as the rest of the agent — one
extra round-trip per task; no separate model required.

**Dry-run** (`safety.dry_run: true`) is a *safety stub*, not a
planner — it returns `{dry_run: true, would_execute: …}` for every
state-changing tool while leaving the real screen unchanged. Useful
for "rehearse a task without touching anything", but not as a
plan-then-execute mechanism: the executor would think each step
succeeded, observe the unchanged real screen, and tie itself in
knots over the contradiction.

If you want plan-first-then-execute semantics, leave dry-run off and
keep the planner on — that's exactly what the planner does.

You can turn the planner off entirely with
`agent.planner.enabled: false` if you don't want the extra round-
trip while debugging.

### A11y-first clicking (most reliable)

`desktop_click_element(name=…, control_type=…)` talks to the real UI
control by name/role via the OS accessibility API instead of clicking
at a guessed pixel position. **Prefer this over `desktop_click`
whenever the target has a visible label** — it survives DPI scaling,
window moves, and async UI redraws.

| Platform     | Backend used                        | Install                                                |
|--------------|-------------------------------------|--------------------------------------------------------|
| Windows      | UIAutomation (`uiautomation` pkg)   | `pip install uiautomation pywin32`                     |
| macOS        | Not available (Pi extension only)   | Use Pi extension for macOS AX element clicking                                         |
| Linux X11    | AT-SPI 2 (`pyatspi`)                | `sudo apt install python3-pyatspi gir1.2-atspi-2.0`    |
| Linux Wayland| AT-SPI 2 (`pyatspi`)                | same as X11                                             |

When the a11y backend isn't available the fallback ladder is:
`desktop_click_text` (OCR/a11y text match) → `desktop_click_mark`
(Set-of-Mark) → `desktop_click(x, y)`. The agent's system prompt
encourages the model to walk this ladder.

### Browser automation (Playwright)

Set `tools.allowed_browser: true` to enable the `browser_*` tool
family — a Playwright-driven Chromium that the agent can navigate,
inspect, and interact with via real DOM/ARIA selectors instead of
pixel coordinates.

Playwright + Chromium are installed by `scripts/install-dependencies.*`
(see "Optional dependencies — install scripts" above). Either run the
script once manually, or set `install_dependencies: true` at the top
of `config.json` to have AutoGUI run it at startup. Until they're
present, the `browser_*` tools register but return a clear error
pointing back at the install script.

Selectors follow Playwright syntax:
- CSS: `button.primary`, `#login-form input[name="email"]`
- Text: `text=Sign in`, `text=/^Continue$/i`
- ARIA role: `role=button[name="Sign in"]`
- XPath: `xpath=//button[contains(.,"Sign in")]`

Use `browser.user_data_dir` to point at a persistent profile if you
want logins/cookies to survive restarts.

### Native input on Windows

On Windows, `desktop_click` / `desktop_type` / `desktop_hotkey` go
through `user32.SendInput` directly via ctypes when available. This
gives you real INPUT events (indistinguishable from a physical
keyboard/mouse), correct per-monitor DPI behaviour, and full Unicode
text input via `KEYEVENTF_UNICODE`. Falls back to pyautogui if
SendInput initialisation fails. No configuration required.

### Typing reliability

`desktop_type` always tries clipboard paste first (one event,
arbitrary length, perfect Unicode), with the platform-correct
modifier — `Cmd+V` on macOS, `Ctrl+V` everywhere else. Only when
the clipboard path fails or `pyperclip` isn't installed does it
fall back to per-character keystrokes. Per-platform fallbacks:

- **Windows / WSL** — `SendInput KEYEVENTF_UNICODE` with a 5 ms
  inter-event pause and 15 ms inter-character pause (slow targets
  used to drop keys at the previous 0 ms cadence).
- **Linux X11** — `xdotool type --clearmodifiers --delay 30` (the
  default 12 ms cadence sometimes loses keys on slow targets,
  producing artefacts like `hello world` → `hello ddddd`).
- **Linux Wayland** — `ydotool type --key-delay 20` with a fallback
  to plain `ydotool type` for older versions.

Every typing call is now logged at INFO level with the actual text
(truncated to 60 chars) and the method that ran, so if a target app
keeps losing keys you can see which path is being used.

The clipboard-paste path saves the user's clipboard before pasting
and restores it afterward, so automation doesn't clobber whatever
the user had copied.

### Best-of-N action sampling

Optional. Set `agent.bon.enabled: true` and on uncertain steps the
agent will sample N candidate completions from the primary model in
parallel, then choose between them via:

1. **Self-consistency** — if a strong majority propose the same first
   tool + arg signature, that's the pick (no extra call).
2. **Verifier** — otherwise the same OpenWebUI client is given a
   one-line summary of each candidate (no tools attached) and asked
   to return only the index of the best one.
3. **Fallback** — any failure path picks the first viable candidate,
   so BoN can never make the agent worse than baseline.

Triggers (also configurable):
- `trigger_on_recent_failure` — last iteration had a failed tool.
- `trigger_on_validator_disagreement` — last validator verdict was
  not `APPROVED`.

Cost: 3–5× tokens on triggered steps. Spend nothing on confident
steps. Defaults are conservative; turn it on when you're chasing the
last ~20% of accuracy.

### Failure recording

A daemon thread maintains a rolling 5-second screen buffer at 5 fps.
On any failed tool call the buffer is flushed to an animated GIF
under `screenshots/failures/`, and a `failure_recording` event is
emitted with the path. Defaults to on; tune via `agent.screen_record`.

### Set-of-Mark grounding

When vision is enabled, the agent uses **Set-of-Mark** screenshots:
numbered boxes are drawn over detected UI elements, and the model
clicks by ID via `desktop_click_mark(mark_id)` instead of guessing
pixel coordinates. The marks come from the OS accessibility tree
where available (Windows UIAutomation, macOS) and from window rects
elsewhere. No setup required — it's on by default.

### Configuration

```bash
cp config.json.example config.json
```

Edit `config.json`:

```json
{
  "openwebui": {
    "base_url": "http://localhost:3000",
    "api_key": "sk-your-key-from-openwebui-settings",
    "model": "llama3.1:70b"
  }
}
```

Your API key: OpenWebUI → **Settings → Account → API Keys**.

The model string must match exactly what appears in your OpenWebUI model list.
If you leave the model wrong, startup validation will offer a menu to pick the right one.

#### Bypassing OpenWebUI — connecting directly to Ollama

If you prefer to skip the OpenWebUI proxy (for example, if the model you want
isn't configured for tool-calling in OpenWebUI, or you don't have admin access
to change that setting), set `api_path` to `/v1/chat/completions` and point
`base_url` at your Ollama instance:

```json
{
  "openwebui": {
    "base_url": "http://localhost:11434",
    "api_path": "/v1/chat/completions",
    "api_key": "",
    "model": "qwen3:14b"
  }
}
```

Ollama exposes an OpenAI-compatible completions endpoint at
`/v1/chat/completions` that AutoGUI targets directly — no OpenWebUI
installation required, and no API key needed.  The `openwebui` config
section name is kept for backwards compatibility; it works for any
OpenAI-compatible endpoint regardless of whether OpenWebUI is involved.

### Verify connectivity

```bash
python main.py --check
```

Prints connection status, configured model, and registered tool list.

---

## REST API

AutoGUI ships a FastAPI REST server that wraps the Agent class, making it
accessible to web UIs, scripts, and CI pipelines without the TUI.

### Install API dependencies

```bash
pip install -r requirements.txt   # fastapi and uvicorn are already included
```

### Start the server

The REST API starts **automatically in the background** whenever you run
`python main.py` (any mode — TUI or single-command). A
`[autogui] REST API listening on http://…` banner is printed to stderr at
startup. You can also start it standalone:

```bash
# With config.json present:
python api.py
# Listening on http://0.0.0.0:8002

# Without a config file — use environment variables:
OPENWEBUI_BASE_URL=http://localhost:3000 \
OPENWEBUI_API_KEY=sk-my-key \
OPENWEBUI_MODEL=llama3.1:70b \
python api.py

# Test without a real desktop or OpenWebUI instance:
AUTOGUI_DRY_RUN=true python api.py
```

### Security and network bind address

> **Warning: the REST API has no authentication and binds to `0.0.0.0`
> (all interfaces) by default.**  This default suits sandbox / container
> environments where network isolation is provided by the runtime.
> **Do not expose the API port to an untrusted network without additional
> access controls.**  For local development, restrict the server to
> loopback (`127.0.0.1`) using the mechanisms below.

| Mechanism | Effect |
|---|---|
| `AUTOGUI_API_HOST=127.0.0.1` | Restrict the API to loopback (recommended for local dev) |
| `AUTOGUI_API_PORT=<port>` | Change the listen port (default `8002`) |
| `AUTOGUI_DISABLE_API=1` | Disable the background API for all `main.py` invocations |

If `fastapi` and `uvicorn` are not installed, the background thread is
silently skipped and the main agent works normally.

### Docker

```bash
# Real agent (needs X11 and OpenWebUI):
docker run -p 8002:8002 \
  -e DISPLAY=$DISPLAY \
  -v /tmp/.X11-unix:/tmp/.X11-unix \
  -e OPENWEBUI_BASE_URL=http://host.docker.internal:3000 \
  -e OPENWEBUI_API_KEY=sk-my-key \
  autogui python api.py

# Dry-run (no display or OpenWebUI needed):
docker run -p 8002:8002 -e AUTOGUI_DRY_RUN=true autogui python api.py
```

### Quick curl example

```bash
# Submit a task
TASK_ID=$(curl -s -X POST http://localhost:8002/api/task \
  -H 'Content-Type: application/json' \
  -d '{"task": "Take a screenshot of the desktop"}' \
  | python3 -c 'import sys,json; print(json.load(sys.stdin)["task_id"])')

# Stream live events
curl -N http://localhost:8002/api/task/$TASK_ID/stream

# Or poll for the finished result
curl -s http://localhost:8002/api/task/$TASK_ID | python3 -m json.tool
```

Full endpoint reference, SSE event format, and all environment variables: **[docs/REST_API.md](docs/REST_API.md)**

Interactive docs (once the server is running):
- Swagger UI: `http://localhost:8002/docs`
- ReDoc: `http://localhost:8002/redoc`

---

## Usage

### Pi extension

The native Pi extension lives in `pi-extension/` and is implemented entirely in
TypeScript. It does not use OpenWebUI or the standalone Python agent loop.

```bash
cd pi-extension
npm install
npm run typecheck
pi -e ./src/index.ts
```

Inside Pi:

```text
/autogui Open a harmless app and describe what you see
```

When an `/autogui` task completes naturally, the extension auto-spawns a
read-only Pi validator in a fresh tmux session (only screenshot and window-
listing tools active) to double-check the desktop state.  Set
`validateAfterAutogui: false` in the extension's `config.json` to skip the
follow-up.

See `pi-extension/README.md` for the full extension details.

### Skill library

Skills are named, replayable sequences of successful tool calls — saved
with `skill_save`, listed with `skill_list`, replayed with `skill_run`
(or `replay.py` outside the agent loop).

**`skills_enabled` controls *creation* only.**  Reads are always
allowed:

| `skills_enabled` | `skill_list` | `skill_run` | `skill_save` | Candidate suggestion at task start |
|------------------|--------------|-------------|--------------|-----------------------------|
| `false` (default) | ✓ | ✓ | — (not registered) | ✓ |
| `true`            | ✓ | ✓ | ✓ | ✓ |

So a fresh checkout never writes a `skills/` directory until you opt
in, but if you copy in an existing `skills.jsonl` (or someone else
on the same machine has already created one) it remains usable
immediately.

```jsonc
{
  "agent": {
    "skills_enabled": false,            // default — no NEW skills are written
    "skills_path": "skills/skills.jsonl"
  }
}
```

The standalone agent stores skills at `./skills/skills.jsonl` relative
to the project root.  This path is **deliberately separate** from the
Pi extension's library at `pi-extension/runtime/skills/skills.jsonl` —
each side manages its own library so they don't shadow each other.
Both directories are git-ignored and created lazily the first time
`skill_save` fires (no creation = no directory).

| Side | Default skill path | Config key (creation gate) |
|------|--------------------|----------------------------|
| Standalone Python agent | `skills/skills.jsonl` | `agent.skills_enabled` |
| Pi extension | `pi-extension/runtime/skills/skills.jsonl` | `skillsEnabled` (in `pi-extension/config.json`) |

If you want a single shared library across both programs, point
`skills_path` and `skillsPath` at the same absolute path — the default
is to keep them separate so each program's library is private.

### App-memory library

Per-app quirk database — failure histograms, success counts, and free-form
notes attached to an app via `memory_note(app, text)`.  Surfaced into the
planner as "app memory hints" for any apps visible at task start so plans
bias toward strategies that worked before.

**`agent.memory.enabled` controls *creation* only.**  Same pattern as
`skills_enabled`:

| `memory.enabled` | `memory_get` | planner hints | controller auto-records | `memory_note` | `memory/` dir on disk |
|---|---|---|---|---|---|
| `false` (default) | ✓ | ✓ (reads existing) | — | — (not registered) | none until user opts in |
| `true` | ✓ | ✓ | ✓ | ✓ | created lazily on first write |

```jsonc
{
  "agent": {
    "memory": {
      "enabled": false,        // default — no NEW records are written
      "dir": "memory"          // standalone-agent quirk database
    }
  }
}
```

The standalone agent stores at `./memory/`; the Pi extension stores at
`pi-extension/runtime/memory/`.  Both directories are git-ignored and
created lazily the first time `memory_note` (or the controller's auto-
recorder) actually fires.  Point both at the same absolute path if you
want a single shared quirk database across both programs.

| Side | Default memory path | Config key (creation gate) |
|------|---------------------|----------------------------|
| Standalone Python agent | `memory/` | `agent.memory.enabled` |
| Pi extension | `pi-extension/runtime/memory/` | `memoryEnabled` (in `pi-extension/config.json`) |

### Runtime directories

The standalone Python agent creates runtime directories as needed:

| Path | Contents |
|------|----------|
| `logs/` | `agent.log` (rotating) + per-session `session_<ts>.log` files |
| `logs/traces/` | Per-task JSONL trajectory logs |
| `logs/artifacts/` | Artifact bodies + `index.jsonl`. Stable-id store: each capture gets a fresh `artifact://<id>` even when the body is identical to a prior capture. |
| `logs/progress/` | Per-task JSON progress records (auto-resume keyed by task hash) |
| `memory/` | **Per-app quirk store** — `memory/<app>.json` + `memory/index.jsonl`. Only created the first time `memory_note` runs or the controller auto-records, which requires `agent.memory.enabled=true`. Reads via `memory_get` work regardless. |
| `screenshots/` | Ad-hoc screenshots taken by the agent |
| `screenshots/failures/` | Animated GIF failure recordings |
| `skills/` | **Skill library** — `skills/skills.jsonl` (only created the first time `skill_save` runs, which requires `skills_enabled=true`) |

The Pi extension writes runtime files under `pi-extension/runtime/`:

| Path | Contents |
|------|----------|
| `pi-extension/runtime/skills/` | **Skill library** — `skills.jsonl` (only created when `skillsEnabled=true` and `skill_save` fires; reads are always allowed) |
| `pi-extension/runtime/traces/` | Per-session JSONL trajectory logs |
| `pi-extension/runtime/artifacts/` | Artifact bodies + `index.jsonl` (stable-id, not deduped). |
| `pi-extension/runtime/progress/` | Per-task JSON progress records |
| `pi-extension/runtime/memory/` | **Per-app quirk store** — `<app>.json` + `index.jsonl`. Created lazily when `memoryEnabled=true` and a write fires; reads via `memory_get` work regardless. |
| `pi-extension/runtime/screenshots/` | Ad-hoc screenshots |
| `pi-extension/runtime/failures/` | Animated GIF failure recordings |
| `pi-extension/runtime/logs/` | `autogui.log` |

All `pi-extension/runtime/` paths are git-ignored.

### Interactive TUI

```bash
python main.py
```

The TUI shows a scrollable conversation pane with a status bar at the bottom.
The status bar always shows the current model name, conversation length, and tool
visibility state.

#### Key Bindings

| Key | Action |
|-----|--------|
| **Enter** | Submit input |
| **Ctrl+P** | Command palette — type "model" → select **Change Model** |
| **Ctrl+R** | Reset conversation history |
| **Ctrl+S** | Save history to `logs/history.jsonl` |
| **Ctrl+T** | Toggle tool call/result visibility |
| **Escape** | Cancel current task (best-effort) |
| **F1** | Help overlay (key bindings + tool list) |
| **Ctrl+C** | Exit |

### Single-command mode

```bash
python main.py "List all Python files in ~/projects and show me their sizes"

python main.py "Open Notepad and type Hello World"

python main.py --no-desktop "Summarize ~/Documents/notes.txt"

python main.py --quiet "Run tests in ~/myproject"

python main.py --model mistral:7b "What files are in the current directory?"
```

### CLI flags

| Flag | Description |
|------|-------------|
| `--config PATH` | Use a custom config file (default: `config.json`) |
| `--model MODEL` | Override model for this session only |
| `--no-desktop` | Disable mouse/keyboard/screenshot tools |
| `--no-shell` | Disable shell execution |
| `--no-tools` | Disable all tools (pure chat mode) |
| `--verbose` | DEBUG-level logging to stderr and log file |
| `--quiet` | Suppress tool call/result output (single-command mode) |
| `--check` | Connectivity health check, then exit |

---

## Startup Validation

Every time the agent starts it runs a validation sequence before opening the TUI:

1. **API key** — if the key is unset or a placeholder, prompts for one (hidden input).
2. **Connection** — calls `/api/models` to verify the key and server are reachable.
   - HTTP 401 → re-prompts for the key (up to 3 attempts).
   - Connection refused / timeout → prompts for a new `base_url`.
3. **Model check** — if the configured model is not in the server's model list, shows
   a numbered menu so you can pick one.

After each successful check you are offered the option to save the new value to
`config.json` (API key, base URL, or model), so you only need to do this once.

---

## Model Picker

Open the model picker via **Ctrl+P** (the command palette) — type "model" and select
**Change Model**:

```
┌─ Select Model ───────────────────────────────────────────────┐
│ 12 models  ·  ↑↓ to navigate                    │
│ ┌─────────────────────────────────────────────┐ │
│ │ llama3.1:70b  ●                             │ │  ← current model (green dot)
│ │ llama3.2:latest                             │ │
│ │ mistral:7b                                  │ │
│ │ phi3:mini                                   │ │
│ └─────────────────────────────────────────────┘ │
│ [ ] Save selection to config.json               │
│                        [Select]  [Cancel]       │
└─────────────────────────────────────────────────┘
```

- The currently active model is highlighted with a green dot.
- Selecting a model takes effect immediately for the next message.
- Checking **Save selection to config.json** persists the choice so it survives restarts.
- Press **Escape** or **Cancel** to close without changing the model.

---

## Robustness, planning & verification (controller-only)

`agent.controller.enabled` defaults to **true**, so all of this runs by
default; set it to false to fall back to the legacy single-loop ReAct
executor.  When the controller is on it layers
several extra safeguards on top of the standard ReAct loop.  Each is
individually toggleable so you can dial in the tradeoff between speed
and reliability.

| Knob | Default | What it does |
|------|---------|-------------|
| `agent.controller.critique_enabled` | `true` | Adds one extra LLM call after the planner that critiques the plan and returns a revised version when issues are found. Catches plan-level mistakes (missing steps, vague post-conditions, wrong dependencies) before any UI is touched. |
| `agent.controller.preflight_enabled` | `true` | Before the first state-changing action, verifies that resources the plan needs are available: apps on PATH, files present, URLs TCP-reachable, named tools registered, probe commands exit 0. Tasks abort with a structured `preflight_failed` event when something is missing. |
| `agent.controller.predicate_check_enabled` | `true` | When a plan step declares a typed `predicate` (`window_title_contains`, `file_exists`, `url_contains`, `text_visible`, `process_running`, `shell_returns`, …), the controller verifies it deterministically after `STEP_DONE`. A miss demotes the verdict to BLOCKED and triggers replan via the standard failure-classification path. |
| `agent.controller.visual_diff_enabled` | `true` | When vision is on, hashes each pre/post screenshot pair via a 16×16 perceptual ("dHash") hash and tags the tool result with `verifier.visual_diff` when a state-changing action moved fewer than ~12% of bits — i.e. the screen barely changed. Catches the silent-no-op failure mode that exit-code checks miss. |
| `agent.controller.watchdog_stall_threshold` | `3` | Hashes `(window list, active window, first proposed tool, first args)` per iteration. When the same signature recurs N times in a row the step is flagged as stuck and routed through the standard BLOCKED path. `0` disables. |
| `agent.budget.max_*` | `0` | Hard ceilings for tool calls / chat calls / total tokens / seconds.  When any ceiling is exceeded a `budget_exceeded` event fires and the task ends before the next step runs. |
| `agent.memory.enabled` | `false` | **Creation gate.** When false (the default) `memory_note` is not registered, the controller does NOT auto-record successes/failures, and no `memory/` directory is created. `memory_get` and the planner's app-memory hints continue to read whatever is already on disk, so an existing quirk database stays useful even when creation is off. Set to `true` to allow new records. Mirrors the `agent.skills_enabled` flag. |
| `agent.memory.dir` | `memory/` | Per-app quirk store location (`memory/<app>.json`). Created lazily the first time something is written. The pi extension keeps its own quirk database under `pi-extension/runtime/memory/` so the two libraries don't shadow each other (point both at the same absolute path if you want them merged). |

The planner also receives **few-shot exemplars** from the skill library
(top-3 matches by keyword) and **app memory hints** for any visible
apps, so plans are biased by what previously succeeded against the
same software.

`replay.py --drift-check` re-runs a saved skill while comparing the
live post-state against the windows + perceptual screen hash recorded
when the skill was first captured (`step.drift_anchor`).  Drift
between rounds is logged so you know when a recipe has gone stale
without having to re-record it from scratch.

The pi extension exposes the same primitives as tools: `check_predicate`,
`preflight`, `memory_get` / `memory_note`, `budget_status`,
`classify_failure`, `desktop_wait_for`.  Pi owns the LLM loop, so the
controller protocol injected into the system prompt instructs Pi's
agent to call them at the right beats (preflight up front, predicate
check before STEP_DONE, etc.) rather than the extension running them
implicitly.

## Test harness

A pytest suite under `tests/` exercises the controller / artifacts /
predicates / failures / app memory / budget / preflight / watchdog /
visual diff modules with no live model and no desktop backend
required:

```bash
pip install pytest pytest-asyncio
python -m pytest
```

The tests use mocked `OpenWebUIClient` and `ToolRegistry` stubs (see
`tests/conftest.py`) to drive `Agent._run_with_controller` end-to-end,
including a budget-exhaustion case that proves the ceiling stops the
loop before the next step runs.  Run this on every controller change
to catch regressions in the orchestration logic without burning real
model calls.

## Safety Guardrails

### Destructive command guard

`shell_run` refuses any command matching patterns like `rm -rf`, `format`, `dd if=`,
`DROP TABLE`, etc.  The model is told to get user confirmation before running destructive
commands.

### Tool execution countdown

Before dispatching a tool call the agent waits N seconds (configured via
`safety.command_confirm_delay_seconds`).  During this window you can cancel:

**TUI** — status bar shows a progress bar; press **Escape**.

**CLI** — countdown printed inline; press **Escape** or **Ctrl+C**.

```
  ⏳ [████░] shell_run: executing in 1s  (Esc / Ctrl+C to cancel)
```

Set `"command_confirm_delay_seconds": 0` to disable the countdown and execute
immediately.

### Hallucination guard

The agent monitors stop-responses for phrases like "I clicked", "I typed", "I ran"
without corresponding tool calls.  When detected, it injects a correction into the
history and continues the loop, forcing the model to issue the actual tool calls.

### Error retry enforcement

When a tool returns an error, non-zero exit code, or timeout, the agent appends an
`[AGENT POLICY] The tool call above FAILED` message to the history.  This prevents
the model from acknowledging the error and moving on — it must diagnose and retry
the same step.

---

## Platform Support

| Platform | Screenshot | Click/Type | Hotkey | Windows | Launch | Find Element |
|----------|-----------|------------|--------|---------|--------|----------|
| **WSL (WSLg)** | pyautogui | pyautogui | pyautogui | PowerShell | PowerShell | PowerShell UIAutomation |
| **Windows** | pyautogui | pyautogui | pyautogui | PowerShell | PowerShell | uiautomation (optional) |
| **macOS** | screencapture | pyautogui | pyautogui | osascript | open -a | osascript |
| **Linux X11** | pyautogui | pyautogui/xdotool | pyautogui | wmctrl | subprocess | — |
| **Linux Wayland** | grim | ydotool | ydotool | swaymsg | subprocess | — |

The correct backend is selected automatically at startup via `platform_detect.detect()`.
No configuration is needed.

**WSL note:** The agent automatically detects WSL and instructs the model to append
`.exe` to Windows programs and search `/mnt/c` when a binary is not on the PATH.

---

## Configuration Reference

```jsonc
{
  "openwebui": {
    "base_url": "http://localhost:3000",          // OpenWebUI server URL (or Ollama: http://localhost:11434)
    "api_key": "sk-...",                          // API key (Settings → Account → API Keys); "" for Ollama
    "model": "llama3.1:70b",                     // Model ID — must match /api/models list
    "api_path": "/api/chat/completions",          // Completions path. Use "/v1/chat/completions" to bypass OpenWebUI and call Ollama directly
    "temperature": 0.2,                           // Sampling temperature (0–1)
    "max_tokens": 4096,                           // Max completion tokens per call
    "timeout_seconds": 120                        // Per-request timeout
  },
  "install_dependencies": false,          // True = run scripts/install-dependencies.* at startup
  "agent": {
    "max_iterations": 30,                 // Hard stop after N agentic loop iterations
    "confirm_destructive": true,         // Block shell commands matching destructive regex patterns
    "vision_screenshots": true,          // Send screenshots to vision-capable models
    "record_trace": true,                // Persist every event to logs/traces/<session>.jsonl
    "trace_dir": "logs/traces",          // Where the JSONL trajectory log lives
    "skills_enabled": false,             // CREATION gate. False (default) blocks skill_save; skill_list/skill_run/candidate-suggestion still work
    "suggest_skills": true,              // Offer top-K saved skills at task start
    "skills_path": "skills/skills.jsonl",  // Standalone-agent skill library; deliberately distinct from
                                            //   pi-extension/runtime/skills/skills.jsonl so each side has its own
    "planner": {                          // Pre-execution planning pass
      "enabled": true                     // One extra LLM call up front (uses the primary client)
    },
    "controller": {                       // Typed-plan + step-by-step executor (default ON)
      "enabled": true,
      "step_max_iterations": 8,           // Per-step iteration ceiling (separate from max_iterations)
      "step_max_retries": 2,
      "auto_resume": true,                // Resume completed step ids from logs/progress
      "replan_on_block": true,
      "critique_enabled": true,           // Extra LLM call to review the plan
      "preflight_enabled": true,          // Verify apps/files/URLs/tools/commands before acting
      "predicate_check_enabled": true,    // Verify typed post-conditions deterministically
      "visual_diff_enabled": true,        // Perceptual-hash diff to flag silent-no-op actions
      "watchdog_stall_threshold": 3       // 0 disables; flag step stuck after N identical signatures
    },
    "artifacts": {"dir": "logs/artifacts"},  // Stable-id observation store (append-only; not deduped)
    "progress":  {"dir": "logs/progress"},   // Per-task resume markers
    "memory": {                              // Per-app quirk database (separate from skills)
      "enabled": false,                      //   CREATION gate. False blocks memory_note + auto-recording;
                                              //   memory_get and planner hints still read whatever is on disk
      "dir": "memory"                        //   Distinct from pi-extension/runtime/memory/
    },
    "budget": {                              // Hard ceilings; 0 = no ceiling
      "max_tool_calls": 0,
      "max_chat_calls": 0,
      "max_total_tokens": 0,
      "max_seconds": 0
    },
    "bon": {                              // Best-of-N action sampling
      "enabled": true,                    // Samples n completions, picks best on uncertain steps
      "n": 3,                             // Number of candidates to sample
      "temperature": 0.7,                 // Sampling temperature for diverse candidates
      "trigger_on_recent_failure": true,
      "trigger_on_validator_disagreement": true
    },
    "screen_record": {                    // Rolling screen buffer
      "enabled": true,                    // Capture into a deque while running
      "fps": 5,                           // Frames per second
      "buffer_seconds": 5.0,              // Length of rolling window in seconds
      "max_width": 960,                   // Downscale before storing
      "out_dir": "screenshots/failures"   // GIFs are written here on tool failure
    }
  },
  "tools": {
    "shell_timeout_seconds": 30,          // Per-command shell timeout
    "screenshot_dir": "screenshots",      // Directory for saved screenshots
    "max_screenshot_width": 1280,         // Resize screenshots wider than this (px)
    "perception_cache_ttl_seconds": 0.5,  // Reuse the last screenshot for this long
    "allowed_shell": true,               // Enable shell_run tool
    "allowed_filesystem": true,          // Enable fs_read / fs_write / fs_list
    "allowed_desktop": true,             // Enable all desktop/* tools
    "allowed_browser": false             // Playwright browser_* tools
  },
  "browser": {                            // Settings for the Playwright backend
    "headless": false,                    // Run with a visible window
    "screenshot_dir": "screenshots/browser",
    "user_data_dir": "",                 // Non-empty path = persistent profile (keeps logins)
    "viewport": {"width": 1280, "height": 800}
  },
  "logging": {
    "level": "INFO",                     // Log level for file handler
    "file": "logs/agent.log",            // Log file path
    "max_bytes": 10485760,               // Rotate at 10 MB
    "backup_count": 3                    // Keep 3 rotated files
  },
  "tui": {
    "theme": "dark",                     // Textual theme
    "show_tool_calls": true,             // Show tool calls in conversation pane by default
    "show_token_counts": false,          // (reserved)
    "history_file": "logs/history.jsonl" // Ctrl+S saves here
  },
  "safety": {
    "command_confirm_delay_seconds": 5,  // Countdown before each tool call (0 = off)
    "dry_run": false,                    // True = state-changing tools return a stub
                                          //   {dry_run, would_execute} instead of running
    "allowed_apps": [],                  // Restrict GUI actions to these apps; empty = unrestricted
    "blocked_window_titles": [],         // Regex patterns; matching active window blocks GUI tools
    "fs_write_snapshot_dir": ""           // Non-empty path = back up files before fs_write overwrite
  }
}
```

---

## Extending the Tool Set

**1.** Implement an async function in `tools.py`:

```python
async def my_tool(param: str) -> dict:
    try:
        result = do_something(param)
        return {"success": True, "result": result}
    except Exception as e:
        return {"error": str(e)}
```

**2.** Register it in `ToolRegistry._build()`:

```python
self._register(
    {"type": "function", "function": {
        "name": "my_tool",
        "description": "Does something useful — the LLM reads this.",
        "parameters": {"type": "object",
                       "properties": {"param": {"type": "string"}},
                       "required": ["param"]},
    }},
    my_tool,
)
```

**3.** Gate it on a config key if needed (check `self._tools_cfg.get("allowed_my_tool", True)`).

---

## Security Notes

- **Shell access** — the destructive guard is not a sandbox.  For untrusted tasks set
  `"allowed_shell": false` or run the agent in a container.
- **API key** — restrict config.json permissions: `chmod 600 config.json`.  The file is
  excluded from git via `.gitignore`.
- **Desktop control** — the agent operates at OS level: it can click anything and type
  anywhere.  Only run on machines and accounts where you accept this capability.
- **REST API** — no authentication is enforced; the server binds to `0.0.0.0` by default
  (all interfaces).  Set `AUTOGUI_API_HOST=127.0.0.1` for loopback-only use, or
  `AUTOGUI_DISABLE_API=1` to disable the background API entirely.  See
  [docs/REST_API.md](docs/REST_API.md) for details.

---

## License

MIT — use freely; attribution appreciated.
