import { Type } from "@earendil-works/pi-ai";
import { defineTool, type ToolDefinition } from "@earendil-works/pi-coding-agent";
import type { BackendLogger, DesktopBackend, Rect, ScreenshotResult } from "./types.js";

type BackendProvider = () => Promise<DesktopBackend>;
interface DesktopToolOptions {
  omitScreenshotImages?: () => boolean;
}

const Region = Type.Object({
  x: Type.Number({ description: "Absolute screen x coordinate" }),
  y: Type.Number({ description: "Absolute screen y coordinate" }),
  width: Type.Number({ description: "Region width in pixels" }),
  height: Type.Number({ description: "Region height in pixels" }),
});

function textResult(text: string, details: Record<string, unknown>) {
  return { content: [{ type: "text" as const, text }], details };
}

function screenshotResult(result: ScreenshotResult, omitImage = false) {
  const text = omitImage
    ? `Screenshot captured: ${result.width}x${result.height}, saved to ${result.path}. Inline image omitted because AutoGUI is in screenshot degrade mode.`
    : `Screenshot captured: ${result.width}x${result.height}, saved to ${result.path}`;
  return {
    content: omitImage
      ? [{ type: "text" as const, text }]
      : [
          { type: "text" as const, text },
          { type: "image" as const, data: result.data, mimeType: result.mimeType },
        ],
    details: {
      path: result.path,
      width: result.width,
      height: result.height,
      mimeType: result.mimeType,
      monitors: result.monitors,
      imageOmitted: omitImage,
    },
  };
}

export function createDesktopTools(getBackend: BackendProvider, saveDir: string, logger?: BackendLogger, options: DesktopToolOptions = {}): ToolDefinition[] {
  const wrap = <T extends Record<string, unknown>>(
    toolName: string,
    fn: (params: T, signal?: AbortSignal) => Promise<ReturnType<typeof textResult> | ReturnType<typeof screenshotResult>>,
  ) => async (_id: string, params: T, signal?: AbortSignal) => {
    await logger?.log("tool.start", { toolName, params });
    try {
      const result = await fn(params, signal);
      await logger?.log("tool.success", { toolName, details: result.details });
      return result;
    } catch (error) {
      await logger?.log("tool.failure", {
        toolName,
        params,
        error: error instanceof Error ? error.message : String(error),
        stack: error instanceof Error ? error.stack : undefined,
        details: typeof error === "object" && error !== null && "details" in error ? (error as { details?: unknown }).details : undefined,
      });
      throw error;
    }
  };

  return [
    defineTool({
      name: "desktop_screenshot",
      label: "Screenshot",
      description: "Capture the current desktop as a PNG image. Use this before clicking and after actions to verify the screen state.",
      promptSnippet: "desktop_screenshot: capture the current desktop and return an image.",
      promptGuidelines: [
        "Use desktop_screenshot before any coordinate-based desktop action unless you already have current window bounds.",
        "Use desktop_screenshot after desktop actions when visual verification matters.",
      ],
      parameters: Type.Object({
        region: Type.Optional(Region),
      }),
      executionMode: "sequential",
      execute: wrap("desktop_screenshot", async (params, signal) => {
        const backend = await getBackend();
        const region = params.region ? normalizeRect(params.region) : undefined;
        return screenshotResult(await backend.screenshot({ region, saveDir }, signal), Boolean(options.omitScreenshotImages?.()));
      }),
    }),

    defineTool({
      name: "desktop_click",
      label: "Click",
      description: "Click the mouse at absolute screen coordinates.",
      promptSnippet: "desktop_click: click absolute screen coordinates.",
      promptGuidelines: [
        "For desktop_click, derive coordinates from desktop_screenshot or desktop_list_windows; do not guess.",
      ],
      parameters: Type.Object({
        x: Type.Number({ description: "Absolute screen x coordinate" }),
        y: Type.Number({ description: "Absolute screen y coordinate" }),
        button: Type.Optional(Type.Union([Type.Literal("left"), Type.Literal("right"), Type.Literal("middle")])),
        clicks: Type.Optional(Type.Number({ description: "Number of clicks" })),
      }),
      executionMode: "sequential",
      execute: wrap("desktop_click", async (params, signal) => {
        const backend = await getBackend();
        const result = await backend.click(Math.max(1, Math.round(params.x)), Math.max(1, Math.round(params.y)), params.button ?? "left", Math.max(1, Math.round(params.clicks ?? 1)), signal);
        return textResult("Clicked desktop.", result);
      }),
    }),

    defineTool({
      name: "desktop_type",
      label: "Type",
      description: "Type text into the currently focused window.",
      promptSnippet: "desktop_type: type text into the focused desktop window.",
      promptGuidelines: [
        "Before desktop_type, use desktop_click or desktop_hotkey to focus the target control.",
      ],
      parameters: Type.Object({
        text: Type.String({ description: "Text to type" }),
      }),
      executionMode: "sequential",
      execute: wrap("desktop_type", async (params, signal) => {
        const backend = await getBackend();
        const result = await backend.typeText(params.text, signal);
        return textResult(`Typed ${params.text.length} characters.`, result);
      }),
    }),

    defineTool({
      name: "desktop_hotkey",
      label: "Hotkey",
      description: "Press a keyboard shortcut such as ['ctrl','l'] or ['alt','f4'].",
      promptSnippet: "desktop_hotkey: press a desktop keyboard shortcut.",
      parameters: Type.Object({
        keys: Type.Array(Type.String(), { description: "Keys in order, e.g. ['ctrl','l']" }),
      }),
      executionMode: "sequential",
      execute: wrap("desktop_hotkey", async (params, signal) => {
        const backend = await getBackend();
        const result = await backend.hotkey(params.keys, signal);
        return textResult(`Pressed hotkey ${params.keys.join("+")}.`, result);
      }),
    }),

    defineTool({
      name: "desktop_scroll",
      label: "Scroll",
      description: "Scroll the focused window. Each 'clicks' value scrolls one page (Page Down/Up) on Windows/WSL/macOS, or one notch on Linux X11. Call desktop_focus_window first. x and y are optional: when both are > 0 they focus the window at that position before scrolling; pass 0 (or omit) to scroll the currently active window.",
      promptSnippet: "desktop_scroll: scroll the focused window by page.",
      promptGuidelines: [
        "Call desktop_focus_window before desktop_scroll to ensure the right window receives the scroll.",
        "Pass x=0, y=0 (or omit) to scroll the active window without needing coordinates.",
        "Each 'clicks' = one Page Down or Page Up on most platforms.",
      ],
      parameters: Type.Object({
        x: Type.Optional(Type.Number({ description: "Screen x of scroll target; 0 or omit to scroll active window" })),
        y: Type.Optional(Type.Number({ description: "Screen y of scroll target; 0 or omit to scroll active window" })),
        clicks: Type.Optional(Type.Number({ description: "Number of scroll steps (default 3)" })),
        direction: Type.Optional(Type.Union([Type.Literal("up"), Type.Literal("down")])),
      }),
      executionMode: "sequential",
      execute: wrap("desktop_scroll", async (params, signal) => {
        const backend = await getBackend();
        const x = Math.round(params.x ?? 0);
        const y = Math.round(params.y ?? 0);
        const result = await backend.scroll(x, y, Math.max(1, Math.round(params.clicks ?? 3)), params.direction ?? "down", signal);
        return textResult("Scrolled desktop.", result);
      }),
    }),

    defineTool({
      name: "desktop_get_window_text",
      label: "Get Window Text",
      description: "Extract visible text from the focused window by selecting all (Ctrl+A / Cmd+A) and copying to clipboard. Returns up to 50,000 characters. Useful for reading web page content, search results, or document text without needing pixel coordinates. The clipboard is restored after reading.",
      promptSnippet: "desktop_get_window_text: read all visible text from the focused window.",
      promptGuidelines: [
        "Use desktop_focus_window and click in the page body before calling desktop_get_window_text in a browser.",
        "Use the returned text to find links, read search results, or determine the next action.",
        "The clipboard is restored to its previous content automatically.",
      ],
      parameters: Type.Object({
        max_chars: Type.Optional(Type.Number({ description: "Maximum characters to return (default 50000)" })),
      }),
      executionMode: "sequential",
      execute: wrap("desktop_get_window_text", async (params, signal) => {
        const backend = await getBackend();
        const result = await backend.getWindowText(params.max_chars ?? 50000, signal);
        const preview = result.text.slice(0, 200).replace(/\s+/g, " ").trim();
        return textResult(
          `Got ${result.length} characters of window text${result.truncated ? " (truncated)" : ""}. Preview: ${preview}`,
          result,
        );
      }),
    }),

    defineTool({
      name: "desktop_list_windows",
      label: "List Windows",
      description: "List visible desktop windows with titles and bounds.",
      promptSnippet: "desktop_list_windows: list visible windows and screen bounds.",
      promptGuidelines: [
        "Use desktop_list_windows to compute click coordinates from window bounds whenever possible.",
      ],
      parameters: Type.Object({}),
      executionMode: "sequential",
      execute: wrap("desktop_list_windows", async (_params, signal) => {
        const backend = await getBackend();
        const result = await backend.listWindows(signal);
        return textResult(`Found ${result.count} visible windows.`, result);
      }),
    }),

    defineTool({
      name: "desktop_active_window",
      label: "Active Window",
      description: "Return the currently focused desktop window with title, app, pid, and bounds when available.",
      promptSnippet: "desktop_active_window: identify the currently focused desktop window.",
      promptGuidelines: [
        "Use desktop_active_window before typing whenever focus is uncertain.",
        "After desktop_focus_window, use desktop_active_window to verify the expected app/window is focused.",
      ],
      parameters: Type.Object({}),
      executionMode: "sequential",
      execute: wrap("desktop_active_window", async (_params, signal) => {
        const backend = await getBackend();
        const result = await backend.activeWindow(signal);
        return textResult(result.found && result.window ? `Active window: ${result.window.title || result.window.app || result.window.id || "unknown"}.` : "No active window found.", result);
      }),
    }),

    defineTool({
      name: "desktop_focus_window",
      label: "Focus Window",
      description: "Focus a desktop window by id, pid, title substring, or app/process name.",
      promptSnippet: "desktop_focus_window: focus a window by id, pid, title, or app before typing.",
      promptGuidelines: [
        "Prefer desktop_focus_window over keyboard shortcuts when choosing which window receives text.",
        "Use id or pid from desktop_list_windows when available; otherwise use app or title.",
        "Do not use alt+space, app menus, or window menus as a focus strategy.",
      ],
      parameters: Type.Object({
        id: Type.Optional(Type.String({ description: "Window id or native handle from desktop_list_windows" })),
        pid: Type.Optional(Type.Number({ description: "Process id from desktop_list_windows" })),
        title: Type.Optional(Type.String({ description: "Title substring to match" })),
        app: Type.Optional(Type.String({ description: "Application/process name substring to match" })),
      }),
      executionMode: "sequential",
      execute: wrap("desktop_focus_window", async (params, signal) => {
        const backend = await getBackend();
        const target = {
          id: typeof params.id === "string" ? params.id : undefined,
          pid: typeof params.pid === "number" ? Math.round(params.pid) : undefined,
          title: typeof params.title === "string" ? params.title : undefined,
          app: typeof params.app === "string" ? params.app : undefined,
        };
        const result = await backend.focusWindow(target, signal);
        return textResult("Focused window.", result);
      }),
    }),

    defineTool({
      name: "desktop_launch",
      label: "Launch",
      description: "Launch an application by executable name or app name.",
      promptSnippet: "desktop_launch: launch a desktop application.",
      promptGuidelines: [
        "After desktop_launch, use desktop_list_windows or desktop_screenshot to confirm the app opened before interacting with it.",
      ],
      parameters: Type.Object({
        application: Type.String(),
        args: Type.Optional(Type.Array(Type.String())),
      }),
      executionMode: "sequential",
      execute: wrap("desktop_launch", async (params, signal) => {
        const backend = await getBackend();
        const result = await backend.launch(params.application, params.args ?? [], signal);
        return textResult(`Launched ${params.application}.`, result);
      }),
    }),

    defineTool({
      name: "desktop_get_cursor_pos",
      label: "Cursor Position",
      description: "Return the current mouse cursor position in screen pixels.",
      promptSnippet: "desktop_get_cursor_pos: get the current mouse position.",
      parameters: Type.Object({}),
      executionMode: "sequential",
      execute: wrap("desktop_get_cursor_pos", async (_params, signal) => {
        const backend = await getBackend();
        const result = await backend.getCursorPos(signal);
        return textResult(`Cursor is at ${result.x}, ${result.y}.`, result);
      }),
    }),

    defineTool({
      name: "desktop_mouse_move",
      label: "Mouse Move",
      description: "Move the mouse by a relative offset and optionally click.",
      promptSnippet: "desktop_mouse_move: move the mouse by a relative offset.",
      parameters: Type.Object({
        dx: Type.Number({ description: "Horizontal delta in pixels" }),
        dy: Type.Number({ description: "Vertical delta in pixels" }),
        click: Type.Optional(Type.Boolean({ description: "Left-click after moving" })),
      }),
      executionMode: "sequential",
      execute: wrap("desktop_mouse_move", async (params, signal) => {
        const backend = await getBackend();
        const result = await backend.mouseMove(Math.round(params.dx), Math.round(params.dy), Boolean(params.click), signal);
        return textResult("Moved cursor.", result);
      }),
    }),
  ];
}

function normalizeRect(rect: Rect): Rect {
  return {
    x: Math.round(rect.x),
    y: Math.round(rect.y),
    width: Math.max(1, Math.round(rect.width)),
    height: Math.max(1, Math.round(rect.height)),
  };
}
