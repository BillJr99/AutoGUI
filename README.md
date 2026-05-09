# OpenWebUI Desktop Agent

A Python CLI/TUI agent that connects any [OpenWebUI](https://openwebui.com/) instance
to your desktop.  The agent drives a ReAct-style loop (Reason → Act → Observe → repeat)
and can run shell commands, read/write files, take screenshots, click, type, launch
programs, and inspect accessibility trees — all via function-calling with any model
available in your OpenWebUI instance.

Architecturally it follows [UFO](https://github.com/microsoft/UFO) and
[open-interpreter](https://github.com/OpenInterpreter/open-interpreter) but is vendor-neutral:
any model that supports OpenAI-compatible tool calling and is registered in your OpenWebUI
works out of the box.

---

## Features

| Category | What it does |
|----------|--------------|
| **ReAct loop** | Reason → tool call → observe result → repeat, up to configurable iteration limit |
| **Shell** | Run any shell command with timeout, destructive-pattern guard, and confirmation delay |
| **Filesystem** | Read, write (or append), and list files/directories |
| **Desktop** | Screenshot, click, double-click, type text, hotkeys, scroll, launch apps, list windows |
| **Accessibility** | Find UI elements by name/type (Windows UIAutomation, macOS osascript, WSL) |
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
│   ├─ desktop_find_element  (Windows, macOS, WSL — when backend supports it)
│   ├─ desktop_get_window_tree (Windows, macOS)
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
│ ☐ Save selection to config.json                 │
│                        [Select]  [Cancel]        │
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
  "agent": {
    "max_iterations": 30,                 // Hard stop after N agentic loop iterations
    "system_prompt": "...",              // Full system prompt (see config.json.example)
    "confirm_destructive": true          // Block shell commands matching destructive patterns
  },
  "tools": {
    "shell_timeout_seconds": 30,          // Per-command shell timeout
    "screenshot_dir": "screenshots",      // Directory for saved screenshots
    "max_screenshot_width": 1280,         // Resize screenshots wider than this (px)
    "allowed_shell": true,               // Enable shell_run tool
    "allowed_filesystem": true,          // Enable fs_read / fs_write / fs_list
    "allowed_desktop": true,             // Enable all desktop/* tools
    "allowed_browser": false             // Playwright browser tools (not yet implemented)
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
    "command_confirm_delay_seconds": 5   // Countdown before each tool call (0 = off)
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
