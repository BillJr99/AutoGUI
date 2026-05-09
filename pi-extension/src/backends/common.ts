import { mkdir, writeFile } from "node:fs/promises";
import { join } from "node:path";
import type { ScreenshotResult } from "../types.js";

export async function createPngPath(saveDir: string, prefix = "screenshot"): Promise<string> {
  await mkdir(saveDir, { recursive: true });
  const stamp = new Date().toISOString().replace(/[:.]/g, "-");
  return join(saveDir, `${prefix}_${stamp}.png`);
}

export async function savePng(base64Png: string, saveDir: string, prefix = "screenshot"): Promise<string> {
  const path = await createPngPath(saveDir, prefix);
  await writeFile(path, Buffer.from(base64Png, "base64"));
  return path;
}

export function makeScreenshotResult(path: string, width: number, height: number, data: string, monitors?: number): ScreenshotResult {
  return {
    path,
    width,
    height,
    mimeType: "image/png",
    data,
    ...(monitors === undefined ? {} : { monitors }),
  };
}

export function parseJsonArray<T>(raw: string): T[] {
  if (!raw.trim()) return [];
  const parsed = JSON.parse(raw);
  return Array.isArray(parsed) ? parsed : [parsed];
}

export function quoteAppleScript(value: string): string {
  return value.replace(/\\/g, "\\\\").replace(/"/g, '\\"');
}
