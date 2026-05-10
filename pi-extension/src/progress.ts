/**
 * progress.ts — Persistent task progress markers.
 *
 * Mirror of Python ``progress.py``.  Each task gets a stable id derived
 * from the user input text, persisted to ``<dir>/<task_id>.json``.
 * Re-running the same task returns the existing record (with completed
 * step ids) so the controller can resume.
 */

import { createHash } from "node:crypto";
import { mkdir, readFile, writeFile, rename, appendFile, readdir } from "node:fs/promises";
import { existsSync } from "node:fs";
import { join } from "node:path";

export interface TaskProgress {
  taskId: string;
  userInput: string;
  created: number;
  updated: number;
  completedStepIds: string[];
  failedStepIds: string[];
  planSnapshot: Record<string, unknown>;
  checkpointData: Record<string, unknown>;
  status: "running" | "done" | "failed" | "abandoned";
}

/** Serialise a TaskProgress to the Python on-disk schema (snake_case keys).
 *  ``progress.py``'s ``TaskProgress`` dataclass + ``asdict()`` produces
 *  snake_case, and the README/header advertise that both runtimes can
 *  share a progress directory.  Cross-runtime resume requires us to
 *  match Python on the wire. */
function progressToPython(rec: TaskProgress): Record<string, unknown> {
  return {
    task_id: rec.taskId,
    user_input: rec.userInput,
    created: rec.created,
    updated: rec.updated,
    completed_step_ids: rec.completedStepIds,
    failed_step_ids: rec.failedStepIds,
    plan_snapshot: rec.planSnapshot,
    checkpoint_data: rec.checkpointData,
    status: rec.status,
  };
}

/** Parse from disk.  Accept the Python schema as authoritative AND the
 *  legacy camelCase shape that older extension builds wrote, so existing
 *  in-flight tasks still resume after the upgrade. */
function progressFromDisk(parsed: unknown): TaskProgress | undefined {
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return undefined;
  const o = parsed as Record<string, unknown>;
  const pick = <T,>(snake: string, camel: string, fallback: T): T => {
    const v = o[snake] !== undefined ? o[snake] : o[camel];
    return (v ?? fallback) as T;
  };
  const taskId = pick("task_id", "taskId", "");
  if (!taskId) return undefined;
  const status = pick("status", "status", "running") as TaskProgress["status"];
  return {
    taskId,
    userInput: pick("user_input", "userInput", ""),
    created: pick("created", "created", Date.now() / 1000),
    updated: pick("updated", "updated", Date.now() / 1000),
    completedStepIds: pick("completed_step_ids", "completedStepIds", []),
    failedStepIds: pick("failed_step_ids", "failedStepIds", []),
    planSnapshot: pick("plan_snapshot", "planSnapshot", {}),
    checkpointData: pick("checkpoint_data", "checkpointData", {}),
    status,
  };
}

export class ProgressStore {
  private readonly dir: string;
  private readonly indexPath: string;

  constructor(directory: string) {
    this.dir = directory;
    this.indexPath = join(directory, "index.jsonl");
  }

  static deriveTaskId(userInput: string): string {
    const normalised = (userInput || "").split(/\s+/).filter(Boolean).join(" ").slice(0, 512);
    return createHash("sha1").update(normalised).digest("hex").slice(0, 12);
  }

  private async ensureDir(): Promise<void> {
    await mkdir(this.dir, { recursive: true });
  }

  private pathFor(taskId: string): string {
    return join(this.dir, `${taskId}.json`);
  }

  private async atomicWrite(path: string, data: TaskProgress): Promise<void> {
    const tmp = path + ".tmp";
    try {
      await writeFile(tmp, JSON.stringify(progressToPython(data), null, 2), "utf8");
      await rename(tmp, path);
    } catch {
      // best-effort write
    }
  }

  async load(taskId: string): Promise<TaskProgress | undefined> {
    await this.ensureDir();
    const p = this.pathFor(taskId);
    if (!existsSync(p)) return undefined;
    try {
      const text = await readFile(p, "utf8");
      return progressFromDisk(JSON.parse(text));
    } catch {
      return undefined;
    }
  }

  async openTask(userInput: string): Promise<TaskProgress> {
    await this.ensureDir();
    const taskId = ProgressStore.deriveTaskId(userInput);
    const existing = await this.load(taskId);
    if (existing && existing.status === "running") {
      existing.updated = Date.now() / 1000;
      return existing;
    }
    const now = Date.now() / 1000;
    const record: TaskProgress = {
      taskId,
      userInput,
      created: now,
      updated: now,
      completedStepIds: [],
      failedStepIds: [],
      planSnapshot: {},
      checkpointData: {},
      status: "running",
    };
    await this.atomicWrite(this.pathFor(taskId), record);
    // Index append is best-effort, mirroring atomicWrite() and the
    // Python ProgressStore.  Indexing is non-critical (resume uses
    // the per-task json files), so a transient FS error must not
    // abort the user's /autogui task.
    try {
      await appendFile(
        this.indexPath,
        JSON.stringify({
          task_id: record.taskId,
          user_input: userInput.slice(0, 160),
          updated: record.updated,
          status: record.status,
        }) + "\n",
        "utf8",
      );
    } catch {
      // swallow — per-task json file is the source of truth
    }
    return record;
  }

  async updatePlanSnapshot(rec: TaskProgress, snapshot: Record<string, unknown>): Promise<void> {
    rec.planSnapshot = { ...(snapshot ?? {}) };
    rec.updated = Date.now() / 1000;
    await this.atomicWrite(this.pathFor(rec.taskId), rec);
  }

  async markDone(rec: TaskProgress, stepId: string): Promise<void> {
    if (stepId && !rec.completedStepIds.includes(stepId)) rec.completedStepIds.push(stepId);
    rec.updated = Date.now() / 1000;
    await this.atomicWrite(this.pathFor(rec.taskId), rec);
  }

  async markFailed(rec: TaskProgress, stepId: string): Promise<void> {
    if (stepId && !rec.failedStepIds.includes(stepId)) rec.failedStepIds.push(stepId);
    rec.updated = Date.now() / 1000;
    await this.atomicWrite(this.pathFor(rec.taskId), rec);
  }

  async updateCheckpoint(rec: TaskProgress, data: Record<string, unknown>): Promise<void> {
    rec.checkpointData = { ...rec.checkpointData, ...(data ?? {}) };
    rec.updated = Date.now() / 1000;
    await this.atomicWrite(this.pathFor(rec.taskId), rec);
  }

  async finalize(rec: TaskProgress, status: TaskProgress["status"]): Promise<void> {
    rec.status = status;
    rec.updated = Date.now() / 1000;
    await this.atomicWrite(this.pathFor(rec.taskId), rec);
    // Match openTask()'s index-line shape (and the Python mirror) — the
    // index is the canonical "list of every task ever seen", so dropping
    // user_input here would make the schema inconsistent for any
    // consumer that relies on it for resume / listing.  Best-effort
    // append: finalize() runs at task end, so a successful run must
    // never surface as an extension error just because the index
    // sidecar couldn't be appended to.
    try {
      await appendFile(
        this.indexPath,
        JSON.stringify({
          task_id: rec.taskId,
          user_input: rec.userInput.slice(0, 160),
          updated: rec.updated,
          status,
        }) + "\n",
        "utf8",
      );
    } catch {
      // swallow — per-task json record is already on disk
    }
  }

  async listResumable(): Promise<TaskProgress[]> {
    await this.ensureDir();
    const entries: TaskProgress[] = [];
    try {
      const files = await readdir(this.dir);
      for (const f of files) {
        if (!f.endsWith(".json") || f === "index.jsonl") continue;
        try {
          const text = await readFile(join(this.dir, f), "utf8");
          const rec = progressFromDisk(JSON.parse(text));
          if (rec && rec.status === "running") entries.push(rec);
        } catch {
          // skip
        }
      }
    } catch {
      // dir missing — nothing to list
    }
    entries.sort((a, b) => b.updated - a.updated);
    return entries;
  }
}
