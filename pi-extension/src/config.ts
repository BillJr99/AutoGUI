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
  /** Where saved skills (replayable macros) live. */
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

  return merged;
}
