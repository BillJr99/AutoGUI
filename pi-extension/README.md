# AutoGUI Pi Extension

Native TypeScript extension for [Pi Coding Agent](https://pi.dev/) that adds desktop automation tools while leaving Pi in charge of the agent workflow.

This extension is decoupled from OpenWebUI. It does not create a model client, model picker, custom ReAct loop, or TUI. Pi supplies the model/provider, session history, tool orchestration, and UI.

## What It Adds

- `/autogui <task>`: sends a prepared desktop-automation prompt into Pi's normal agent loop.
- `/autogui-abort`: aborts the current AutoGUI/Pi agent operation.
- `/autogui-validate <task>`: spawns a separate read-only Pi validator in tmux.
- `/desktop-status`: reports the detected backend and platform dependencies.
- Desktop tools:
  - `desktop_screenshot`
  - `desktop_click`
  - `desktop_type`
  - `desktop_hotkey`
  - `desktop_scroll`
  - `desktop_list_windows`
  - `desktop_active_window`
  - `desktop_focus_window`
  - `desktop_launch`
  - `desktop_get_cursor_pos`
  - `desktop_mouse_move`

Pi's built-in `read`, `bash`, `edit`, `write`, `grep`, `find`, and `ls` tools remain responsible for coding and filesystem work.

## Install Dependencies

From this directory:

```bash
npm install
npm run typecheck
```

The extension targets the installed Pi package name `@earendil-works/pi-coding-agent`.

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

## Optional tmux Validator

Use `/autogui-validate <task>` to launch a separate Pi process in a detached tmux session:

```text
/autogui-validate Confirm the browser is open on the expected page
```

The validator is intentionally read-only. It starts Pi with only:

- `desktop_screenshot`
- `desktop_list_windows`
- `desktop_active_window`

The spawned tmux session is named `autogui-validator-<timestamp>`. Attach to it with:

```bash
tmux attach -t autogui-validator-<timestamp>
```

This is useful when you want a second model pass to inspect the desktop state without giving that pass click/type/launch tools. It is opt-in because spawning another Pi can consume another model request and should be visible to the user.

## Runtime Files

Screenshots are saved under:

```text
pi-extension/runtime/screenshots/
```

Verbose logs are written as JSON lines under:

```text
pi-extension/runtime/logs/autogui.log
```

The extension creates these directories recursively as needed. Runtime files, local Pi metadata, and `node_modules` are ignored by git.

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
