/**
 * config.ts — Extension-level configuration.
 *
 * The Pi extension intentionally has no per-session config UI; it would
 * compete with Pi's own session management.  Instead a JSON file at
 * `<extensionRoot>/config.json` (created by the user from
 * `config.json.example`) sets the knobs that affect graceful-degrade
 * behaviour, optional features, and resource locations.
 *
 * Every key has a sensible default so the extension boots cleanly even
 * without a config file.
 */

import { copyFile, readFile } from "node:fs/promises";
import { homedir } from "node:os";
import { join } from "node:path";

export interface ExtensionConfig {
  /** Creation gate ONLY (mirrors memoryEnabled).  When true, skill_save
   *  is registered so new skills can be persisted.  When false (the
   *  default), skill_save is omitted and the controller never writes a
   *  skills file — but skill_list / skill_run register whenever a
   *  SkillStore is constructed (it always is, lazily, with no disk
   *  side effects), so any pre-existing library at skillsPath remains
   *  readable and replayable. */
  skillsEnabled: boolean;
  /** Where saved skills (replayable macros) live.  Empty string resolves
   *  to <extensionRoot>/runtime/skills/skills.jsonl, intentionally
   *  distinct from the standalone Python agent's ./skills/skills.jsonl
   *  so the two libraries don't shadow each other.  The file is
   *  created lazily on first write (only when skillsEnabled is true). */
  skillsPath: string;
  /** When true, the extension records a JSONL trace of every tool call. */
  recordTrace: boolean;
  /** Directory for trace JSONL files. */
  traceDir: string;
  /** Run scripts/install-dependencies.* once at session start. */
  installDependencies: boolean;
  /** Short-lived cache so the auto-verify cycle doesn't double-grab. */
  perceptionCacheTtlMs: number;
  /** Browser tools are gated behind this flag. */
  allowedBrowser: boolean;
  browser: {
    headless: boolean;
    screenshotDir: string;
    userDataDir: string;
    viewport: { width: number; height: number };
  };
  /** When true, state-changing tools return a stub instead of executing. */
  dryRun: boolean;
  /** Allow-list of app/process substrings; empty = unrestricted. */
  allowedApps: string[];
  /** Block-list of regex patterns matched against active window titles. */
  blockedWindowTitles: string[];
  /** When true, plan-first behaviour is included in the AUTOGUI_PROMPT. */
  plannerEnabled: boolean;
  /** When true, the typed-plan controller protocol is injected into the prompt
   *  and plan_set / plan_update_step / plan_get tools are wired to the plan slot. */
  controllerEnabled: boolean;
  /** When true, large tool outputs (file bodies, page text, big stdout)
   *  are auto-captured into the on-disk artifact store and replaced
   *  inline with a short preview + id.  Default true.  Set false to
   *  disable the store entirely; ``artifactsDir`` then has no effect. */
  artifactsEnabled: boolean;
  /** Directory for the artifact store.  When empty, resolves to
   *  ``<extensionRoot>/runtime/artifacts``.  Has no effect when
   *  ``artifactsEnabled`` is false. */
  artifactsDir: string;
  /** When true, per-task progress markers are persisted under
   *  ``progressDir`` and re-running the same task text resumes from
   *  the previous attempt.  Default true.  Set false to disable. */
  progressEnabled: boolean;
  /** Directory for persistent task progress records.  Empty resolves
   *  to ``<extensionRoot>/runtime/progress``.  Has no effect when
   *  ``progressEnabled`` is false. */
  progressDir: string;
  /** Directory for the per-app memory store (failure histograms, success
   *  counts, free-form notes).  Empty resolves to
   *  ``<extensionRoot>/runtime/memory`` so reads via memory_get keep
   *  working; ``memoryEnabled`` is the actual on/off gate (writes only). */
  memoryDir: string;
  /** When true, memory_note registers and the controller persists
   *  recordFailure / recordSuccess / addNote calls.  Default false:
   *  memory_get and the planner's app-memory hints continue to read
   *  whatever is already on disk, but the extension itself never
   *  creates a memory file. */
  memoryEnabled: boolean;
  /** Hard ceilings for the per-task budget tracker.  0 = no ceiling. */
  budget: {
    maxToolCalls: number;
    maxSeconds: number;
  };
  screenRecord: {
    enabled: boolean;
    fps: number;
    bufferSeconds: number;
    maxWidth: number;
    outDir: string;
  };
  /** When false, desktop_screenshot returns only the file path (no inline image). */
  visionEnabled: boolean;
}

const DEFAULTS: ExtensionConfig = {
  skillsEnabled: false,
  skillsPath: "",  // resolved to <extensionRoot>/runtime/skills/skills.jsonl in loadConfig
  recordTrace: true,
  traceDir: "",    // resolved to <extensionRoot>/runtime/traces in loadConfig
  installDependencies: false,
  perceptionCacheTtlMs: 500,
  allowedBrowser: false,
  browser: {
    headless: false,
    screenshotDir: "",
    userDataDir: "",
    viewport: { width: 1280, height: 800 },
  },
  dryRun: false,
  allowedApps: [],
  blockedWindowTitles: [],
  plannerEnabled: true,
  controllerEnabled: true,
  artifactsEnabled: true,
  artifactsDir: "",       // resolved to <extensionRoot>/runtime/artifacts when enabled
  progressEnabled: true,
  progressDir: "",        // resolved to <extensionRoot>/runtime/progress when enabled
  memoryDir: "",          // resolved to <extensionRoot>/runtime/memory when enabled
  memoryEnabled: false,
  budget: {
    maxToolCalls: 0,
    maxSeconds: 0,
  },
  screenRecord: {
    enabled: true,
    fps: 5,
    bufferSeconds: 5.0,
    maxWidth: 960,
    outDir: "",
  },
  visionEnabled: true,
};

function deepMerge<T>(base: T, patch: unknown): T {
  if (!patch || typeof patch !== "object" || Array.isArray(patch)) return base;
  const out = { ...base } as Record<string, unknown>;
  for (const [k, v] of Object.entries(patch as Record<string, unknown>)) {
    const existing = (out as Record<string, unknown>)[k];
    if (
      v && typeof v === "object" && !Array.isArray(v)
      && existing && typeof existing === "object" && !Array.isArray(existing)
    ) {
      out[k] = deepMerge(existing, v);
    } else {
      out[k] = v;
    }
  }
  return out as T;
}

export async function loadConfig(extensionRoot: string): Promise<ExtensionConfig> {
  // Bootstrap: if config.json doesn't exist yet, seed it from the example file
  // so users get sensible defaults (allowedBrowser=true, visionEnabled=true, etc.)
  // without having to manually copy and edit the example.
  const primaryConfig = join(extensionRoot, "config.json");
  const exampleConfig = join(extensionRoot, "config.json.example");
  try {
    await readFile(primaryConfig, "utf8");
  } catch (e) {
    if ((e as NodeJS.ErrnoException).code === "ENOENT") {
      try {
        await copyFile(exampleConfig, primaryConfig);
      } catch {
        // Example missing or unreadable — proceed with built-in defaults.
      }
    }
  }

  const candidates = [
    primaryConfig,
    join(homedir(), ".autogui", "pi-extension.json"),
  ];

  let merged: ExtensionConfig = { ...DEFAULTS };

  for (const path of candidates) {
    try {
      const text = await readFile(path, "utf8");
      const parsed = JSON.parse(text);
      merged = deepMerge(merged, parsed);
    } catch (error) {
      const code = (error as NodeJS.ErrnoException).code;
      if (code !== "ENOENT") {
        // Surface parse errors so a typo isn't silently ignored.
        throw new Error(`Failed to read config at ${path}: ${(error as Error).message}`);
      }
    }
  }

  // Resolve runtime-relative defaults AFTER merging all config files so that
  // empty-string values from config.json don't silently override the absolute
  // paths we would otherwise compute here.  A non-empty value in config.json
  // is kept as-is (allowing deliberate user overrides).
  if (!merged.skillsPath) {
    merged.skillsPath = join(extensionRoot, "runtime", "skills", "skills.jsonl");
  }
  if (!merged.traceDir) {
    merged.traceDir = join(extensionRoot, "runtime", "traces");
  }
  if (!merged.browser.screenshotDir) {
    merged.browser.screenshotDir = join(extensionRoot, "runtime", "browser");
  }
  if (!merged.screenRecord.outDir) {
    merged.screenRecord.outDir = join(extensionRoot, "runtime", "failures");
  }
  // Only fill in runtime defaults when the store is enabled.  When the
  // user disables a store via its *Enabled flag, leaving the dir
  // empty is the deliberate "no path needed" signal — index.ts will
  // not construct that store at all.  This is the behaviour the
  // README documents, replacing the old "empty string disables"
  // (which was never actually honoured because the default was
  // always reapplied here).
  if (merged.artifactsEnabled && !merged.artifactsDir) {
    merged.artifactsDir = join(extensionRoot, "runtime", "artifacts");
  }
  if (merged.progressEnabled && !merged.progressDir) {
    merged.progressDir = join(extensionRoot, "runtime", "progress");
  }
  if (!merged.memoryDir) {
    // memoryEnabled is the creation gate (writes-only); reads of an
    // existing store still want a default location, so always resolve
    // a runtime path here regardless of memoryEnabled.
    merged.memoryDir = join(extensionRoot, "runtime", "memory");
  }

  return merged;
}
