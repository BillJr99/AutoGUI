import { readFile } from "node:fs/promises";
import os from "node:os";
import type { BackendLogger, DesktopBackend, PlatformInfo } from "./types.js";
import { LinuxBackend } from "./backends/linux.js";
import { MacBackend } from "./backends/macos.js";
import { PowerShellBackend } from "./backends/powershell.js";

async function procVersionContainsMicrosoft(): Promise<boolean> {
  try {
    return (await readFile("/proc/version", "utf8")).toLowerCase().includes("microsoft");
  } catch {
    return false;
  }
}

export async function detectPlatform(): Promise<PlatformInfo> {
  const system = process.platform;
  const release = os.release().toLowerCase();
  const isWsl = system === "linux" && (release.includes("microsoft") || release.includes("wsl") || await procVersionContainsMicrosoft());
  const isWayland = system === "linux" && !isWsl && Boolean(process.env.WAYLAND_DISPLAY);
  const isX11 = system === "linux" && !isWsl && !isWayland && Boolean(process.env.DISPLAY);
  const hasDisplay = system === "win32" || system === "darwin" || isWsl || isWayland || isX11;

  let summary: string;
  if (isWsl) summary = `WSL (${os.release()})`;
  else if (system === "win32") summary = `Windows ${os.release()}`;
  else if (system === "darwin") summary = `macOS ${os.release()}`;
  else if (isWayland) summary = `Linux/Wayland (${os.release()})`;
  else if (isX11) summary = `Linux/X11 (${os.release()})`;
  else summary = `${system} ${os.release()} (headless)`;

  return { system, isWsl, isWayland, isX11, hasDisplay, summary };
}

export async function createBackend(logger?: BackendLogger): Promise<DesktopBackend> {
  const platform = await detectPlatform();
  await logger?.log("platform.detected", { ...platform });
  if (platform.isWsl || platform.system === "win32") return new PowerShellBackend(platform, logger);
  if (platform.system === "darwin") return new MacBackend(platform);
  return new LinuxBackend(platform);
}
