/**
 * screen_record.ts — Rolling screen buffer that flushes to a GIF on failure.
 *
 * Mirrors the mainline `screen_record.py` recorder: a fixed-size FIFO of
 * recent frames is captured at low fps in the background; on tool
 * failure the buffer is concatenated into an animated GIF so the user
 * can see *how* the agent got into trouble, not just the final state.
 *
 * Capture path: spawns the platform screenshot tool that the
 * `DesktopBackend` already knows how to use (we ask the backend for a
 * full-screen PNG).  No new image deps; GIF assembly uses ImageMagick
 * `magick`/`convert` which is the same tool we already use for the
 * Set-of-Mark overlay.  When ImageMagick isn't on PATH, the recorder
 * still keeps the frames as PNGs and writes a manifest text file so
 * the user can build the GIF later by hand.
 */

import { mkdir, rm, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { commandExists, execFile } from "./process.js";
import type { BackendLogger, DesktopBackend } from "./types.js";

export interface ScreenRecorderConfig {
  enabled: boolean;
  fps: number;
  bufferSeconds: number;
  maxWidth: number;
  outDir: string;
}

interface BufferedFrame {
  path: string;
  takenAt: number;
}

export class ScreenRecorder {
  private timer?: ReturnType<typeof setInterval>;
  private buffer: BufferedFrame[] = [];
  private active = false;
  private framesDir: string;
  private maxFrames: number;

  constructor(
    private readonly cfg: ScreenRecorderConfig,
    private readonly getBackend: () => Promise<DesktopBackend>,
    private readonly logger?: BackendLogger,
  ) {
    this.maxFrames = Math.max(1, Math.round(cfg.fps * cfg.bufferSeconds));
    this.framesDir = join(cfg.outDir, "frames");
  }

  async start(): Promise<boolean> {
    if (!this.cfg.enabled) return false;
    if (this.active) return true;
    try {
      await mkdir(this.framesDir, { recursive: true });
    } catch (e) {
      await this.logger?.log("screen_record.start.fail", { error: (e as Error).message });
      return false;
    }
    this.active = true;
    const intervalMs = Math.max(50, Math.round(1000 / Math.max(1, this.cfg.fps)));
    this.timer = setInterval(() => {
      void this.captureOne();
    }, intervalMs);
    // Don't keep the Node process alive just because we're recording.
    if (this.timer && typeof this.timer === "object" && "unref" in this.timer) {
      try { (this.timer as { unref?: () => void }).unref?.(); } catch { /* noop */ }
    }
    return true;
  }

  stop(): void {
    this.active = false;
    if (this.timer) {
      clearInterval(this.timer);
      this.timer = undefined;
    }
  }

  /**
   * Flush the rolling buffer to disk as an animated GIF.  On success returns
   * the GIF path; otherwise returns the manifest path or undefined.
   */
  async flush(label: string): Promise<string | undefined> {
    const frames = this.buffer.slice();
    if (!frames.length) return undefined;
    const safe = label.replace(/[^A-Za-z0-9._-]/g, "_").slice(0, 60);
    const stamp = nowStamp();
    const gifPath = join(this.cfg.outDir, `${stamp}_${safe}.gif`);
    const haveMagick = (await commandExists("magick")) ? "magick" : (await commandExists("convert")) ? "convert" : undefined;
    if (!haveMagick) {
      const manifest = join(this.cfg.outDir, `${stamp}_${safe}.manifest.txt`);
      await writeFile(manifest, frames.map((f) => f.path).join("\n"), "utf8");
      await this.logger?.log("screen_record.manifest_only", { manifest, frameCount: frames.length });
      return manifest;
    }
    const delay = Math.max(2, Math.round(100 / Math.max(1, this.cfg.fps))); // GIF delay in 1/100 s
    const args = [...frames.map((f) => f.path), "-delay", String(delay), "-loop", "0", gifPath];
    try {
      const r = await execFile(haveMagick, args, { timeoutMs: 30000 });
      if (r.code !== 0) {
        await this.logger?.log("screen_record.flush.fail", { stderr: r.stderr.slice(0, 400) });
        return undefined;
      }
      await this.logger?.log("screen_record.flush.ok", { gifPath, frameCount: frames.length });
      return gifPath;
    } catch (e) {
      await this.logger?.log("screen_record.flush.exception", { error: (e as Error).message });
      return undefined;
    }
  }

  private async captureOne(): Promise<void> {
    if (!this.active) return;
    try {
      const backend = await this.getBackend();
      const result = await backend.screenshot({ saveDir: this.framesDir });
      this.buffer.push({ path: result.path, takenAt: Date.now() });
      // Trim oldest.
      while (this.buffer.length > this.maxFrames) {
        const dropped = this.buffer.shift();
        if (dropped) {
          // Don't await — best-effort cleanup.
          void rm(dropped.path, { force: true });
        }
      }
    } catch (e) {
      // Capture failures are fine — the next tick will try again.
      await this.logger?.log("screen_record.capture_skip", { error: (e as Error).message });
    }
  }
}

function nowStamp(): string {
  const d = new Date();
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}${pad(d.getMonth() + 1)}${pad(d.getDate())}_${pad(d.getHours())}${pad(d.getMinutes())}${pad(d.getSeconds())}`;
}
