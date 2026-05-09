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

---

## Features

| Category | What it does |
|----------|--------------|
| **Planner** | One LLM call up front produces a numbered plan; executor follows it across the ReAct loop. Off-switchable, defaults on |
| **ReAct loop** | Reason → tool call → observe result → repeat, up to configurable iteration limit |
| **Shell** | Run any shell command with timeout, destructive-pattern guard, and confirmation delay |
| **Filesystem** | Read, write (or append), and list files/directories; optional pre-overwrite snapshots |
| **Desktop (pixel)** | Screenshot, click, double-click, type text, hotkeys, scroll, launch apps, list windows |
| **Desktop (a11y-first)** | `desktop_click_element(name, …)` clicks real UI controls via UIAutomation (Windows), AT-SPI (Linux), and AppleScript (macOS) — no pixel guessing |
| **Set-of-Mark grounding** | Numbered overlay on detected elements; the model clicks by id (`desktop_click_mark`) instead of pixel coords |
| **Click-by-text** | OCR-anchored click (`desktop_click_text`) with optional auto-install of Tesseract |
| **Browser (Playwright)** | First-class Chromium driver: real DOM/ARIA selectors, `browser_click`, `browser_fill`, `browser_eval` — opt-in via `allowed_browser` |
| **Native input (Windows)** | `click`/`type_text`/`hotkey` go through `user32.SendInput` directly (real INPUT events, correct DPI, full Unicode) |
| **Best-of-N sampling** | On uncertain steps (recent failure or non-APPROVED validator), sample N candidates and pick via self-consistency or a verifier model |
| **Two-tier model** | Optional cheap `openwebui_fast` client for the validator/verifier; primary client owns user-facing turns |
| **Skill library** | `skill_save`/`skill_list`/`skill_run`: persist successful tool sequences, retrieve them by keyword on the next task |
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

---

## Architecture

```
main.py             Entry point — argparse, validation, component wiring, TUI/CLI dispatch
│
├── config.json     Runtime configuration (URL, model, safety, logging, TUI settings)
│
├── client.py       Async OpenWebUI API client (aiohttp, OpenAI-compatible)
│   ├─ chat()           POST /api/chat/completions
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

### OCR (click-by-text fallback)

`desktop_click_text` and `desktop_find_text` use the accessibility tree on
Windows/macOS. On Linux/X11/Wayland, or as a fallback when an element
isn't exposed in the a11y tree, they use OCR via Tesseract. Setting up
OCR is **optional** — without it the tools still work, they just return
a "please install" message instead of a click.

**Auto-install (recommended).** Set the config flag and AutoGUI handles
the rest at startup:

```jsonc
{
  "tools": {
    "auto_install_tesseract": true
  }
}
```

On first launch with the flag enabled, AutoGUI runs the platform-native
installer, then `pip install pytesseract`:

| Platform        | Command issued                                                   |
|-----------------|------------------------------------------------------------------|
| Linux (Debian)  | `sudo apt-get install -y tesseract-ocr`                          |
| Linux (Fedora)  | `sudo dnf install -y tesseract`                                  |
| Linux (Arch)    | `sudo pacman -S --noconfirm tesseract`                           |
| Linux (SUSE)    | `sudo zypper install -y tesseract-ocr`                           |
| WSL             | uses the Linux side's `apt-get` (binary lives inside WSL)        |
| macOS           | `brew install tesseract` (Homebrew must be installed first)      |
| Windows         | `winget install --id=UB-Mannheim.TesseractOCR --silent`          |

Each step is logged to stdout (`[tesseract_install] $ …`) so you can
see exactly what's running. Auto-install attempts at most once per
process; if the install fails, the message tells you why and you can
fix it manually.

**Manual install** if you'd rather not enable the flag:

```bash
# Linux / WSL
sudo apt install tesseract-ocr && pip install pytesseract

# macOS
brew install tesseract && pip install pytesseract

# Windows (PowerShell as admin)
winget install UB-Mannheim.TesseractOCR
pip install pytesseract
# Then add C:\Program Files\Tesseract-OCR to PATH if winget didn't.
```

### Planner (vs. dry-run — they're different things)

**Planner** = one extra LLM call at the start of each task that
produces a numbered, high-level plan (3–8 steps describing goals,
not specific clicks). The plan is injected as a `[PLAN]` block into
the executor's context so every subsequent decision has the full
trajectory in mind. Configured under `agent.planner.enabled`,
defaults on. The planner uses `openwebui_fast` when configured.

**Dry-run** (`safety.dry_run: true`) is a *safety stub*, not a
planner — it returns `{dry_run: true, would_execute: …}` for every
state-changing tool while leaving the real screen unchanged. Useful
for "rehearse a task without touching anything", but not as a
plan-then-execute mechanism: the executor would think each step
succeeded, observe the unchanged real screen, and tie itself in
knots over the contradiction.

If you want plan-first-then-execute semantics, leave dry-run off and
keep the planner on — that's exactly what the planner does.

### A11y-first clicking (most reliable)

`desktop_click_element(name=…, control_type=…)` talks to the real UI
control by name/role via the OS accessibility API instead of clicking
at a guessed pixel position. **Prefer this over `desktop_click`
whenever the target has a visible label** — it survives DPI scaling,
window moves, and async UI redraws.

| Platform     | Backend used                        | Install                                                |
|--------------|-------------------------------------|--------------------------------------------------------|
| Windows      | UIAutomation (`uiautomation` pkg)   | `pip install uiautomation pywin32`                     |
| macOS        | AppleScript / AX (`pyobjc`)         | `pip install pyobjc-framework-Quartz pyobjc-framework-AppKit` (built-in if installed) |
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

The first time you start AutoGUI with browser tools enabled it will
detect that Playwright + Chromium are missing and install them for
you. Loud by design — every command prints to stdout so you can see
what gets installed and where. Set `tools.auto_install_playwright:
false` to opt out and install manually.

**What auto-install runs** (`tesseract_install.py` and
`playwright_install.py` are the source of truth). Run any of these
yourself if you want to do it by hand:

```bash
# Playwright + Chromium (auto-install does these in order)
pip install playwright
python -m playwright install chromium

# Tesseract — manual install
# Linux (Debian/Ubuntu)
sudo apt-get update && sudo apt-get install -y tesseract-ocr
# Linux (Fedora)
sudo dnf install -y tesseract
# Linux (Arch)
sudo pacman -S --noconfirm tesseract
# Linux (openSUSE)
sudo zypper install -y tesseract-ocr
# macOS
brew install tesseract
# Windows (PowerShell as admin)
winget install --id=UB-Mannheim.TesseractOCR --silent --accept-package-agreements --accept-source-agreements
# Then in every case
pip install pytesseract
```

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

### Best-of-N action sampling

Optional. Set `agent.bon.enabled: true` and on uncertain steps the
agent will sample N candidate completions from the primary model in
parallel, then choose between them via:

1. **Self-consistency** — if a strong majority propose the same first
   tool + arg signature, that's the pick (no extra call).
2. **Verifier** — otherwise the `openwebui_fast` client (or the
   primary, if no fast client is configured) is given a one-line
   summary of each candidate and asked which to use.
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

### Verify connectivity

```bash
python main.py --check
```

Prints connection status, configured model, and registered tool list.

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

For a read-only second pass, `/autogui-validate <task>` spawns a separate Pi
validator in a tmux session with only screenshot and window-listing tools active.

See `pi-extension/README.md` for the full extension details.

### Runtime directories

The standalone Python agent creates runtime directories as needed:

- `logs/` is created before `logs/agent.log` is opened.
- The parent directory for `logs/history.jsonl` is created when TUI history is saved.
- `screenshots/` is created by the active screenshot backend before saving a screenshot.

The Pi extension creates `pi-extension/runtime/screenshots/` recursively before
saving screenshots. These runtime paths are ignored by git.

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
┌─ Select Model ──────────────────────────────────┐
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
|----------|-----------|------------|--------|---------|--------|--------------|
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
    "base_url": "http://localhost:3000",   // OpenWebUI server URL
    "api_key": "sk-...",                   // API key (Settings → Account → API Keys)
    "model": "llama3.1:70b",              // Model ID — must match /api/models list
    "temperature": 0.2,                   // Sampling temperature (0–1)
    "max_tokens": 4096,                   // Max completion tokens per call
    "timeout_seconds": 120               // Per-request timeout
  },
  "openwebui_fast": {                     // Optional second client used only for the
    "base_url": "...",                    //   coherence validator (cheaper, faster
    "api_key":  "...",                    //   model). Omit this whole block to reuse
    "model":    "llama3.1:8b"             //   the primary client for everything.
  },
  "agent": {
    "max_iterations": 30,                 // Hard stop after N agentic loop iterations
    "confirm_destructive": true,         // Block shell commands matching destructive regex patterns
    "vision_screenshots": true,          // Send screenshots to vision-capable models
    "record_trace": true,                // Persist every event to logs/traces/<session>.jsonl
    "trace_dir": "logs/traces",          // Where the JSONL trajectory log lives
    "suggest_skills": true,              // Offer top-K saved skills at task start
    "skills_path": "~/.autogui/skills.jsonl",  // Skill library location
    "planner": {                          // Pre-execution planning pass
      "enabled": true                     // One extra LLM call up front; uses openwebui_fast when set
    },
    "bon": {                              // Best-of-N action sampling
      "enabled": false,                   // Off by default — multiplies token cost on uncertain steps
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
    "auto_install_tesseract": false,      // True = run the platform installer on startup
    "auto_install_playwright": true,      // Install Playwright + Chromium when allowed_browser=true (default true)
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

---

## License

MIT — use freely; attribution appreciated.
