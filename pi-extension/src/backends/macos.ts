import { readFile } from "node:fs/promises";
import { execFile, execFileWithStdin } from "../process.js";
import { DesktopError, type BackendCapabilities, type DesktopBackend, type ElementInfo, type Mark, type PlatformInfo, type ScreenshotOptions, type ScreenshotResult, type WindowInfo } from "../types.js";
import { createPngPath, makeScreenshotResult, parseJsonArray, quoteAppleScript } from "./common.js";
import { marksFromWindows } from "../som.js";

export class MacBackend implements DesktopBackend {
  readonly name = "macos";
  readonly capabilities: BackendCapabilities = {
    findElement: true,
    richMarks: false,
    nativeInput: false,
  };

  constructor(readonly platform: PlatformInfo) {}

  async findElement(query: { name?: string; controlType?: string; windowTitle?: string; index?: number }, signal?: AbortSignal): Promise<ElementInfo> {
    const name = quoteAppleScript(query.name ?? "");
    const role = quoteAppleScript(query.controlType ?? "");
    const winTitle = quoteAppleScript(query.windowTitle ?? "");
    const idx = Math.max(0, Math.floor(query.index ?? 0)) + 1; // AppleScript is 1-indexed
    // Walk frontmost process's UI element tree.  System Events exposes
    // descriptions/names/roles plus position+size we can use as a rect.
    const script = `
-- Collect matching elements into |results| (a list, passed by reference so
-- mutations are visible to the caller).  matchIdx is passed in so the
-- recursion can short-circuit as soon as enough matches are found — no need
-- to walk the entire tree when we only want the Nth hit.
on collectMatches(elem, target, role, results, matchIdx)
  try
    set elemRole to (role of elem) as string
  on error
    set elemRole to ""
  end try
  try
    set elemDesc to (description of elem) as string
  on error
    set elemDesc to ""
  end try
  try
    set elemName to (name of elem) as string
  on error
    set elemName to ""
  end try
  try
    set elemTitle to (title of elem) as string
  on error
    set elemTitle to ""
  end try
  set joined to (elemName & " " & elemDesc & " " & elemTitle)
  set haystack to my toLower(joined)
  set roleHay to my toLower(elemRole)
  set nameHit to (target is "" or haystack contains target)
  set roleHit to (role is "" or roleHay contains role)
  if nameHit and roleHit then
    try
      set p to position of elem
      set s to size of elem
      -- JSON-escape name and controlType so embedded quotes/backslashes
      -- don't produce invalid JSON (e.g. a button named OK "Cancel").
      set safeName to my jsonEscape(elemName)
      set safeRole to my jsonEscape(elemRole)
      set end of results to "{\\\"name\\\":\\\"" & safeName & "\\\",\\\"controlType\\\":\\\"" & safeRole & "\\\",\\\"rect\\\":{\\\"x\\\":" & item 1 of p & ",\\\"y\\\":" & item 2 of p & ",\\\"width\\\":" & item 1 of s & ",\\\"height\\\":" & item 2 of s & "}}"
    on error
      -- Element matched but has no geometry; skip it.
    end try
  end if
  -- Short-circuit: stop descending once we have enough matches.
  if (count of results) >= matchIdx then return
  try
    set kids to UI elements of elem
  on error
    return
  end try
  repeat with k in kids
    my collectMatches(k, target, role, results, matchIdx)
    if (count of results) >= matchIdx then exit repeat
  end repeat
end collectMatches

on toLower(s)
  set chars to {"A","B","C","D","E","F","G","H","I","J","K","L","M","N","O","P","Q","R","S","T","U","V","W","X","Y","Z"}
  set lowers to {"a","b","c","d","e","f","g","h","i","j","k","l","m","n","o","p","q","r","s","t","u","v","w","x","y","z"}
  set out to ""
  repeat with ch in s
    set found to false
    repeat with i from 1 to length of chars
      if (ch as string) is item i of chars then
        set out to out & item i of lowers
        set found to true
        exit repeat
      end if
    end repeat
    if not found then set out to out & (ch as string)
  end repeat
  return out
end toLower

-- Escape characters that are forbidden or special in JSON strings:
-- backslash, double-quote, and all control characters (code < 32).
-- AppleScript has no string escape sequences; use ASCII character codes
-- and 'id of' for character code comparisons instead.
on jsonEscape(s)
  set bslash to (ASCII character 92)
  set dquote to (ASCII character 34)
  set nl to (ASCII character 10)
  set cr to (ASCII character 13)
  set tab to (ASCII character 9)
  set hexChars to "0123456789abcdef"
  set out to ""
  repeat with ch in characters of s
    set c to ch as string
    if c is bslash then
      set out to out & bslash & bslash
    else if c is dquote then
      set out to out & bslash & dquote
    else if c is nl then
      set out to out & bslash & "n"
    else if c is cr then
      set out to out & bslash & "r"
    else if c is tab then
      set out to out & bslash & "t"
    else
      set code to id of c
      if code < 32 then
        -- Remaining control characters: emit as unicode escape u00XX
        set hi to (code div 16) + 1
        set lo to (code mod 16) + 1
        set out to out & bslash & "u00" & (character hi of hexChars) & (character lo of hexChars)
      else
        set out to out & c
      end if
    end if
  end repeat
  return out
end jsonEscape

set targetName to "${name}"
set targetRole to "${role}"
set winNeedle to "${winTitle}"
set matchIdx to ${idx}
set results to {}
tell application "System Events"
  set frontProc to first process whose frontmost is true
  if winNeedle is "" then
    set candidates to {window 1 of frontProc}
  else
    set candidates to (every window of frontProc whose name contains winNeedle)
  end if
  repeat with w in candidates
    my collectMatches(w, my toLower(targetName), my toLower(targetRole), results, matchIdx)
    if (count of results) >= matchIdx then exit repeat
  end repeat
end tell
if (count of results) >= matchIdx then
  return item matchIdx of results
end if
return "{\\\"error\\\":\\\"No matching AX element found.\\\"}"
`;
    const r = await execFile("osascript", ["-e", script], { timeoutMs: 15000, signal });
    if (r.code !== 0) {
      throw new DesktopError(
        "macOS AX query failed. The terminal running Pi needs Accessibility permission " +
        "(System Settings → Privacy & Security → Accessibility).",
        { stderr: r.stderr.slice(0, 400) },
      );
    }
    let parsed: Record<string, unknown>;
    try {
      parsed = JSON.parse(r.stdout.trim());
    } catch {
      throw new DesktopError(`AX helper returned non-JSON: ${r.stdout.slice(0, 200)}`);
    }
    if ("error" in parsed) throw new DesktopError(String(parsed.error));
    return parsed as unknown as ElementInfo;
  }

  async getMarks(signal?: AbortSignal): Promise<Mark[]> {
    const { windows } = await this.listWindows(signal);
    return marksFromWindows(windows);
  }

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
    // key code 121 = Page Down (kVK_PageDown), 116 = Page Up (kVK_PageUp)
    const keyCode = direction === "down" ? 121 : 116;
    const n = Math.max(1, clicks);
    const script = `tell application "System Events"\n  repeat ${n} times\n    key code ${keyCode}\n    delay 0.05\n  end repeat\nend tell`;
    const result = await execFile("osascript", ["-e", script], { timeoutMs: 15000, signal });
    if (result.code !== 0) throw permissionError("scroll", result.stderr);
    return { success: true, clicks, direction, method: "keyboard" };
  }

  async getWindowText(maxChars = 50000, signal?: AbortSignal): Promise<{ text: string; length: number; truncated: boolean }> {
    // Save old clipboard content via pbpaste
    const oldResult = await execFile("pbpaste", [], { timeoutMs: 5000, signal });
    const oldClip = Buffer.from(oldResult.stdout, "utf8");

    // Cmd+A then Cmd+C via osascript
    const selectScript = `tell application "System Events"\n  keystroke "a" using command down\n  delay 0.3\n  keystroke "c" using command down\nend tell`;
    await execFile("osascript", ["-e", selectScript], { timeoutMs: 10000, signal });
    await new Promise<void>((resolve) => setTimeout(resolve, 500));

    // Read new clipboard via pbpaste
    const pasteResult = await execFile("pbpaste", [], { timeoutMs: 5000, signal });
    let text = pasteResult.stdout;

    // Restore old clipboard via pbcopy (requires stdin)
    await execFileWithStdin("pbcopy", [], oldClip, { timeoutMs: 5000, signal });

    const truncated = text.length > maxChars;
    text = truncated ? text.slice(0, maxChars) : text;
    return { text, length: text.length, truncated };
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
