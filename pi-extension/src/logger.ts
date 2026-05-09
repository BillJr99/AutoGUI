import { appendFile, mkdir } from "node:fs/promises";
import { join } from "node:path";

export interface Logger {
  log(event: string, details?: Record<string, unknown>): Promise<void>;
}

export function createLogger(extensionRoot: string): Logger {
  const logDir = join(extensionRoot, "runtime", "logs");
  const logFile = join(logDir, "autogui.log");

  return {
    async log(event: string, details: Record<string, unknown> = {}) {
      await mkdir(logDir, { recursive: true });
      const entry = {
        ts: new Date().toISOString(),
        event,
        ...details,
      };
      await appendFile(logFile, `${JSON.stringify(entry, redactLargeValues)}\n`, "utf8");
    },
  };
}

function redactLargeValues(_key: string, value: unknown) {
  if (typeof value === "string" && value.length > 2000) {
    return `${value.slice(0, 2000)}... [truncated ${value.length - 2000} chars]`;
  }
  return value;
}
