/**
 * app_memory.ts — Per-app quirk / strategy memory.
 *
 * Mirror of Python ``app_memory.py``.  JSON-per-app under <dir>/<app>.json
 * plus an index.jsonl.  All writes are best-effort + atomic via tmp+rename.
 */

import { mkdir, readFile, writeFile, rename, appendFile, readdir } from "node:fs/promises";
import { existsSync } from "node:fs";
import { join } from "node:path";

const MAX_NOTES = 20;

export interface AppRecord {
  app: string;
  failureCounts: Record<string, number>;
  successCounts: Record<string, number>;
  lastFailures: Array<{ tool: string; class: string; reason: string; ts: number }>;
  notes: Array<{ text: string; tag: string; ts: number }>;
  updated: number;
}

export function normalizeApp(app: string): string {
  if (!app) return "";
  const base = app.trim().split(/[\\/]/).pop() ?? app;
  return base.replace(/\.(exe|app|dmg)$/i, "").toLowerCase();
}

function slug(app: string): string {
  return normalizeApp(app).replace(/[^a-z0-9._-]+/g, "_") || "_unknown";
}

function emptyRecord(app: string): AppRecord {
  return {
    app: normalizeApp(app),
    failureCounts: {},
    successCounts: {},
    lastFailures: [],
    notes: [],
    updated: Date.now() / 1000,
  };
}

export class AppMemory {
  constructor(private readonly dir: string) {}

  private async ensureDir(): Promise<void> {
    await mkdir(this.dir, { recursive: true });
  }

  private pathFor(app: string): string {
    return join(this.dir, `${slug(app)}.json`);
  }

  private async load(app: string): Promise<AppRecord> {
    await this.ensureDir();
    const p = this.pathFor(app);
    if (!existsSync(p)) return emptyRecord(app);
    try {
      const text = await readFile(p, "utf8");
      return JSON.parse(text) as AppRecord;
    } catch {
      return emptyRecord(app);
    }
  }

  private async save(rec: AppRecord): Promise<void> {
    await this.ensureDir();
    const p = this.pathFor(rec.app);
    const tmp = p + ".tmp";
    try {
      await writeFile(tmp, JSON.stringify(rec, null, 2), "utf8");
      await rename(tmp, p);
      await appendFile(
        join(this.dir, "index.jsonl"),
        JSON.stringify({ app: rec.app, updated: rec.updated }) + "\n",
        "utf8",
      ).catch(() => undefined);
    } catch {
      // best-effort
    }
  }

  async recordFailure(opts: { app: string; tool: string; failureClass: string; reason?: string }): Promise<void> {
    if (!opts.app) return;
    const rec = await this.load(opts.app);
    const key = `${opts.tool}:${opts.failureClass}`;
    rec.failureCounts[key] = (rec.failureCounts[key] ?? 0) + 1;
    rec.lastFailures.push({
      tool: opts.tool, class: opts.failureClass,
      reason: (opts.reason ?? "").slice(0, 160), ts: Date.now() / 1000,
    });
    if (rec.lastFailures.length > MAX_NOTES) rec.lastFailures = rec.lastFailures.slice(-MAX_NOTES);
    rec.updated = Date.now() / 1000;
    await this.save(rec);
  }

  async recordSuccess(opts: { app: string; tool: string }): Promise<void> {
    if (!opts.app || !opts.tool) return;
    const rec = await this.load(opts.app);
    rec.successCounts[opts.tool] = (rec.successCounts[opts.tool] ?? 0) + 1;
    rec.updated = Date.now() / 1000;
    await this.save(rec);
  }

  async addNote(opts: { app: string; text: string; tag?: string }): Promise<void> {
    if (!opts.app || !opts.text) return;
    const rec = await this.load(opts.app);
    rec.notes.push({
      text: opts.text.slice(0, 400),
      tag: (opts.tag ?? "").slice(0, 40),
      ts: Date.now() / 1000,
    });
    if (rec.notes.length > MAX_NOTES) rec.notes = rec.notes.slice(-MAX_NOTES);
    rec.updated = Date.now() / 1000;
    await this.save(rec);
  }

  async get(app: string): Promise<AppRecord> {
    return this.load(app);
  }

  async hintForPlanner(app: string): Promise<string> {
    const rec = await this.load(app);
    if (
      Object.keys(rec.failureCounts).length === 0
      && Object.keys(rec.successCounts).length === 0
      && rec.notes.length === 0
    ) return "";
    const lines: string[] = [`App memory for "${rec.app}":`];
    const wins = Object.entries(rec.successCounts).sort((a, b) => b[1] - a[1]).slice(0, 5);
    if (wins.length) lines.push("  reliable tools: " + wins.map(([k, v]) => `${k}(${v})`).join(", "));
    const losses = Object.entries(rec.failureCounts).sort((a, b) => b[1] - a[1]).slice(0, 5);
    if (losses.length) lines.push("  recent failure classes: " + losses.map(([k, v]) => `${k}(${v})`).join(", "));
    for (const n of rec.notes.slice(-3)) {
      if (n.text) lines.push(`  note: ${n.text.slice(0, 160)}`);
    }
    return lines.join("\n");
  }

  async listApps(): Promise<string[]> {
    await this.ensureDir();
    try {
      const files = await readdir(this.dir);
      return files
        .filter((f) => f.endsWith(".json") && f !== "index.jsonl")
        .map((f) => f.replace(/\.json$/, ""))
        .sort();
    } catch {
      return [];
    }
  }
}
