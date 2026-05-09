export interface Rect {
  x: number;
  y: number;
  width: number;
  height: number;
}

export interface WindowInfo extends Rect {
  id?: string;
  title: string;
  pid?: number;
  app?: string;
  active?: boolean;
}

export interface PlatformInfo {
  system: NodeJS.Platform;
  isWsl: boolean;
  isWayland: boolean;
  isX11: boolean;
  hasDisplay: boolean;
  summary: string;
}

export interface ScreenshotOptions {
  region?: Rect;
  saveDir: string;
}

export interface ScreenshotResult {
  path: string;
  width: number;
  height: number;
  mimeType: "image/png";
  data: string;
  monitors?: number;
  marks?: Mark[];
  annotated?: boolean;
}

/** A numbered handle for a UI element, used by Set-of-Mark grounding. */
export interface Mark {
  id: number;
  x: number;
  y: number;
  width: number;
  height: number;
  name?: string;
  role?: string;
  kind?: "window" | "control" | "text";
}

/** Result of a backend `findElement` lookup. */
export interface ElementInfo {
  name?: string;
  controlType?: string;
  rect: Rect;
}

/**
 * Optional per-backend capability flags.  When a flag is `false`/missing,
 * the corresponding tool either isn't registered, or is registered with a
 * graceful-degrade implementation that returns a "not supported" result.
 */
export interface BackendCapabilities {
  findElement: boolean;
  /** Set-of-Mark works on every backend (window list at minimum); flag is for
   * a richer marks list that includes a11y-tree children. */
  richMarks: boolean;
  nativeInput: boolean;
}

export interface DesktopBackend {
  name: string;
  platform: PlatformInfo;
  capabilities?: BackendCapabilities;
  status(signal?: AbortSignal): Promise<Record<string, unknown>>;
  screenshot(options: ScreenshotOptions, signal?: AbortSignal): Promise<ScreenshotResult>;
  click(x: number, y: number, button: "left" | "right" | "middle", clicks: number, signal?: AbortSignal): Promise<Record<string, unknown>>;
  typeText(text: string, signal?: AbortSignal): Promise<Record<string, unknown>>;
  hotkey(keys: string[], signal?: AbortSignal): Promise<Record<string, unknown>>;
  scroll(x: number, y: number, clicks: number, direction: "up" | "down", signal?: AbortSignal): Promise<Record<string, unknown>>;
  listWindows(signal?: AbortSignal): Promise<{ windows: WindowInfo[]; count: number }>;
  activeWindow(signal?: AbortSignal): Promise<{ window?: WindowInfo; found: boolean }>;
  focusWindow(target: { id?: string; title?: string; pid?: number; app?: string }, signal?: AbortSignal): Promise<Record<string, unknown>>;
  launch(application: string, args: string[], signal?: AbortSignal): Promise<Record<string, unknown>>;
  getCursorPos(signal?: AbortSignal): Promise<{ x: number; y: number }>;
  mouseMove(dx: number, dy: number, click: boolean, signal?: AbortSignal): Promise<Record<string, unknown>>;
  getWindowText(maxChars?: number, signal?: AbortSignal): Promise<{ text: string; length: number; truncated: boolean }>;
  /** Optional: AT-SPI / UIAutomation / AppleScript-AX element lookup. */
  findElement?(query: { name?: string; controlType?: string; windowTitle?: string; index?: number }, signal?: AbortSignal): Promise<ElementInfo>;
  /** Optional: derive marks from windows + a11y children for Set-of-Mark. */
  getMarks?(signal?: AbortSignal): Promise<Mark[]>;
}

export interface BackendLogger {
  log(event: string, details?: Record<string, unknown>): Promise<void>;
}

export class DesktopError extends Error {
  constructor(message: string, readonly details: Record<string, unknown> = {}) {
    super(message);
    this.name = "DesktopError";
  }
}
