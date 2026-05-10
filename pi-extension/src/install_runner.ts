/**
 * install_runner.ts — pick + spawn the right install script for this OS.
 *
 * The scripts themselves live in `scripts/` at the project root and are
 * the canonical source of truth.  This module just locates the project
 * root from the extension's location and spawns the right one.
 */

import { spawn } from "node:child_process";
import { existsSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import type { BackendLogger } from "./types.js";

/** Walk up from the extension's `dist/src` (or `src/`) to find a folder
 *  containing `scripts/install-dependencies.sh`.  Stops at filesystem
 *  root.  Returns undefined when not found — the user is running from
 *  somewhere that isn't a checkout. */
export function findScriptsRoot(start: string): string | undefined {
  let cur = start;
  for (let i = 0; i < 8; i++) {
    if (existsSync(join(cur, "scripts", "install-dependencies.sh"))
        || existsSync(join(cur, "scripts", "install-dependencies.ps1"))) {
      return cur;
    }
    const parent = dirname(cur);
    if (parent === cur) break;
    cur = parent;
  }
  return undefined;
}

export interface RunResult {
  ok: boolean;
  exitCode: number | null;
  message?: string;
}

export async function runInstaller(logger?: BackendLogger): Promise<RunResult> {
  const here = dirname(fileURLToPath(import.meta.url));
  const root = findScriptsRoot(here);
  if (!root) {
    const message = "scripts/install-dependencies.* not found from " + here;
    await logger?.log("install_runner.no_scripts", { searchedFrom: here });
    return { ok: false, exitCode: null, message };
  }

  const isWin = process.platform === "win32";
  const cmd = isWin
    ? ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", join(root, "scripts", "install-dependencies.ps1")]
    : ["bash", join(root, "scripts", "install-dependencies.sh")];

  await logger?.log("install_runner.start", { cwd: root, cmd });
  console.log(`[install_runner] $ ${cmd.join(" ")}`);

  return await new Promise<RunResult>((resolve) => {
    const child = spawn(cmd[0]!, cmd.slice(1), { cwd: root, stdio: "inherit" });
    child.on("error", async (err) => {
      await logger?.log("install_runner.spawn_error", { error: err.message });
      resolve({ ok: false, exitCode: null, message: err.message });
    });
    child.on("close", async (code) => {
      const ok = code === 0;
      await logger?.log("install_runner.exit", { code, ok });
      resolve({ ok, exitCode: code });
    });
  });
}
