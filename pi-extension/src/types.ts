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
}

export interface DesktopBackend {
  name: string;
  platform: PlatformInfo;
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
