/**
 * artifacts.ts — Stable-id artifact store (NOT content-addressed).
 *
 * Mirror of the Python ``artifacts.py`` module so observations from
 * fs_read / browser_get_text / shell_run can be referenced by id rather
 * than pasted in full into the LLM context.  IDs use the same
 * ``artifact://<8-hex>`` format and the same timestamp-salted derivation
 * — they are deliberately not content hashes, so the model can tell
 * apart "what did I see at step 3" from "what did I see at step 7"
 * even when the bodies are identical.
 *
 * Artifact store contents are not portable across the Python and TS
 * sides — each program writes into its own runtime/artifacts folder.
 */

import { createHash } from "node:crypto";
import { mkdir, readFile, writeFile, appendFile, stat } from "node:fs/promises";
import { existsSync } from "node:fs";
import { join } from "node:path";

const INLINE_BODY_LIMIT = 4096;

export interface Artifact {
  id: string;
  kind: string;
  source: string;
  summary: string;
  bytesLen: number;
  created: number;
  meta: Record<string, unknown>;
  bodyPath?: string;
  bodyInline?: string;
}

export class ArtifactStore {
  private readonly dir: string;
  private readonly indexPath: string;
  private readonly artifacts: Map<string, Artifact> = new Map();
  private loaded = false;

  constructor(directory: string) {
    this.dir = directory;
    this.indexPath = join(directory, "index.jsonl");
  }

  /** Lazy load + ensure dir exists. */
  private async ensureLoaded(): Promise<void> {
    if (this.loaded) return;
    await mkdir(this.dir, { recursive: true });
    if (existsSync(this.indexPath)) {
      try {
        const text = await readFile(this.indexPath, "utf8");
        for (const line of text.split("\n")) {
          if (!line.trim()) continue;
          try {
            const rec = JSON.parse(line) as Artifact;
            if (rec.id) this.artifacts.set(rec.id, rec);
          } catch {
            // skip malformed line
          }
        }
      } catch {
        // best-effort load
      }
    }
    this.loaded = true;
  }

  /**
   * Store ``body`` and return its artifact id.  IDs are derived from
   * sha1(kind|source|timestamp|body-prefix); identical bodies stored
   * moments apart receive different ids on purpose — the store is a
   * capture log, not a content-addressed cache.
   */
  async put(
    body: string,
    options: { kind: string; source?: string; summary?: string; meta?: Record<string, unknown> },
  ): Promise<string> {
    await this.ensureLoaded();
    const text = String(body ?? "");
    const hashInput = `${options.kind}|${options.source ?? ""}|${process.hrtime.bigint()}|${text.slice(0, 200)}`;
    const h = createHash("sha1").update(hashInput).digest("hex").slice(0, 8);
    const id = `artifact://${h}`;
    const summary = options.summary || autoSummary(options.kind, options.source ?? "", text);
    const art: Artifact = {
      id,
      kind: options.kind,
      source: options.source ?? "",
      summary,
      bytesLen: Buffer.byteLength(text, "utf8"),
      created: Date.now() / 1000,
      meta: { ...(options.meta ?? {}) },
    };
    if (text.length <= INLINE_BODY_LIMIT) {
      art.bodyInline = text;
    } else {
      const bodyPath = join(this.dir, `${h}.txt`);
      await writeFile(bodyPath, text, "utf8");
      art.bodyPath = bodyPath;
    }
    this.artifacts.set(id, art);
    await appendFile(this.indexPath, JSON.stringify(art) + "\n", "utf8");
    return id;
  }

  async get(id: string): Promise<Artifact | undefined> {
    await this.ensureLoaded();
    return this.artifacts.get(normalizeId(id));
  }

  async getBody(id: string): Promise<string | undefined> {
    const art = await this.get(id);
    if (!art) return undefined;
    if (art.bodyInline !== undefined) return art.bodyInline;
    if (art.bodyPath) {
      try {
        return await readFile(art.bodyPath, "utf8");
      } catch {
        return undefined;
      }
    }
    return "";
  }

  async listRecent(opts: { kind?: string; limit?: number } = {}): Promise<Artifact[]> {
    await this.ensureLoaded();
    const limit = opts.limit ?? 20;
    let items = Array.from(this.artifacts.values());
    if (opts.kind) items = items.filter((a) => a.kind === opts.kind);
    items.sort((a, b) => b.created - a.created);
    return items.slice(0, limit);
  }

  /** Auto-store a tool result body when it exceeds the inline limit. */
  async maybeCapture(
    body: string,
    opts: { kind: string; source?: string; summary?: string },
  ): Promise<{ id: string; preview: string } | undefined> {
    if (typeof body !== "string" || body.length <= INLINE_BODY_LIMIT) return undefined;
    const id = await this.put(body, opts);
    const preview = body.slice(0, 600) + `\n...\n[truncated; full content stored as ${id}]`;
    return { id, preview };
  }
}

function autoSummary(kind: string, source: string, body: string): string {
  const first = (body || "").split("\n")[0]?.trim() ?? "";
  const preview = first.length > 120 ? first.slice(0, 117) + "..." : first;
  if (source) return `${kind} of ${source}: ${preview}`;
  return preview ? `${kind}: ${preview}` : kind;
}

function normalizeId(id: string): string {
  if (id.startsWith("artifact://")) return id;
  return `artifact://${id.replace(/^\//, "")}`;
}

/** Best-effort filesystem freshness check; useful for tests. */
export async function pathExists(p: string): Promise<boolean> {
  try {
    await stat(p);
    return true;
  } catch {
    return false;
  }
}

