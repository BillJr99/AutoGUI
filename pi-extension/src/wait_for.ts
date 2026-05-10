/**
 * wait_for.ts — `desktop_wait_for` polling primitive.
 *
 * Mirror of Python ``wait_for.py``.  Polls the backend until any of
 * window_title / element_name / text / window_id resolves, or the
 * timeout elapses.
 */

import type { DesktopBackend } from "./types.js";

export interface WaitForResult {
  found: boolean;
  target?: string;
  elapsed: number;
  timeout?: number;
  observation?: unknown;
  targets?: Record<string, string>;
  lastObservation?: Record<string, unknown>;
  error?: string;
}

export interface WaitForOptions {
  windowTitle?: string;
  elementName?: string;
  text?: string;
  windowId?: string;
  timeout?: number;
  pollInterval?: number;
}

export async function waitFor(
  backend: DesktopBackend,
  options: WaitForOptions,
  signal?: AbortSignal,
): Promise<WaitForResult> {
  const targets: Record<string, string> = {};
  if (options.windowTitle) targets.window_title = options.windowTitle;
  if (options.elementName) targets.element_name = options.elementName;
  if (options.text) targets.text = options.text;
  if (options.windowId) targets.window_id = options.windowId;

  if (Object.keys(targets).length === 0) {
    return {
      found: false,
      elapsed: 0,
      error: "wait_for requires at least one of window_title, element_name, text, window_id.",
    };
  }

  const timeout = Math.max(0.5, options.timeout ?? 15.0);
  const pollInterval = Math.max(0.1, options.pollInterval ?? 0.5);
  const start = Date.now();
  const deadline = start + timeout * 1000;
  const lastObservation: Record<string, unknown> = {};

  while (true) {
    const elapsed = (Date.now() - start) / 1000;

    if (options.windowTitle || options.windowId) {
      try {
        const r = await backend.listWindows(signal);
        const wins = r.windows ?? [];
        for (const w of wins) {
          const title = String(w.title ?? "");
          const id = String(w.id ?? "");
          if (options.windowTitle && title.toLowerCase().includes(options.windowTitle.toLowerCase())) {
            return { found: true, target: "window_title", elapsed: elapsedSec(start), observation: w };
          }
          if (options.windowId && options.windowId === id) {
            return { found: true, target: "window_id", elapsed: elapsedSec(start), observation: w };
          }
        }
        lastObservation.windows = wins.length;
      } catch (e) {
        lastObservation.window_error = (e as Error).message;
      }
    }

    if (options.elementName && backend.findElement) {
      try {
        const el = await backend.findElement({ name: options.elementName }, signal);
        if (el && el.rect) {
          return { found: true, target: "element_name", elapsed: elapsedSec(start), observation: el };
        }
      } catch (e) {
        lastObservation.element_error = (e as Error).message;
      }
    }

    if (elapsed >= timeout) {
      return {
        found: false,
        elapsed: elapsedSec(start),
        timeout,
        targets,
        lastObservation,
      };
    }

    const remaining = (deadline - Date.now()) / 1000;
    const sleep = Math.max(0.05, Math.min(pollInterval, remaining));
    await new Promise((r) => setTimeout(r, sleep * 1000));
    if (signal?.aborted) {
      return { found: false, elapsed: elapsedSec(start), error: "aborted" };
    }
  }
}

function elapsedSec(start: number): number {
  return Math.round(((Date.now() - start) / 1000) * 1000) / 1000;
}
