/**
 * skills.ts — JSONL-backed skill (macro) library.
 *
 * Mirror of the mainline skills.py with the same on-disk format so a
 * skill recorded by either side can be replayed by either side.  Each
 * line in `skills.jsonl` is one record:
 *
 *   {
 *     "name": str,
 *     "keywords": [str, ...],
 *     "app": str,
 *     "steps": [{"tool": str, "args": object}, ...],
 *     "created": float,        // unix seconds
 *     "success_count": int
 *   }
 *
 * Append-only writes; full rewrite is done atomically (tmp file + rename)
 * for `delete` and `incrementSuccess`.
 */

import { mkdir, readFile, rename, writeFile } from "node:fs/promises";
import { dirname } from "node:path";

export interface SkillStep {
  tool: string;
  args: Record<string, unknown>;
}

export interface Skill {
  name: string;
  keywords: string[];
  app: string;
  steps: SkillStep[];
  created: number;
  success_count: number;
}

function tokenize(s: string): string[] {
  return (s || "")
    .toLowerCase()
    .split(/\W+/)
    .filter((t) => t.length > 2);
}

export class SkillStore {
  constructor(private readonly path: string) {}

  async all(): Promise<Skill[]> {
    try {
      const text = await readFile(this.path, "utf8");
      const skills: Skill[] = [];
      for (const line of text.split(/\r?\n/)) {
        if (!line.trim()) continue;
        try {
          skills.push(JSON.parse(line) as Skill);
        } catch {
          // skip malformed lines
        }
      }
      return skills;
    } catch (e) {
      if ((e as NodeJS.ErrnoException).code === "ENOENT") return [];
      throw e;
    }
  }

  async get(name: string): Promise<Skill | undefined> {
    return (await this.all()).find((s) => s.name === name);
  }

  async search(query: string, limit = 5): Promise<Skill[]> {
    const all = await this.all();
    if (!query.trim()) return all.slice(0, limit);
    const qtoks = new Set(tokenize(query));
    if (qtoks.size === 0) return [];
    const scored: Array<{ score: number; success: number; created: number; skill: Skill }> = [];
    for (const s of all) {
      const ktoks = new Set([...tokenize(s.keywords.join(" ")), ...tokenize(s.name)]);
      let overlap = 0;
      for (const t of qtoks) if (ktoks.has(t)) overlap++;
      if (overlap === 0 && !s.name.toLowerCase().includes(query.toLowerCase())) continue;
      scored.push({ score: -overlap, success: -(s.success_count ?? 0), created: -(s.created ?? 0), skill: s });
    }
    scored.sort((a, b) =>
      a.score - b.score || a.success - b.success || a.created - b.created,
    );
    return scored.slice(0, limit).map((x) => x.skill);
  }

  async save(input: { name: string; keywords?: string[]; app?: string; steps: SkillStep[] }): Promise<Skill> {
    if (!input.name) throw new Error("Skill name is required");
    if (!input.steps?.length) throw new Error("Cannot save a skill with no steps");
    const existing = (await this.all()).filter((s) => s.name !== input.name);
    const skill: Skill = {
      name: input.name,
      keywords: input.keywords ?? [],
      app: input.app ?? "",
      steps: input.steps,
      created: Date.now() / 1000,
      success_count: 0,
    };
    existing.push(skill);
    await this.rewrite(existing);
    return skill;
  }

  async incrementSuccess(name: string): Promise<void> {
    const all = await this.all();
    let changed = false;
    for (const s of all) {
      if (s.name === name) {
        s.success_count = (s.success_count ?? 0) + 1;
        changed = true;
      }
    }
    if (changed) await this.rewrite(all);
  }

  async delete(name: string): Promise<boolean> {
    const all = await this.all();
    const kept = all.filter((s) => s.name !== name);
    if (kept.length === all.length) return false;
    await this.rewrite(kept);
    return true;
  }

  private async rewrite(skills: Skill[]): Promise<void> {
    await mkdir(dirname(this.path), { recursive: true });
    const tmp = this.path + ".tmp";
    const body = skills.map((s) => JSON.stringify(s)).join("\n") + (skills.length ? "\n" : "");
    await writeFile(tmp, body, "utf8");
    await rename(tmp, this.path);
  }
}
