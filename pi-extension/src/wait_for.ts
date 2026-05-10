/**
 * wait_for.ts — `desktop_wait_for` polling primitive.
 *
 * Mirror of Python ``wait_for.py``.  Polls the backend until any of
 * window_title / element_name / text / window_id resolves, or the
 * timeout elapses.
 */

import { findText } from "./tesseract.js";
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
  /** Where to stash interim screenshots when text-based waiting falls
   *  back to OCR.  Optional; if omitted, OCR-based waits are skipped
   *  and the timeout fires on the text target. */
  saveDir?: string;
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
    // Match the timeout/success branches' shape so callers can branch
    // on `target` / `elapsed` / `lastObservation` without KeyError-ing.
    // Mirrors the Python wait_for early-return contract.
    return {
      found: false,
      target: undefined,
      elapsed: 0,
      timeout: Math.max(0.5, options.timeout ?? 15.0),
      targets: {},
      lastObservation: {},
      error: "wait_for requires at least one of window_title, element_name, text, window_id.",
    };
  }

  // Normalise once so the loop-exit comparison and the sleep math
  // share a single ceiling — mirrors the Python wait_for clamp.
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

    // Text branch: snap a screenshot and OCR-search for the substring.
    // Skipped silently when no saveDir is provided (the caller didn't
    // wire OCR support for this invocation), so existing window/element
    // callers don't pay the cost.
    if (options.text && options.saveDir) {
      try {
        const shot = await backend.screenshot({ saveDir: options.saveDir }, signal);
        const result = await findText(shot.path, options.text, 0, signal);
        if (result.found && result.match) {
          return {
            found: true,
            target: "text",
            elapsed: elapsedSec(start),
            observation: result.match,
          };
        }
        if (result.error) lastObservation.text_error = result.error;
      } catch (e) {
        lastObservation.text_error = (e as Error).message;
      }
    } else if (options.text && !options.saveDir) {
      lastObservation.text_error = "OCR text waiting requires saveDir; not configured by caller";
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
    // Floor at 0 (NOT 0.05) — a 50ms minimum sleep would push the loop
    // past the configured deadline near the end of the window.
    // setTimeout with a non-positive delay queues a microtask, which is
    // exactly what we want when we're already at the deadline.
    const sleep = remaining <= 0 ? 0 : Math.min(pollInterval, remaining);
    await new Promise((r) => setTimeout(r, sleep * 1000));
    if (signal?.aborted) {
      return { found: false, elapsed: elapsedSec(start), error: "aborted" };
    }
  }
}

function elapsedSec(start: number): number {
  return Math.round(((Date.now() - start) / 1000) * 1000) / 1000;
}
