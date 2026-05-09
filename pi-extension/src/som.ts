/**
 * som.ts — Set-of-Mark overlay.
 *
 * Given an existing PNG and a list of marks (id + rect + optional label),
 * draw numbered boxes on top so a vision-capable model can refer to UI
 * elements by id (`desktop_click_mark(3)`) instead of pixel coordinates.
 *
 * Annotation strategy:
 *   * If `convert` (ImageMagick) is on PATH, shell out to it.  This is the
 *     simplest cross-platform way to produce a labelled overlay without
 *     pulling in a heavy native image-processing dep.
 *   * Otherwise return the original PNG plus the marks list.  The model
 *     can still call `desktop_click_mark(id)`; the visual overlay just
 *     isn't there to help it pick.
 */

import { readFile, writeFile } from "node:fs/promises";
import { commandExists, execFile } from "./process.js";
import type { BackendLogger, Mark } from "./types.js";

const PALETTE = [
  "#FF3C3C", "#3CC85A", "#4682FF", "#FFA51E",
  "#C85ADC", "#1EC8C8", "#DCC83C",
];

export interface AnnotateResult {
  /** Path to the (possibly new) PNG file. */
  path: string;
  /** Whether the file is a freshly-written annotated copy. */
  annotated: boolean;
  /** PNG bytes as base64 ready to send to a multimodal model. */
  data: string;
  /** Why annotation didn't happen, if applicable.  Useful for graceful degrade. */
  reason?: string;
}

export async function annotateScreenshot(
  pngPath: string,
  marks: Mark[],
  logger?: BackendLogger,
  signal?: AbortSignal,
): Promise<AnnotateResult> {
  if (!marks.length) {
    const data = (await readFile(pngPath)).toString("base64");
    return { path: pngPath, annotated: false, data, reason: "no marks" };
  }

  const tool = await pickAnnotator(signal);
  if (!tool) {
    const data = (await readFile(pngPath)).toString("base64");
    return {
      path: pngPath,
      annotated: false,
      data,
      reason: "ImageMagick `convert` not on PATH; install imagemagick (apt/brew/winget) for annotated marks",
    };
  }

  const outPath = pngPath.replace(/\.png$/i, "") + ".marked.png";
  const drawArgs = buildImagemagickDraw(marks);
  try {
    const r = await execFile(tool, [pngPath, ...drawArgs, outPath], { timeoutMs: 15000, signal });
    if (r.code !== 0) {
      await logger?.log("som.annotate.fail", { stderr: r.stderr.slice(0, 400), code: r.code });
      const data = (await readFile(pngPath)).toString("base64");
      return { path: pngPath, annotated: false, data, reason: `convert failed: ${r.stderr.slice(0, 200)}` };
    }
    const data = (await readFile(outPath)).toString("base64");
    return { path: outPath, annotated: true, data };
  } catch (e) {
    await logger?.log("som.annotate.exception", { error: (e as Error).message });
    const data = (await readFile(pngPath)).toString("base64");
    return { path: pngPath, annotated: false, data, reason: `annotate threw: ${(e as Error).message}` };
  }
}

async function pickAnnotator(signal?: AbortSignal): Promise<string | undefined> {
  // ImageMagick 7 ships `magick`; pre-7 has `convert`.
  if (await commandExists("magick", signal)) return "magick";
  if (await commandExists("convert", signal)) return "convert";
  return undefined;
}

function buildImagemagickDraw(marks: Mark[]): string[] {
  const args: string[] = [];
  args.push("-strokewidth", "3");
  args.push("-fill", "none");
  args.push("-pointsize", "20");
  for (const m of marks) {
    if (m.width <= 1 || m.height <= 1) continue;
    const colour = PALETTE[(m.id - 1) % PALETTE.length] ?? PALETTE[0]!;
    const x2 = m.x + m.width;
    const y2 = m.y + m.height;
    args.push("-stroke", colour);
    args.push("-fill", "none");
    args.push("-draw", `rectangle ${m.x},${m.y} ${x2},${y2}`);
    // Filled tag in the top-left corner with the mark id.
    args.push("-stroke", "none");
    args.push("-fill", colour);
    const tagW = 26;
    const tagH = 24;
    const tagX = m.x;
    const tagY = m.y;
    args.push("-draw", `rectangle ${tagX},${tagY} ${tagX + tagW},${tagY + tagH}`);
    args.push("-fill", "white");
    // anchor text inside the tag — ImageMagick "text" anchor is bottom-left.
    args.push("-draw", `text ${tagX + 4},${tagY + tagH - 6} '${m.id}'`);
  }
  return args;
}

/**
 * Build a default mark list from a window list.  Each visible window
 * becomes one mark.  Backends with a richer a11y tree should override
 * `getMarks` to include child controls.
 */
export function marksFromWindows(windows: Array<{ x?: number; y?: number; width?: number; height?: number; title?: string; app?: string }>): Mark[] {
  const out: Mark[] = [];
  let id = 1;
  for (const w of windows) {
    const width = Number(w.width ?? 0);
    const height = Number(w.height ?? 0);
    if (width <= 1 || height <= 1) continue;
    out.push({
      id,
      x: Math.max(0, Math.round(Number(w.x ?? 0))),
      y: Math.max(0, Math.round(Number(w.y ?? 0))),
      width: Math.round(width),
      height: Math.round(height),
      name: ((w.title || w.app || "") as string).slice(0, 60),
      role: "window",
      kind: "window",
    });
    id++;
  }
  return out;
}

/** Re-export so callers can `writeFile(annotatedPath, data)` etc. */
export async function writePng(path: string, base64: string): Promise<void> {
  await writeFile(path, Buffer.from(base64, "base64"));
}
