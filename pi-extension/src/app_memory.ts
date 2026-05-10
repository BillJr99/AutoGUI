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

/** Serialise an AppRecord to the Python on-disk schema (snake_case keys).
 *  ``app_memory.py`` uses snake_case, and the README/header advertise that
 *  both runtimes can share a memory directory — so the wire format MUST
 *  match the Python mirror or cross-runtime resume falls over. */
function recordToPython(rec: AppRecord): Record<string, unknown> {
  return {
    app: rec.app,
    failure_counts: rec.failureCounts,
    success_counts: rec.successCounts,
    last_failures: rec.lastFailures,
    notes: rec.notes,
    updated: rec.updated,
  };
}

/** Parse from disk.  Accept the Python schema as the authoritative format
 *  AND the legacy camelCase shape that older extension builds wrote, so
 *  records produced before this migration still load.  Each field is
 *  type-validated — a corrupted record where, say, ``failure_counts`` is
 *  a string must not silently flow through and explode later when
 *  ``Object.entries(...)`` runs over it. */
function recordFromDisk(parsed: unknown, app: string): AppRecord {
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    return emptyRecord(app);
  }
  const o = parsed as Record<string, unknown>;
  const raw = (snake: string, camel: string): unknown =>
    o[snake] !== undefined ? o[snake] : o[camel];
  const numberMap = (v: unknown): Record<string, number> => {
    if (!v || typeof v !== "object" || Array.isArray(v)) return {};
    const out: Record<string, number> = {};
    for (const [k, val] of Object.entries(v as Record<string, unknown>)) {
      if (typeof val === "number" && Number.isFinite(val)) out[k] = val;
    }
    return out;
  };
  const objArr = (v: unknown): Record<string, unknown>[] => {
    if (!Array.isArray(v)) return [];
    return v.filter(
      (e): e is Record<string, unknown> =>
        !!e && typeof e === "object" && !Array.isArray(e),
    );
  };
  return {
    app: typeof o["app"] === "string" && o["app"] ? (o["app"] as string) : normalizeApp(app),
    failureCounts: numberMap(raw("failure_counts", "failureCounts")),
    successCounts: numberMap(raw("success_counts", "successCounts")),
    lastFailures: objArr(raw("last_failures", "lastFailures")) as AppRecord["lastFailures"],
    notes: objArr(raw("notes", "notes")) as AppRecord["notes"],
    updated: typeof o["updated"] === "number" && Number.isFinite(o["updated"] as number)
      ? (o["updated"] as number)
      : Date.now() / 1000,
  };
}

export class AppMemory {
  /**
   * ``allowWrites`` gates the mutators (recordFailure / recordSuccess /
   * addNote).  When false they are silent no-ops, the constructor
   * never touches the disk, and listApps() returns [] for a missing
   * directory — so a read-only store on a fresh checkout leaves no
   * filesystem state behind.
   */
  constructor(
    private readonly dir: string,
    private readonly allowWrites: boolean = false,
  ) {}

  /** Always safe — only fires on the write path. */
  private async ensureDir(): Promise<void> {
    await mkdir(this.dir, { recursive: true });
  }

  private pathFor(app: string): string {
    return join(this.dir, `${slug(app)}.json`);
  }

  private async load(app: string): Promise<AppRecord> {
    // No mkdir on the read path — a missing directory simply means "no
    // record yet", returned as an empty AppRecord.
    const p = this.pathFor(app);
    if (!existsSync(p)) return emptyRecord(app);
    try {
      const text = await readFile(p, "utf8");
      return recordFromDisk(JSON.parse(text), app);
    } catch {
      return emptyRecord(app);
    }
  }

  private async save(rec: AppRecord): Promise<void> {
    // Hard guard mirrored from Python: callers gate at the recorder
    // level, but a misuse never accidentally writes a memory file when
    // allowWrites is false.
    if (!this.allowWrites) return;
    await this.ensureDir();
    const p = this.pathFor(rec.app);
    const tmp = p + ".tmp";
    try {
      await writeFile(tmp, JSON.stringify(recordToPython(rec), null, 2), "utf8");
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
    // Don't mkdir on the read path; ENOENT cleanly maps to [].
    if (!existsSync(this.dir)) return [];
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

  /** Whether the store will persist new records.  Useful for tools.ts
   *  to decide whether to register memory_note. */
  get writes(): boolean {
    return this.allowWrites;
  }
}
