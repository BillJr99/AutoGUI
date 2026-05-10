# AutoGUI Pi Extension

Native TypeScript extension for [Pi Coding Agent](https://pi.dev/) that adds desktop automation tools while leaving Pi in charge of the agent workflow.

This extension is decoupled from OpenWebUI. It does not create a model client, model picker, custom ReAct loop, or TUI. Pi supplies the model/provider, session history, tool orchestration, and UI.

## What It Adds

- `/autogui <task>`: sends a prepared desktop-automation prompt into Pi's normal agent loop.  When the task completes naturally and `validateAfterAutogui` is on (default), a read-only Pi validator is auto-spawned in a fresh tmux pane to double-check the desktop state.
- `/autogui-abort`: aborts the current AutoGUI/Pi agent operation.
- `/desktop-status`: reports the detected backend, capabilities, and config snapshot.
- Desktop tools (all platforms):
  - `desktop_screenshot`, `desktop_screenshot_marked`
  - `desktop_click`, `desktop_click_mark`, `desktop_click_text`, `desktop_click_element`
  - `desktop_find_text`
  - `desktop_type`, `desktop_hotkey`, `desktop_scroll`
  - `desktop_list_windows`, `desktop_active_window`, `desktop_focus_window`
  - `desktop_launch`, `desktop_get_cursor_pos`, `desktop_mouse_move`
  - `desktop_get_window_text`
- Skill library (replayable macros — `skillsEnabled` gates *creation* only; reads are always available):
  - `skill_list`, `skill_run` — always registered, so existing libraries are usable.
  - `skill_save` — registered only when `skillsEnabled=true` (default false).
- Browser tools (registered when `allowedBrowser=true` in config; uses Playwright+Chromium):
  - `browser_navigate`, `browser_back`, `browser_forward`, `browser_reload`
  - `browser_click`, `browser_fill`, `browser_press`
  - `browser_get_text`, `browser_screenshot`, `browser_eval`, `browser_close`

Pi's built-in `read`, `bash`, `edit`, `write`, `grep`, `find`, and `ls` tools remain responsible for coding and filesystem work.

## Reliability Stack

The tools above form a click-fidelity ladder; the system prompt teaches Pi to walk it in order:

1. **`browser_click`** for any element on a web page (DOM/ARIA selectors).
2. **`desktop_click_element`** for any native UI control with a visible name/label (UIAutomation on Windows, AT-SPI on Linux, AppleScript-AX on macOS).
3. **`desktop_click_text`** finds visible text via OCR (Tesseract) and clicks the centre of its bounding box.
4. **`desktop_screenshot_marked` + `desktop_click_mark`** uses Set-of-Mark grounding when the target lacks a clean name.
5. **`desktop_click(x, y)`** is the last resort — only when none of the above can identify the target.

Each step gracefully degrades: if Tesseract isn't installed `desktop_click_text` returns a "please install" message instead of failing silently. If the AT-SPI helper isn't available on Linux, `desktop_click_element` returns the install instructions. If `magick`/`convert` isn't on PATH, `desktop_screenshot_marked` still emits the marks list — the model can still call `desktop_click_mark(id)` even without the visual annotation overlay.

## Other Features

- **Planner**: when `plannerEnabled=true` (default) the `/autogui` system prompt instructs the model to produce a numbered plan as its first message before executing. No separate LLM call — Pi owns the model loop.
- **Skill library**: `skillsEnabled` controls *creation* only. When false (the default) the extension does NOT register `skill_save` and never writes a skills file to disk — but `skill_list`, `skill_run`, and the candidate-skills block that `/autogui` prepends to the prompt are always available, so any existing skills at `skillsPath` remain readable and replayable. Set `skillsEnabled: true` in `pi-extension/config.json` to allow creation. When skills are written they go to **`pi-extension/runtime/skills/skills.jsonl`** — its own private library, deliberately separate from the standalone Python agent's `./skills/skills.jsonl` so the two don't shadow each other. Override the path with `skillsPath` (absolute) — leave it empty to use the runtime default; point it at the same path as the mainline if you want a shared library. The directory is created lazily on first save and is git-ignored.
- **Trajectory log**: every tool call (start, success, failure) is appended to `pi-extension/runtime/traces/<session>.jsonl` for post-hoc inspection.
- **Failure recording**: a daemon thread maintains a 5-second rolling screen buffer; on any tool failure it dumps the frames as an animated GIF (via ImageMagick) into `runtime/failures/` so you can see *how* the agent got into trouble.
- **Action scoping & dry-run**: `allowedApps`, `blockedWindowTitles`, and `dryRun` config flags gate every state-changing tool. Default is unrestricted; turn them on for sensitive contexts.
- **Pre/post window-set diff**: every state-changing desktop tool emits an `unchanged: true` flag if the window list didn't change, plus an `unexpectedModal` field when a new window matches `/error|warning|sign in|password|allow|permission|are you sure|confirm|update available/i`.
- **Native Windows input**: on Windows/WSL, `desktop_click` uses `user32.SendInput` directly via PowerShell PInvoke — real INPUT events, correct DPI, falls back to legacy `mouse_event` only on PowerShell errors.
- **Perception cache**: `desktop_screenshot` and `desktop_list_windows` results are cached for `perceptionCacheTtlMs` (500 ms default) and invalidated on any state-changing tool, so the auto-verify cycle is cheap.

## Configuration

The extension reads optional JSON from one of:

1. `pi-extension/config.json` (next to `package.json`)
2. `~/.autogui/pi-extension.json`

Every key has a sensible default — leave the file out and everything works. See `config.json.example` for the full schema. Highlights:

- `installDependencies`: when true, the extension runs the same `scripts/install-dependencies.*` shell script as the mainline, once at session start. Default false.
- `allowedBrowser`: set true to register the `browser_*` tools (requires Playwright + Chromium).
- `visionEnabled`: when true (default), `desktop_screenshot` includes the inline PNG image in the tool result so the model can see the screen. Set false if the provider struggles with image payloads.
- `dryRun` / `allowedApps` / `blockedWindowTitles`: safety gates.
- `plannerEnabled`: planner-first protocol in the system prompt.
- `controllerEnabled` (default `true`): typed-plan + step-by-step protocol; injects the `plan_set` / `plan_update_step` / `checkpoint` workflow into the prompt and wires the plan slot to the new meta-tools. Set false for the legacy free-text planner.
- `skillsEnabled` (default `false`): creation gate. False blocks `skill_save`; `skill_list`, `skill_run`, and the candidate-skills suggestion are always available so any existing library at `skillsPath` stays usable. Override the path with `skillsPath` (absolute).
- `artifactsEnabled` (default `true`) / `progressEnabled` (default `true`): explicit on/off for the artifact and progress stores. Set either to false to disable that store entirely. `artifactsDir` / `progressDir` override the runtime-default path (`runtime/artifacts/` / `runtime/progress/`); leave them empty to use the default. Empty string alone no longer disables a store — set the corresponding `*Enabled` flag.
- `memoryEnabled` (default `false`): creation gate for the per-app quirk database (mirrors `skillsEnabled`). False blocks `memory_note` and the controller's auto-recording; `memory_get` and the planner's app-memory hints continue to read whatever is already at `memoryDir`. Set true to allow new records.
- `memoryDir`: location for the per-app quirk database. Empty string resolves to `runtime/memory/` (so `memory_get` and the planner's app-memory hints can still serve reads); `memoryEnabled` is the actual on/off gate for *writes*. The pi-extension uses its own memory dir, separate from the standalone agent's `./memory/` so they don't shadow each other.
- `budget.maxToolCalls` / `budget.maxSeconds`: hard ceilings consulted by the `budget_status` tool. 0 = no ceiling.
- `screenRecord.*`: rolling screen buffer for failure post-mortem.

### Verification & robustness tools (always available when the supporting store is constructed)

| Tool | What it does |
|------|--------------|
| `desktop_wait_for(window_title \| element_name \| text \| window_id, timeout)` | Block until the target appears; never click on a not-yet-drawn window. |
| `check_predicate(kind, value/path/command/...)` | Verify a typed post-condition deterministically (window/file/URL/text/process/shell). Use after a step's expected outcome should hold. |
| `preflight(checks?)` | Verify required apps / files / URLs / tools / commands are available. With no `checks` arg, derives the list from the active plan's `tools_hint` + predicate paths + explicit `preflight` block. |
| `classify_failure(tool_name, error_message)` | Maps an error to one of `{transient_io, app_not_ready, missing_element, permission, predicate_not_met, user_input_needed, unknown}` and recommends `retry` / `wait_and_retry` / `replan` / `escalate`. |
| `memory_get(app)` | Always available. Read the per-app quirk database under `runtime/memory/`. Empty `app` lists every recorded app. |
| `memory_note(app, text, tag?)` | Registered only when `memoryEnabled=true` (default false). Persists a free-form note into the quirk database so future tasks against the same app see the warning. |
| `budget_status()` | Return tool-call / wall-time counters and the fraction of any configured ceiling consumed. |
| `plan_set` / `plan_get` / `plan_update_step` | Manage the typed plan from inside the loop; wire `plan_update_step(id, status="done")` after each verified step. |
| `checkpoint(label, data?)` | Persist a free-form progress marker so the task can resume after a crash or abort. |
| `get_artifact` / `list_artifacts` | Fetch / enumerate large bodies (file content, page text, command stdout > 4KB) the wrap helper auto-stored. |

Optional system dependencies for graceful-degrade features (all
installed by `scripts/install-dependencies.*`):

| Feature                          | Dep                                                              |
|----------------------------------|------------------------------------------------------------------|
| `desktop_screenshot_marked` overlay | ImageMagick (`magick` or `convert`)                            |
| `desktop_click_text`/`desktop_find_text` | Tesseract                                              |
| `desktop_click_element` on Linux | `python3` + `python3-pyatspi` + `gir1.2-atspi-2.0`              |
| `browser_*` tools                | Playwright + Chromium                                            |
| Failure GIF                      | ImageMagick (manifest written if missing)                        |

## Install Dependencies

From this directory:

```bash
npm install
npm run typecheck
```

The extension targets the installed Pi package name `@earendil-works/pi-coding-agent`.

### Optional system dependencies (single install script)

All the optional system tools (Tesseract, ImageMagick, Playwright + Chromium, AT-SPI bindings on Linux, Python deps for the mainline, Node deps for this extension) are installed by **one script per OS** that lives at the project root under `scripts/`:

```bash
# Linux / macOS / WSL
bash scripts/install-dependencies.sh

# Windows (cmd shim)
scripts\install-dependencies.cmd

# Windows (PowerShell)
powershell -ExecutionPolicy Bypass -File scripts\install-dependencies.ps1
```

The script is idempotent — every dep is checked first and skipped if it's already installed — and loud, echoing every command before running it.

You can also have AutoGUI run the script automatically at session start by setting the config flag (default false):

```json
{ "installDependencies": true }
```

After running, set `allowedBrowser: true` to register the `browser_*` tools (Playwright + Chromium are now installed inside `pi-extension/node_modules/`).

**What each tool needs if you'd rather install one piece by hand:**

```bash
# Tesseract (for desktop_click_text / desktop_find_text)
sudo apt install tesseract-ocr   # Linux / WSL
brew install tesseract           # macOS
winget install UB-Mannheim.TesseractOCR   # Windows

# ImageMagick (for SoM overlay + failure GIFs)
sudo apt install imagemagick     # Linux
brew install imagemagick         # macOS
winget install ImageMagick.ImageMagick   # Windows

# Linux a11y for desktop_click_element
sudo apt install python3 python3-pyatspi gir1.2-atspi-2.0

# Playwright + Chromium for browser_* tools (playwright is an optional dep;
# npm install picks it up automatically, then install the browser binary)
cd pi-extension
npm install
npx playwright install chromium
```

When something is missing the corresponding tool returns a clear error pointing back at the install script — the agent can then walk down the click ladder to the next-best option (`desktop_click_element` → `desktop_click_text` → `desktop_click_mark` → `desktop_click(x, y)`).

When ImageMagick is missing, marks are still emitted as a list (you can call `desktop_click_mark(id)` even without the visual overlay), and failure recordings are written as a manifest file listing the captured PNG frames instead of a single GIF.

## Load The Extension

For local development:

```bash
pi -e ./src/index.ts
```

From the repository root:

```bash
pi -e ./pi-extension/src/index.ts
```

Pi can also load extension directories from settings or its normal extension folders. During development, `-e` is the most explicit path and avoids copying files.

## Use `/autogui`

Inside Pi:

```text
/autogui Open Notepad and type hello world
```

The command injects a prompt that tells Pi to:

- inspect the desktop first with `desktop_list_windows` or `desktop_screenshot`;
- derive click coordinates from screenshots or window bounds;
- focus a target before typing with `desktop_focus_window` when possible;
- verify focus with `desktop_active_window` before typing when focus is uncertain;
- avoid using app/window menus such as Alt+Space as a focus strategy;
- verify visible changes after launching apps or interacting with the UI;
- use Pi's built-in coding tools for code and files.

`/autogui` uses whichever model Pi is currently configured to use.

## Abort AutoGUI

To stop an in-flight AutoGUI task:

```text
/autogui-abort
```

The command calls Pi's current operation abort hook, cancels any pending AutoGUI retry timer, and clears AutoGUI's active task state. It does not change the selected model or provider.

## Provider Errors And Retry

Some OpenRouter routes, especially free routes, may return provider errors such as `404 No endpoints found that support tool use`, `404 No endpoints available matching your guardrail restrictions and data policy`, or `429` rate limits while the desktop tools are active. The extension does not change the selected model. For AutoGUI tasks, it treats `404` and `429` responses as retryable whether they appear in the provider response hook or in the final assistant error text, then tags the error so Pi core can apply its normal exponential retry/backoff behavior.

AutoGUI also has an outer retry loop for these provider statuses. If Pi's own retry limit is exhausted, AutoGUI schedules another attempt with capped exponential backoff and keeps doing that until `/autogui-abort` is run or the task completes. This is intended for temporary route/rate-limit failures. If a model/route permanently lacks tool-use support, it will keep retrying until aborted.

After repeated `404`/`429` provider failures, AutoGUI enters screenshot degrade mode. Screenshots are still captured and saved to `runtime/screenshots`, but future `desktop_screenshot` results omit the inline image payload and the context hook strips earlier screenshot image payloads before provider calls. This lets the agent continue with window bounds, active-window detection, explicit window focusing, screenshot paths, and non-visual tools when the selected provider route struggles with image payloads.

## Auto-Spawned tmux Validator

When an `/autogui` task ends naturally (assistant `stopReason="stop"`) and `validateAfterAutogui` is `true` in `config.json` (the default), the extension auto-spawns a read-only Pi validator in a fresh detached tmux session.  The validator gets only:

- `desktop_screenshot`
- `desktop_list_windows`
- `desktop_active_window`

…and the same task description as the original run, plus a "validator mode" rider that tells the model not to click/type/launch.  The spawned session is named `autogui-validator-<timestamp>`; attach with:

```bash
tmux attach -t autogui-validator-<timestamp>
```

Use this when you want a second model pass to inspect the desktop state without giving that pass click/type/launch tools.  Aborted tasks (`/autogui-abort`) skip the validator.  Set `validateAfterAutogui: false` in `config.json` to disable the auto-spawn entirely — the cost is a separate Pi process per completed task, which means another model request, so users on tight token budgets will want to flip it off.

## Runtime Files

All runtime output lives under `pi-extension/runtime/` (git-ignored):

| Path | Contents |
|------|----------|
| `runtime/skills/skills.jsonl` | **Skill library** — created the first time `skill_save` runs (which requires `skillsEnabled=true`); reads via `skill_list`/`skill_run` work regardless of the flag |
| `runtime/traces/` | Per-session JSONL trajectory logs |
| `runtime/artifacts/` | Artifact bodies + `index.jsonl` — stable-id store (each capture gets a fresh `artifact://<id>`; identical bodies are not deduped). |
| `runtime/progress/` | Per-task JSON progress records (auto-resume keyed by task hash) |
| `runtime/memory/` | Per-app quirk store — `<app>.json` + `index.jsonl`. Only created when `memoryEnabled=true` and a write fires; reads via `memory_get` work regardless. |
| `runtime/screenshots/` | Ad-hoc screenshots taken by the agent |
| `runtime/failures/` | Animated GIF failure recordings |
| `runtime/browser/` | Playwright browser screenshots |
| `runtime/logs/autogui.log` | Verbose JSON-lines event log |

The pi extension's skills library lives at `pi-extension/runtime/skills/skills.jsonl` and is **distinct** from the standalone Python agent's `./skills/skills.jsonl` — each side keeps its own library so they don't shadow each other.  Point `skillsPath` at a shared absolute path if you want to merge them.

The extension creates these directories recursively as needed.

The log records command starts, tool starts, successes, failures, backend detection, PowerShell script attempts, PowerShell stdout/stderr, and provider response statuses. Large string fields are truncated in the log so screenshots are not written into logs wholesale.

Provider request retry/backoff is owned primarily by Pi core. The extension logs `429` and `404` provider responses when Pi emits them, tags those AutoGUI provider errors so Pi core can retry them, and runs an AutoGUI-level outer retry loop if provider failures continue. It does not change models or bypass provider routing.

## Platform Backends

| Platform | Backend |
|----------|---------|
| Windows | `powershell.exe`/PowerShell, Windows Forms, user32 |
| WSL | `powershell.exe` interop against the Windows desktop |
| macOS | `screencapture`, `osascript`, `open` |
| Linux X11 | `xdotool`, `wmctrl`, ImageMagick `import` or `gnome-screenshot` |
| Linux Wayland | `grim`, `swaymsg`; mutating mouse/keyboard actions are dependency-gated in v1 |

On WSL, the extension intentionally controls the Windows desktop through Windows PowerShell rather than Linux screenshot utilities. On native Linux and macOS, it uses the native backends above and does not route desktop actions through PowerShell.

Missing dependencies or permissions are reported as tool errors for Pi to surface.

## Development Checks

```bash
npm run typecheck
pi --offline --no-session -e ./src/index.ts --list-models
```

The second command is a startup smoke test: it verifies Pi can load the extension without starting an interactive desktop task.
