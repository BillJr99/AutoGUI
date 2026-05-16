/**
 * screen_observer_client.ts — Optional HTTP/MCP client for OS Screen Observer.
 *
 * Port of mainline `screen_observer_client.py`.  Used by tools.ts as a
 * perception overlay when an OSO server is reachable (auto-probed in
 * index.ts).  All methods return null on any failure so callers fall
 * through to the native backend path with no extra error handling.
 *
 * A 30-second cooldown prevents hammering a down server.  An MCP-backed
 * variant is supplied by index.ts when an OSO MCP server is registered
 * with the Pi runtime; the surface is identical so tools.ts doesn't care
 * which transport is in use.
 */
import type { Logger } from "./logger.js";

const COOLDOWN_MS = 30_000;

const noopLogger: Logger = { async log() { /* no-op */ } };

/** Capability keys from /api/capabilities 'supports' dict.  Defaults apply
 *  when OSO is reachable but the key is absent (older OSO versions).
 *  Mirror of `_CAP_DEFAULTS` in screen_observer_client.py. */
export const OSO_CAPABILITY_DEFAULTS: Record<string, boolean> = {
  accessibility_tree: true,
  ocr: false,
  vlm: false,
  uia_invoke: false,
  occlusion_detection: true,
  drag: true,
  screenshot: true,
  monitors: true,
  bring_to_foreground: true,
  element_targeting: true,
  observe_with_diff: true,
};

export interface OsoConfig {
  base_url?: string;
  timeout_seconds?: number;
  enabled?: boolean;
}

export interface OsoElement {
  element_id?: string;
  name: string;
  control_type: string;
  rect: { x: number; y: number; width: number; height: number };
  method: string;
}

interface TreeNode {
  name?: string;
  role?: string;
  bounds?: { x?: number; y?: number; width?: number; height?: number };
  children?: TreeNode[];
}

/** Recursively flatten OSO element tree into matching elements. */
function walkForElement(node: TreeNode | undefined, nameQuery: string, controlType: string | null): OsoElement[] {
  if (!node) return [];
  const results: OsoElement[] = [];
  const nameLower = nameQuery.toLowerCase();
  const nodeName = (node.name ?? "").toLowerCase();
  const nodeRole = node.role ?? "";
  const nameMatch = nameLower.length === 0 || nodeName.includes(nameLower);
  const typeMatch = !controlType || nodeRole.toLowerCase().includes(controlType.toLowerCase());
  if (nameMatch && typeMatch && node.bounds) {
    const b = node.bounds;
    results.push({
      name: node.name ?? "",
      control_type: nodeRole,
      rect: {
        x: b.x ?? 0,
        y: b.y ?? 0,
        width: b.width ?? 0,
        height: b.height ?? 0,
      },
      method: "screen_observer",
    });
  }
  for (const child of node.children ?? []) {
    results.push(...walkForElement(child, nameQuery, controlType));
  }
  return results;
}

/**
 * Transport seam: the REST client uses `fetch`; the MCP-backed variant in
 * index.ts implements the same interface by routing each method to an
 * MCP tool call.  Keeping the type abstract lets tools.ts depend on the
 * surface only, not the transport.
 */
export interface ScreenObserverClient {
  readonly enabled: boolean;
  readonly osoCapabilities: Record<string, boolean>;
  isAvailable(): Promise<boolean>;
  getCapabilities(): Promise<Record<string, unknown> | null>;
  getWindows(): Promise<Record<string, unknown> | null>;
  getScreenshot(windowIndex?: number): Promise<Record<string, unknown> | null>;
  getMonitors(): Promise<Record<string, unknown> | null>;
  getStructure(windowIndex?: number): Promise<{ tree?: TreeNode } | null>;
  findElement(query: { name?: string; controlType?: string; windowTitle?: string; windowIndex?: number; index?: number }): Promise<OsoElement | null>;
  findElementInTree(query: { name: string; controlType?: string; windowIndex?: number; index?: number }): Promise<OsoElement | null>;
  bringToForeground(query: { windowTitle?: string; windowIndex?: number; windowUid?: string }): Promise<Record<string, unknown> | null>;
  observe(query?: { windowIndex?: number; windowTitle?: string }): Promise<Record<string, unknown> | null>;
  elementClick(elementId: string): Promise<Record<string, unknown> | null>;
  elementFocus(elementId: string): Promise<Record<string, unknown> | null>;
  elementInvoke(elementId: string): Promise<Record<string, unknown> | null>;
  elementSetValue(elementId: string, value: string): Promise<Record<string, unknown> | null>;
}

export class RestScreenObserverClient implements ScreenObserverClient {
  private base: string;
  private timeoutMs: number;
  private _enabled: boolean;
  private disabledUntil = 0;
  private caps: Record<string, boolean> = { ...OSO_CAPABILITY_DEFAULTS };
  private capsFetched = false;

  private logger: Logger;

  constructor(cfg: OsoConfig, logger: Logger = noopLogger) {
    this.base = (cfg.base_url ?? "http://127.0.0.1:5001").replace(/\/+$/, "");
    this.timeoutMs = Math.round((cfg.timeout_seconds ?? 2.0) * 1000);
    this._enabled = !!cfg.enabled;
    this.logger = logger;
  }

  get enabled(): boolean { return this._enabled; }
  get osoCapabilities(): Record<string, boolean> { return { ...this.caps }; }

  /** Force-enable after a successful out-of-band probe. */
  enable(): void { this._enabled = true; }

  private cooled(): boolean { return Date.now() >= this.disabledUntil; }
  private backOff(): void { this.disabledUntil = Date.now() + COOLDOWN_MS; }

  private async request(method: "GET" | "POST", path: string, params?: Record<string, unknown>, body?: unknown): Promise<Record<string, unknown> | null> {
    if (!this._enabled || !this.cooled()) return null;
    let url = `${this.base}${path}`;
    if (method === "GET" && params && Object.keys(params).length > 0) {
      const usp = new URLSearchParams();
      for (const [k, v] of Object.entries(params)) {
        if (v === undefined || v === null) continue;
        usp.append(k, String(v));
      }
      const q = usp.toString();
      if (q) url += `?${q}`;
    }
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), this.timeoutMs);
    try {
      const res = await fetch(url, {
        method,
        signal: ctrl.signal,
        headers: body !== undefined ? { "content-type": "application/json" } : undefined,
        body: body !== undefined ? JSON.stringify(body) : undefined,
      });
      if (!res.ok) {
        await this.logger.log("oso.http_error", { path, status: res.status });
        return null;
      }
      return await res.json() as Record<string, unknown>;
    } catch (e) {
      await this.logger.log("oso.request_failed", { path, error: (e as Error).message });
      this.backOff();
      return null;
    } finally {
      clearTimeout(timer);
    }
  }

  async isAvailable(): Promise<boolean> {
    const r = await this.request("GET", "/api/healthz");
    if (r !== null && !this.capsFetched) {
      await this.getCapabilities();
    }
    return r !== null;
  }

  async getCapabilities(): Promise<Record<string, unknown> | null> {
    const r = await this.request("GET", "/api/capabilities");
    if (r !== null) {
      const supports = (r.supports as Record<string, boolean>) ?? {};
      this.caps = { ...OSO_CAPABILITY_DEFAULTS, ...supports };
      this.capsFetched = true;
      await this.logger.log("oso.capabilities", { version: r.version ?? "?", supports: this.caps });
    }
    return r;
  }

  getWindows(): Promise<Record<string, unknown> | null> { return this.request("GET", "/api/windows"); }

  getScreenshot(windowIndex?: number): Promise<Record<string, unknown> | null> {
    return this.request("GET", "/api/screenshot", windowIndex !== undefined ? { window_index: windowIndex } : undefined);
  }

  getMonitors(): Promise<Record<string, unknown> | null> { return this.request("GET", "/api/monitors"); }

  getStructure(windowIndex?: number): Promise<{ tree?: TreeNode } | null> {
    return this.request("GET", "/api/structure", windowIndex !== undefined ? { window_index: windowIndex } : undefined) as Promise<{ tree?: TreeNode } | null>;
  }

  async findElementInTree(query: { name: string; controlType?: string; windowIndex?: number; index?: number }): Promise<OsoElement | null> {
    const tree = await this.getStructure(query.windowIndex);
    if (!tree) return null;
    const matches = walkForElement(tree.tree, query.name, query.controlType ?? null);
    if (matches.length === 0) return null;
    const idx = Math.max(0, Math.min(query.index ?? 0, matches.length - 1));
    return matches[idx];
  }

  async findElement(query: { name?: string; controlType?: string; windowTitle?: string; windowIndex?: number; index?: number }): Promise<OsoElement | null> {
    if (this.caps.accessibility_tree === false) return null;
    if (!query.name && !query.controlType) return null;
    const role = query.controlType ?? "*";
    const selector = query.name ? `//${role}[name*="${query.name}"]` : `//${role}`;
    const params: Record<string, unknown> = { selector };
    if (query.windowIndex !== undefined) params.window_index = query.windowIndex;
    else if (query.windowTitle) params.window_title = query.windowTitle;
    const r = await this.request("GET", "/api/find_element", params);
    if (!r || !r.ok) return null;
    const allMatches = (r.all_matches as Array<Record<string, unknown>> | undefined) ?? [];
    if (allMatches.length > 0 && (query.index ?? 0) > 0) {
      const idx = Math.max(0, Math.min(query.index!, allMatches.length - 1));
      const m = allMatches[idx];
      return {
        element_id: m.element_id as string | undefined,
        name: (m.name as string) ?? "",
        control_type: (m.role as string) ?? "",
        rect: (m.bounds as OsoElement["rect"]) ?? { x: 0, y: 0, width: 0, height: 0 },
        method: "screen_observer_find_element",
      };
    }
    const bounds = (r.bounds as OsoElement["rect"]) ?? { x: 0, y: 0, width: 0, height: 0 };
    const first = (allMatches[0] ?? {}) as Record<string, unknown>;
    return {
      element_id: r.element_id as string | undefined,
      name: (first.name as string) ?? "",
      control_type: (first.role as string) ?? "",
      rect: bounds,
      method: "screen_observer_find_element",
    };
  }

  bringToForeground(query: { windowTitle?: string; windowIndex?: number; windowUid?: string }): Promise<Record<string, unknown> | null> {
    const params: Record<string, unknown> = {};
    if (query.windowUid) params.window_uid = query.windowUid;
    else if (query.windowIndex !== undefined) params.window_index = query.windowIndex;
    else if (query.windowTitle) params.window_title = query.windowTitle;
    else return Promise.resolve(null);
    return this.request("GET", "/api/bring_to_foreground", params);
  }

  observe(query?: { windowIndex?: number; windowTitle?: string }): Promise<Record<string, unknown> | null> {
    const params: Record<string, unknown> = {};
    if (query?.windowIndex !== undefined) params.window_index = query.windowIndex;
    else if (query?.windowTitle) params.window_title = query.windowTitle;
    return this.request("GET", "/api/observe", params);
  }

  elementClick(id: string): Promise<Record<string, unknown> | null> { return this.request("POST", "/api/element/click", undefined, { element_id: id }); }
  elementFocus(id: string): Promise<Record<string, unknown> | null> { return this.request("POST", "/api/element/focus", undefined, { element_id: id }); }
  elementInvoke(id: string): Promise<Record<string, unknown> | null> { return this.request("POST", "/api/element/invoke", undefined, { element_id: id }); }
  elementSetValue(id: string, value: string): Promise<Record<string, unknown> | null> { return this.request("POST", "/api/element/set_value", undefined, { element_id: id, value }); }
}

/**
 * Probe order: caller-provided `mcpClient` first (MCP runtime), then the
 * REST endpoint at `cfg.base_url`.  Returns the first reachable client or
 * null if both fail.  Either way, the cooldown ensures repeat probes
 * don't stall startup.
 */
export async function probeScreenObserver(
  cfg: OsoConfig,
  logger: Logger = noopLogger,
  mcpProbe?: () => Promise<ScreenObserverClient | null>,
): Promise<ScreenObserverClient | null> {
  if (mcpProbe) {
    try {
      const mcp = await mcpProbe();
      if (mcp) return mcp;
    } catch (e) {
      await logger.log("oso.mcp_probe_failed", { error: (e as Error).message });
    }
  }
  const rest = new RestScreenObserverClient({ ...cfg, enabled: true }, logger);
  if (await rest.isAvailable()) return rest;
  return null;
}
