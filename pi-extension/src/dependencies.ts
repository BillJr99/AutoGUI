/**
 * dependencies.ts — Optional runtime-installer helpers.
 *
 * Two install pipelines, both opt-in via config:
 *
 *   * Tesseract OCR — needed for `desktop_click_text` / `desktop_find_text`.
 *     Installed via apt/dnf/pacman/zypper on Linux, Homebrew on macOS,
 *     winget on Windows.
 *
 *   * Playwright + Chromium — needed for the `browser_*` tool family.
 *     Installed via `npm install playwright` then `npx playwright install
 *     chromium`.
 *
 * Both are loud by design — every command run is logged to the BackendLogger
 * and printed via `console.log` so the user can see exactly what was run.
 * Each pipeline attempts at most once per process.
 */

import { spawn } from "node:child_process";
import { commandExists } from "./process.js";
import type { BackendLogger } from "./types.js";

let tesseractAttempted = false;
let playwrightAttempted = false;

export interface InstallStatus {
  ready: boolean;
  message?: string;
}

export async function ensureTesseract(
  autoInstall: boolean,
  logger?: BackendLogger,
): Promise<InstallStatus> {
  const have = await commandExists("tesseract");
  if (have) return { ready: true };

  if (!autoInstall) {
    return {
      ready: false,
      message:
        "Tesseract is not installed. Set autoInstallTesseract=true in config.json " +
        "or install manually:\n" +
        "  Linux:   sudo apt install tesseract-ocr\n" +
        "  macOS:   brew install tesseract\n" +
        "  Windows: winget install UB-Mannheim.TesseractOCR",
    };
  }

  if (tesseractAttempted) {
    return { ready: false, message: "Auto-install already attempted earlier this session." };
  }
  tesseractAttempted = true;

  await logger?.log("dependencies.tesseract.start", {});
  const ok = await runInstall(tesseractInstallCommand(), logger, "tesseract");
  return ok ? { ready: true } : { ready: false, message: "Tesseract auto-install failed; see log." };
}

export async function ensurePlaywright(
  autoInstall: boolean,
  logger?: BackendLogger,
): Promise<InstallStatus> {
  // Check if @playwright/test or playwright is reachable from the extension.
  const haveModule = await playwrightModulePresent();
  const haveChromium = haveModule && (await playwrightChromiumPresent());
  if (haveModule && haveChromium) return { ready: true };

  if (!autoInstall) {
    return {
      ready: false,
      message:
        "Playwright + Chromium not ready. Either set autoInstallPlaywright=true " +
        "in config.json or install manually:\n" +
        "  npm install playwright && npx playwright install chromium",
    };
  }

  if (playwrightAttempted) {
    return { ready: false, message: "Auto-install already attempted earlier this session." };
  }
  playwrightAttempted = true;

  await logger?.log("dependencies.playwright.start", { haveModule, haveChromium });
  if (!haveModule) {
    const ok = await runInstall(["npm", "install", "playwright"], logger, "playwright-pkg");
    if (!ok) {
      return { ready: false, message: "`npm install playwright` failed; see log." };
    }
  }
  const ok2 = await runInstall(["npx", "--yes", "playwright", "install", "chromium"], logger, "playwright-chromium");
  if (!ok2) {
    return { ready: false, message: "`playwright install chromium` failed; see log." };
  }
  return { ready: true };
}

async function playwrightModulePresent(): Promise<boolean> {
  try {
    // Dynamic import; "playwright" may not be installed and is therefore
    // not declared as a typed dep.  The string indirection prevents tsc
    // from trying to resolve the module at compile time.
    const mod: unknown = "playwright";
    await import(mod as string);
    return true;
  } catch {
    return false;
  }
}

async function playwrightChromiumPresent(): Promise<boolean> {
  try {
    const mod: unknown = "playwright";
    const pw = await import(mod as string) as { chromium?: { executablePath?: () => string } };
    const path = pw.chromium?.executablePath?.();
    return Boolean(path);
  } catch {
    return false;
  }
}

function tesseractInstallCommand(): string[] {
  if (process.platform === "darwin") return ["brew", "install", "tesseract"];
  if (process.platform === "win32") {
    return [
      "winget", "install", "--id=UB-Mannheim.TesseractOCR",
      "--silent",
      "--accept-package-agreements", "--accept-source-agreements",
    ];
  }
  // Linux + WSL (Linux side).
  return ["sudo", "apt-get", "install", "-y", "tesseract-ocr"];
}

/**
 * Spawn an installer command and stream stdout/stderr to console (so the
 * user can see what's happening) and to the trace logger.  Resolves to
 * `true` on exit code 0.
 */
function runInstall(cmd: string[], logger: BackendLogger | undefined, tag: string): Promise<boolean> {
  return new Promise((resolve) => {
    const display = cmd.join(" ");
    console.log(`[autogui-pi-ext:${tag}] $ ${display}`);
    const child = spawn(cmd[0]!, cmd.slice(1), { stdio: ["ignore", "pipe", "pipe"] });
    let stderr = "";
    child.stdout.on("data", (chunk) => process.stdout.write(chunk));
    child.stderr.on("data", (chunk) => {
      stderr += String(chunk);
      process.stderr.write(chunk);
    });
    child.on("error", async (err) => {
      console.log(`[autogui-pi-ext:${tag}] spawn failed: ${err.message}`);
      await logger?.log(`dependencies.${tag}.error`, { error: err.message });
      resolve(false);
    });
    child.on("close", async (code) => {
      const ok = code === 0;
      await logger?.log(`dependencies.${tag}.${ok ? "ok" : "fail"}`, { code, stderrTail: stderr.slice(-400) });
      resolve(ok);
    });
  });
}
