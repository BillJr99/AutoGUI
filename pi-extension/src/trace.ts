/**
 * trace.ts — Per-session JSONL trajectory log.
 *
 * Every tool call (start, success, failure) is appended as a single JSON
 * object on its own line.  The format is intentionally compatible with
 * the mainline AutoGUI trace files so a replay tool on either side can
 * consume either file.
 *
 * Lazily creates the trace file on first write so a process that never
 * uses any AutoGUI tools doesn't litter the filesystem with empty files.
 */

import { mkdir, appendFile } from "node:fs/promises";
import { join } from "node:path";
import { randomUUID } from "node:crypto";

export class TraceWriter {
  readonly sessionId: string;
  readonly path: string;
  private headerWritten = false;

  constructor(dir: string, sessionId?: string) {
    this.sessionId = sessionId ?? `${nowStamp()}_${randomUUID().slice(0, 8)}`;
    this.path = join(dir, `${this.sessionId}.jsonl`);
  }

  async writeMeta(fields: Record<string, unknown>): Promise<void> {
    await this.write({ kind: "meta", ...fields });
  }

  async writeEvent(kind: string, content: string, data?: Record<string, unknown>): Promise<void> {
    await this.write({ kind, content, data: data ?? {} });
  }

  private async write(record: Record<string, unknown>): Promise<void> {
    if (!this.headerWritten) {
      await mkdir(this.path.replace(/\/[^/]+$/, ""), { recursive: true });
      this.headerWritten = true;
    }
    const line = JSON.stringify({
      t: Date.now() / 1000,
      session_id: this.sessionId,
      ...record,
    }) + "\n";
    try {
      await appendFile(this.path, line, "utf8");
    } catch {
      // Best-effort; never throw from the trace path.
    }
  }
}

function nowStamp(): string {
  const d = new Date();
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}${pad(d.getMonth() + 1)}${pad(d.getDate())}_${pad(d.getHours())}${pad(d.getMinutes())}${pad(d.getSeconds())}`;
}
