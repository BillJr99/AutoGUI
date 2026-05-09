import { readFile } from "node:fs/promises";
import { execFile } from "../process.js";
import { DesktopError, type DesktopBackend, type PlatformInfo, type ScreenshotOptions, type ScreenshotResult, type WindowInfo } from "../types.js";
import { createPngPath, makeScreenshotResult, parseJsonArray, quoteAppleScript } from "./common.js";

export class MacBackend implements DesktopBackend {
  readonly name = "macos";

  constructor(readonly platform: PlatformInfo) {}

  async status(signal?: AbortSignal): Promise<Record<string, unknown>> {
    const commands = await Promise.all(["screencapture", "osascript", "open"].map(async (cmd) => {
      try {
        const result = await execFile("which", [cmd], { timeoutMs: 3000, signal });
        return [cmd, result.code === 0] as const;
      } catch {
        return [cmd, false] as const;
      }
    }));
    return { backend: this.name, platform: this.platform.summary, commands: Object.fromEntries(commands) };
  }

  async screenshot(options: ScreenshotOptions, signal?: AbortSignal): Promise<ScreenshotResult> {
    const path = await createPngPath(options.saveDir);
    const args = ["-x"];
    if (options.region) {
      const r = options.region;
      args.push("-R", `${r.x},${r.y},${r.width},${r.height}`);
    }
    args.push(path);
    const result = await execFile("screencapture", args, { timeoutMs: 15000, signal });
    if (result.code !== 0) throw new DesktopError("screencapture failed", { stderr: result.stderr, code: result.code });
    const data = (await readFile(path)).toString("base64");
    const size = await pngSize(data);
    return makeScreenshotResult(path, size.width, size.height, data);
  }

  async click(x: number, y: number, button: "left" | "right" | "middle", clicks: number, signal?: AbortSignal): Promise<Record<string, unknown>> {
    const script = `tell application "System Events" to click at {${x}, ${y}}`;
    for (let i = 0; i < clicks; i++) {
      const result = await execFile("osascript", ["-e", script], { timeoutMs: 5000, signal });
      if (result.code !== 0) throw permissionError("click", result.stderr);
    }
    return { success: true, x, y, button, clicks };
  }

  async typeText(text: string, signal?: AbortSignal): Promise<Record<string, unknown>> {
    const script = `tell application "System Events" to keystroke "${quoteAppleScript(text)}"`;
    const result = await execFile("osascript", ["-e", script], { timeoutMs: 10000, signal });
    if (result.code !== 0) throw permissionError("type text", result.stderr);
    return { success: true, length: text.length };
  }

  async hotkey(keys: string[], signal?: AbortSignal): Promise<Record<string, unknown>> {
    const key = keys[keys.length - 1] ?? "";
    const modifiers = keys.slice(0, -1).map((k) => `${macModifier(k)} down`).filter(Boolean);
    const script = modifiers.length
      ? `tell application "System Events" to keystroke "${quoteAppleScript(key)}" using {${modifiers.join(", ")}}`
      : `tell application "System Events" to keystroke "${quoteAppleScript(key)}"`;
    const result = await execFile("osascript", ["-e", script], { timeoutMs: 5000, signal });
    if (result.code !== 0) throw permissionError("press hotkey", result.stderr);
    return { success: true, keys };
  }

  async scroll(_x: number, _y: number, clicks: number, direction: "up" | "down", signal?: AbortSignal): Promise<Record<string, unknown>> {
    const amount = direction === "down" ? -clicks : clicks;
    const result = await execFile("osascript", ["-e", `tell application "System Events" to scroll wheel ${amount}`], { timeoutMs: 5000, signal });
    if (result.code !== 0) throw permissionError("scroll", result.stderr);
    return { success: true, clicks, direction };
  }

  async listWindows(signal?: AbortSignal): Promise<{ windows: WindowInfo[]; count: number }> {
    const script = `
set out to "["
tell application "System Events"
  set firstItem to true
  repeat with p in (processes whose visible is true)
    repeat with w in windows of p
      try
        set pos to position of w
        set sz to size of w
        set titleText to name of w
        if firstItem is false then set out to out & ","
        set firstItem to false
        set out to out & "{\\"title\\":\\"" & titleText & "\\",\\"app\\":\\"" & name of p & "\\",\\"x\\":" & item 1 of pos & ",\\"y\\":" & item 2 of pos & ",\\"width\\":" & item 1 of sz & ",\\"height\\":" & item 2 of sz & "}"
      end try
    end repeat
  end repeat
end tell
return out & "]"
`;
    const result = await execFile("osascript", ["-e", script], { timeoutMs: 10000, signal });
    if (result.code !== 0) throw permissionError("list windows", result.stderr);
    const windows = parseJsonArray<WindowInfo>(result.stdout);
    return { windows, count: windows.length };
  }

  async activeWindow(signal?: AbortSignal): Promise<{ window?: WindowInfo; found: boolean }> {
    const script = `
tell application "System Events"
  set p to first process whose frontmost is true
  set appName to name of p
  set pidValue to unix id of p
  try
    set w to front window of p
    set pos to position of w
    set sz to size of w
    return "{\\"found\\":true,\\"window\\":{\\"title\\":\\"" & name of w & "\\",\\"app\\":\\"" & appName & "\\",\\"pid\\":" & pidValue & ",\\"x\\":" & item 1 of pos & ",\\"y\\":" & item 2 of pos & ",\\"width\\":" & item 1 of sz & ",\\"height\\":" & item 2 of sz & ",\\"active\\":true}}"
  on error
    return "{\\"found\\":true,\\"window\\":{\\"title\\":\\"\\",\\"app\\":\\"" & appName & "\\",\\"pid\\":" & pidValue & ",\\"x\\":0,\\"y\\":0,\\"width\\":0,\\"height\\":0,\\"active\\":true}}"
  end try
end tell
`;
    const result = await execFile("osascript", ["-e", script], { timeoutMs: 10000, signal });
    if (result.code !== 0) throw permissionError("get active window", result.stderr);
    return JSON.parse(result.stdout) as { window?: WindowInfo; found: boolean };
  }

  async focusWindow(target: { id?: string; title?: string; pid?: number; app?: string }, signal?: AbortSignal): Promise<Record<string, unknown>> {
    const appNeedle = quoteAppleScript(target.app ?? "");
    const titleNeedle = quoteAppleScript(target.title ?? "");
    const pidNeedle = target.pid ? String(Math.round(target.pid)) : "";
    const script = `
tell application "System Events"
  set appNeedle to "${appNeedle}"
  set titleNeedle to "${titleNeedle}"
  set pidNeedle to "${pidNeedle}"
  if appNeedle is not "" then
    set p to first process whose name contains appNeedle
  else if pidNeedle is not "" then
    set p to first process whose (unix id as text) is pidNeedle
  else if titleNeedle is not "" then
    set p to first process whose visible is true and (name of windows contains titleNeedle)
  else
    error "desktop_focus_window requires app, pid, or title."
  end if
  set frontmost of p to true
  return "{\\"success\\":true,\\"app\\":\\"" & name of p & "\\"}"
end tell
`;
    const result = await execFile("osascript", ["-e", script], { timeoutMs: 10000, signal });
    if (result.code !== 0) throw permissionError("focus window", result.stderr);
    return JSON.parse(result.stdout) as Record<string, unknown>;
  }

  async launch(application: string, args: string[], signal?: AbortSignal): Promise<Record<string, unknown>> {
    const result = await execFile("open", ["-a", application, ...args], { timeoutMs: 10000, signal });
    if (result.code !== 0) throw new DesktopError("open failed", { stderr: result.stderr, code: result.code });
    return { success: true, application, args };
  }

  async getCursorPos(): Promise<{ x: number; y: number }> {
    throw new DesktopError("Cursor position is not implemented on macOS without an additional native dependency.");
  }

  async mouseMove(dx: number, dy: number, click: boolean, signal?: AbortSignal): Promise<Record<string, unknown>> {
    throw new DesktopError("Relative mouse movement is not implemented on macOS without an additional native dependency.", { dx, dy, click, signal: Boolean(signal) });
  }
}

function macModifier(key: string): string {
  const lower = key.toLowerCase();
  if (lower === "cmd" || lower === "command" || lower === "meta") return "command";
  if (lower === "ctrl" || lower === "control") return "control";
  if (lower === "alt" || lower === "option") return "option";
  if (lower === "shift") return "shift";
  return lower;
}

function permissionError(action: string, stderr: string): DesktopError {
  return new DesktopError(`Could not ${action}. macOS may require Accessibility permission for the terminal running Pi.`, { stderr });
}

async function pngSize(data: string): Promise<{ width: number; height: number }> {
  const buf = Buffer.from(data, "base64");
  if (buf.length >= 24 && buf.toString("ascii", 12, 16) === "IHDR") {
    return { width: buf.readUInt32BE(16), height: buf.readUInt32BE(20) };
  }
  return { width: 0, height: 0 };
}
