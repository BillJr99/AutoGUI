/**
 * tesseract.ts — Thin wrapper around the tesseract CLI for click-by-text.
 *
 * Why CLI rather than a Node binding:  the official Node packages either
 * bundle a JS port (slow + WASM) or wrap the same CLI we're already
 * shelling out to.  Calling `tesseract <png> stdout tsv` is fast, has no
 * extra deps, and gives us tab-separated rows with bounding boxes and
 * confidence scores in one shot.
 */

import { commandExists, execFile } from "./process.js";
import type { Rect } from "./types.js";

export interface TextMatch extends Rect {
  text: string;
  conf: number;
}

export interface FindTextResult {
  found: boolean;
  matches: TextMatch[];
  match?: TextMatch;
  occurrence: number;
  totalMatches: number;
  error?: string;
}

export async function tesseractAvailable(signal?: AbortSignal): Promise<boolean> {
  return await commandExists("tesseract", signal);
}

/**
 * Find every occurrence of `query` (case-insensitive substring) in the
 * given PNG, returning matches sorted by reading order (top → bottom,
 * left → right).
 */
export async function findText(
  pngPath: string,
  query: string,
  occurrence = 0,
  signal?: AbortSignal,
): Promise<FindTextResult> {
  if (!query.trim()) {
    return { found: false, matches: [], occurrence: 0, totalMatches: 0, error: "query is empty" };
  }
  if (!await tesseractAvailable(signal)) {
    return {
      found: false,
      matches: [],
      occurrence: 0,
      totalMatches: 0,
      error:
        "Tesseract is not installed. Set autoInstallTesseract=true in config.json " +
        "or install manually (`brew/apt/winget` install tesseract).",
    };
  }

  const result = await execFile("tesseract", [pngPath, "stdout", "tsv"], {
    timeoutMs: 30000,
    signal,
  });
  if (result.code !== 0) {
    return { found: false, matches: [], occurrence: 0, totalMatches: 0, error: result.stderr.slice(0, 400) || "tesseract failed" };
  }

  const q = query.trim().toLowerCase();
  const matches: TextMatch[] = [];
  // tesseract TSV columns: level page block para line word left top width height conf text
  const lines = result.stdout.split(/\r?\n/);
  for (const line of lines) {
    if (!line) continue;
    const cols = line.split("\t");
    if (cols.length < 12) continue;
    const text = cols[11]!;
    if (!text || !text.trim()) continue;
    if (!text.toLowerCase().includes(q)) continue;
    matches.push({
      text: text.trim(),
      x: Number(cols[6]),
      y: Number(cols[7]),
      width: Number(cols[8]),
      height: Number(cols[9]),
      conf: Math.max(0, Number(cols[10] ?? 0)),
    });
  }

  if (!matches.length) {
    return { found: false, matches: [], occurrence: 0, totalMatches: 0 };
  }
  const idx = Math.max(0, Math.min(Math.floor(occurrence), matches.length - 1));
  return {
    found: true,
    matches,
    match: matches[idx],
    occurrence: idx,
    totalMatches: matches.length,
  };
}
