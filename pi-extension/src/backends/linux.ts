import { readFile } from "node:fs/promises";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { commandExists, execFile, execFileWithStdin } from "../process.js";
import { DesktopError, type BackendCapabilities, type DesktopBackend, type ElementInfo, type Mark, type PlatformInfo, type ScreenshotOptions, type ScreenshotResult, type WindowInfo } from "../types.js";
import { createPngPath, makeScreenshotResult } from "./common.js";
import { marksFromWindows } from "../som.js";

const ATSPI_HELPER = join(dirname(dirname(fileURLToPath(import.meta.url))), "atspi_find.py");

export class LinuxBackend implements DesktopBackend {
  readonly name: string;
  readonly capabilities: BackendCapabilities = {
    findElement: true,         // attempted via the python AT-SPI helper; degrades to error if missing
    richMarks: false,          // window-level marks only on Linux v1
    nativeInput: false,        // xdotool / ydotool — already platform-native
  };

  constructor(readonly platform: PlatformInfo) {
    this.name = platform.isWayland ? "linux-wayland" : platform.isX11 ? "linux-x11" : "linux-headless";
  }

  async findElement(query: { name?: string; controlType?: string; windowTitle?: string; index?: number }, signal?: AbortSignal): Promise<ElementInfo> {
    if (!await commandExists("python3", signal)) {
      throw new DesktopError(
        "python3 not found on PATH. desktop_click_element on Linux uses an AT-SPI helper " +
        "that requires Python and pyatspi. Install with: sudo apt install python3 python3-pyatspi gir1.2-atspi-2.0",
      );
    }
    const args = [ATSPI_HELPER];
    if (query.name) args.push("--name", query.name);
    if (query.controlType) args.push("--role", query.controlType);
    if (query.windowTitle) args.push("--window", query.windowTitle);
    if (typeof query.index === "number") args.push("--index", String(query.index));
    const r = await execFile("python3", args, { timeoutMs: 15000, signal });
    if (r.code !== 0 && !r.stdout.trim()) {
      throw new DesktopError(`atspi_find helper failed: ${r.stderr.trim() || "unknown error"}`);
    }
    let parsed: Record<string, unknown>;
    try {
      parsed = JSON.parse(r.stdout.trim());
    } catch {
      throw new DesktopError(`atspi_find helper returned non-JSON: ${r.stdout.slice(0, 200)}`);
    }
    if ("error" in parsed) {
      throw new DesktopError(String(parsed.error));
    }
    return parsed as unknown as ElementInfo;
  }

  async getMarks(signal?: AbortSignal): Promise<Mark[]> {
    const { windows } = await this.listWindows(signal);
    return marksFromWindows(windows);
  }

  async status(signal?: AbortSignal): Promise<Record<string, unknown>> {
    const wanted = this.platform.isWayland ? ["grim", "swaymsg", "ydotool"] : ["import", "gnome-screenshot", "xdotool", "wmctrl"];
    const entries = await Promise.all(wanted.map(async (cmd) => [cmd, await commandExists(cmd, signal)] as const));
    return { backend: this.name, platform: this.platform.summary, hasDisplay: this.platform.hasDisplay, commands: Object.fromEntries(entries) };
  }

  async screenshot(options: ScreenshotOptions, signal?: AbortSignal): Promise<ScreenshotResult> {
    if (!this.platform.hasDisplay) throw new DesktopError("No graphical display detected.");
    const path = await createPngPath(options.saveDir);
    const r = options.region;
    if (this.platform.isWayland) {
      if (!await commandExists("grim", signal)) throw new DesktopError("Missing Wayland screenshot dependency: grim");
      const args = r ? ["-g", `${r.x},${r.y} ${r.width}x${r.height}`, path] : [path];
      const result = await execFile("grim", args, { timeoutMs: 15000, signal });
      if (result.code !== 0) throw new DesktopError("grim failed", { stderr: result.stderr, code: result.code });
    } else if (await commandExists("import", signal)) {
      const args = r ? ["-window", "root", "-crop", `${r.width}x${r.height}+${r.x}+${r.y}`, path] : ["-window", "root", path];
      const result = await execFile("import", args, { timeoutMs: 15000, signal });
      if (result.code !== 0) throw new DesktopError("import failed", { stderr: result.stderr, code: result.code });
    } else if (await commandExists("gnome-screenshot", signal)) {
      const result = await execFile("gnome-screenshot", ["-f", path], { timeoutMs: 15000, signal });
      if (result.code !== 0) throw new DesktopError("gnome-screenshot failed", { stderr: result.stderr, code: result.code });
    } else {
      throw new DesktopError("Missing Linux screenshot dependency. Install grim for Wayland or imagemagick/gnome-screenshot for X11.");
    }
    const data = (await readFile(path)).toString("base64");
    const size = pngSize(data);
    return makeScreenshotResult(path, size.width, size.height, data);
  }

  async click(x: number, y: number, button: "left" | "right" | "middle", clicks: number, signal?: AbortSignal): Promise<Record<string, unknown>> {
    if (this.platform.isWayland) throw new DesktopError("Wayland click requires ydotool support and is not enabled in this v1 backend.");
    await requireCommand("xdotool", signal);
    const buttonNum = button === "left" ? "1" : button === "middle" ? "2" : "3";
    const result = await execFile("xdotool", ["mousemove", String(x), String(y), "click", "--repeat", String(clicks), buttonNum], { timeoutMs: 5000, signal });
    if (result.code !== 0) throw new DesktopError("xdotool click failed", { stderr: result.stderr, code: result.code });
    return { success: true, x, y, button, clicks };
  }

  async typeText(text: string, signal?: AbortSignal): Promise<Record<string, unknown>> {
    if (this.platform.isWayland) throw new DesktopError("Wayland typing requires ydotool support and is not enabled in this v1 backend.");
    await requireCommand("xdotool", signal);
    const result = await execFile("xdotool", ["type", "--delay", "10", text], { timeoutMs: 15000, signal });
    if (result.code !== 0) throw new DesktopError("xdotool type failed", { stderr: result.stderr, code: result.code });
    return { success: true, length: text.length };
  }

  async hotkey(keys: string[], signal?: AbortSignal): Promise<Record<string, unknown>> {
    if (this.platform.isWayland) throw new DesktopError("Wayland hotkeys require ydotool support and are not enabled in this v1 backend.");
    await requireCommand("xdotool", signal);
    const combo = keys.join("+");
    const result = await execFile("xdotool", ["key", combo], { timeoutMs: 5000, signal });
    if (result.code !== 0) throw new DesktopError("xdotool key failed", { stderr: result.stderr, code: result.code });
    return { success: true, keys };
  }

  async scroll(_x: number, _y: number, clicks: number, direction: "up" | "down", signal?: AbortSignal): Promise<Record<string, unknown>> {
    const n = Math.max(1, clicks);
    // X11 keysym names: Next = Page Down, Prior = Page Up
    const key = direction === "down" ? "Next" : "Prior";
    if (this.platform.isWayland) {
      await requireCommand("ydotool", signal);
      for (let i = 0; i < n; i++) {
        const r = await execFile("ydotool", ["key", "--", key], { timeoutMs: 5000, signal });
        if (r.code !== 0 && i === 0) throw new DesktopError("ydotool scroll failed", { stderr: r.stderr, code: r.code });
      }
      return { success: true, clicks, direction, method: "keyboard" };
    }
    await requireCommand("xdotool", signal);
    const result = await execFile("xdotool", ["key", "--clearmodifiers", "--repeat", String(n), "--delay", "50", key], { timeoutMs: 10000, signal });
    if (result.code !== 0) throw new DesktopError("xdotool scroll failed", { stderr: result.stderr, code: result.code });
    return { success: true, clicks, direction, method: "keyboard" };
  }

  async getWindowText(maxChars = 50000, signal?: AbortSignal): Promise<{ text: string; length: number; truncated: boolean }> {
    const sleep = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms));
    if (this.platform.isWayland) {
      await requireCommand("ydotool", signal);
      if (!await commandExists("wl-paste", signal)) throw new DesktopError("Missing dependency: wl-paste (install wl-clipboard)");
      const oldResult = await execFile("wl-paste", ["--no-newline"], { timeoutMs: 5000, signal });
      const oldClip = Buffer.from(oldResult.stdout, "utf8");
      for (const key of ["ctrl+a", "ctrl+c"]) {
        const r = await execFile("ydotool", ["key", "--", key], { timeoutMs: 5000, signal });
        if (r.code !== 0) throw new DesktopError("ydotool key failed", { key, stderr: r.stderr });
        await sleep(300);
      }
      await sleep(300);
      const pasteResult = await execFile("wl-paste", ["--no-newline"], { timeoutMs: 5000, signal });
      let text = pasteResult.stdout;
      await execFileWithStdin("wl-copy", [], oldClip, { timeoutMs: 5000, signal });
      const truncated = text.length > maxChars;
      text = truncated ? text.slice(0, maxChars) : text;
      return { text, length: text.length, truncated };
    }
    // X11
    await requireCommand("xdotool", signal);
    if (!await commandExists("xclip", signal)) throw new DesktopError("Missing dependency: xclip (sudo apt install xclip)");
    const oldResult = await execFile("xclip", ["-o", "-selection", "clipboard"], { timeoutMs: 5000, signal });
    const oldClip = Buffer.from(oldResult.stdout, "utf8");
    for (const key of ["ctrl+a", "ctrl+c"]) {
      const r = await execFile("xdotool", ["key", "--clearmodifiers", key], { timeoutMs: 5000, signal });
      if (r.code !== 0) throw new DesktopError("xdotool key failed", { key, stderr: r.stderr });
      await sleep(300);
    }
    await sleep(300);
    const pasteResult = await execFile("xclip", ["-o", "-selection", "clipboard"], { timeoutMs: 5000, signal });
    let text = pasteResult.stdout;
    await execFileWithStdin("xclip", ["-i", "-selection", "clipboard"], oldClip, { timeoutMs: 5000, signal });
    const truncated = text.length > maxChars;
    text = truncated ? text.slice(0, maxChars) : text;
    return { text, length: text.length, truncated };
  }

  async listWindows(signal?: AbortSignal): Promise<{ windows: WindowInfo[]; count: number }> {
    if (this.platform.isWayland) {
      await requireCommand("swaymsg", signal);
      const result = await execFile("swaymsg", ["-t", "get_tree"], { timeoutMs: 10000, signal });
      if (result.code !== 0) throw new DesktopError("swaymsg failed", { stderr: result.stderr, code: result.code });
      const windows: WindowInfo[] = [];
      collectSwayWindows(JSON.parse(result.stdout), windows);
      return { windows, count: windows.length };
    }
    await requireCommand("wmctrl", signal);
    const result = await execFile("wmctrl", ["-lGpx"], { timeoutMs: 5000, signal });
    if (result.code !== 0) throw new DesktopError("wmctrl failed", { stderr: result.stderr, code: result.code });
    const windows = result.stdout.trim().split(/\r?\n/).filter(Boolean).map((line) => {
      const parts = line.trim().split(/\s+/);
      return {
        pid: Number(parts[2]),
        id: parts[0],
        x: Number(parts[3]),
        y: Number(parts[4]),
        width: Number(parts[5]),
        height: Number(parts[6]),
        app: parts[7],
        title: parts.slice(8).join(" "),
      };
    });
    return { windows, count: windows.length };
  }

  async activeWindow(signal?: AbortSignal): Promise<{ window?: WindowInfo; found: boolean }> {
    if (this.platform.isWayland) throw new DesktopError("Wayland active window detection is not implemented in this v1 backend.");
    await requireCommand("xdotool", signal);
    const idResult = await execFile("xdotool", ["getactivewindow"], { timeoutMs: 5000, signal });
    if (idResult.code !== 0) throw new DesktopError("xdotool getactivewindow failed", { stderr: idResult.stderr, code: idResult.code });
    const id = idResult.stdout.trim();
    const windows = (await this.listWindows(signal)).windows;
    const active = windows.find((window) => window.id && (window.id.toLowerCase() === id.toLowerCase() || Number(window.id) === Number(id)));
    if (active) return { found: true, window: { ...active, active: true } };
    const nameResult = await execFile("xdotool", ["getwindowname", id], { timeoutMs: 5000, signal });
    return { found: Boolean(id), window: { id, title: nameResult.stdout.trim(), x: 0, y: 0, width: 0, height: 0, active: true } };
  }

  async focusWindow(target: { id?: string; title?: string; pid?: number; app?: string }, signal?: AbortSignal): Promise<Record<string, unknown>> {
    if (this.platform.isWayland) throw new DesktopError("Wayland window focusing is not implemented in this v1 backend.");
    await requireCommand("wmctrl", signal);
    const windows = (await this.listWindows(signal)).windows;
    const match = windows.find((window) => {
      if (target.id && window.id) return window.id.toLowerCase() === target.id.toLowerCase();
      if (target.pid) return window.pid === target.pid;
      if (target.title) return window.title.toLowerCase().includes(target.title.toLowerCase());
      if (target.app) return window.app?.toLowerCase().includes(target.app.toLowerCase());
      return false;
    });
    if (!match?.id) throw new DesktopError("No matching window to focus.", { target });
    const result = await execFile("wmctrl", ["-ia", match.id], { timeoutMs: 5000, signal });
    if (result.code !== 0) throw new DesktopError("wmctrl focus failed", { stderr: result.stderr, code: result.code, target, match });
    return { success: true, requested: target, window: match };
  }

  async launch(application: string, args: string[], signal?: AbortSignal): Promise<Record<string, unknown>> {
    const result = await execFile(application, args, { timeoutMs: 3000, signal });
    if (result.code !== 0 && !result.timedOut) throw new DesktopError("launch failed", { stderr: result.stderr, code: result.code });
    return { success: true, application, args };
  }

  async getCursorPos(signal?: AbortSignal): Promise<{ x: number; y: number }> {
    if (this.platform.isWayland) throw new DesktopError("Wayland cursor position is not implemented in this v1 backend.");
    await requireCommand("xdotool", signal);
    const result = await execFile("xdotool", ["getmouselocation", "--shell"], { timeoutMs: 5000, signal });
    if (result.code !== 0) throw new DesktopError("xdotool getmouselocation failed", { stderr: result.stderr, code: result.code });
    return { x: Number(result.stdout.match(/X=(\d+)/)?.[1] ?? 0), y: Number(result.stdout.match(/Y=(\d+)/)?.[1] ?? 0) };
  }

  async mouseMove(dx: number, dy: number, click: boolean, signal?: AbortSignal): Promise<Record<string, unknown>> {
    const from = await this.getCursorPos(signal);
    const to = { x: Math.max(1, from.x + dx), y: Math.max(1, from.y + dy) };
    await requireCommand("xdotool", signal);
    const args = ["mousemove", String(to.x), String(to.y)];
    if (click) args.push("click", "1");
    const result = await execFile("xdotool", args, { timeoutMs: 5000, signal });
    if (result.code !== 0) throw new DesktopError("xdotool mousemove failed", { stderr: result.stderr, code: result.code });
    return { success: true, from, to, clicked: click };
  }
}

async function requireCommand(command: string, signal?: AbortSignal): Promise<void> {
  if (!await commandExists(command, signal)) throw new DesktopError(`Missing dependency: ${command}`);
}

function pngSize(data: string): { width: number; height: number } {
  const buf = Buffer.from(data, "base64");
  if (buf.length >= 24 && buf.toString("ascii", 12, 16) === "IHDR") return { width: buf.readUInt32BE(16), height: buf.readUInt32BE(20) };
  return { width: 0, height: 0 };
}

function collectSwayWindows(node: any, out: WindowInfo[]): void {
  if (node?.type === "con" && node.name && node.rect) {
    out.push({ title: node.name, app: node.app_id, x: node.rect.x, y: node.rect.y, width: node.rect.width, height: node.rect.height });
  }
  for (const child of [...(node.nodes ?? []), ...(node.floating_nodes ?? [])]) collectSwayWindows(child, out);
}
