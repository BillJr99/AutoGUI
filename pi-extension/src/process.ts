import { spawn } from "node:child_process";

export interface ExecResult {
  code: number | null;
  stdout: string;
  stderr: string;
  timedOut: boolean;
}

export async function execFile(
  command: string,
  args: string[] = [],
  options: { timeoutMs?: number; signal?: AbortSignal } = {},
): Promise<ExecResult> {
  const timeoutMs = options.timeoutMs ?? 15000;

  return await new Promise<ExecResult>((resolve, reject) => {
    const child = spawn(command, args, {
      stdio: ["ignore", "pipe", "pipe"],
      windowsHide: true,
    });

    let stdout = "";
    let stderr = "";
    let settled = false;
    let timedOut = false;

    const finish = (result: ExecResult) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      options.signal?.removeEventListener("abort", abort);
      resolve(result);
    };

    const abort = () => {
      child.kill("SIGTERM");
      finish({ code: null, stdout, stderr: "Aborted", timedOut: false });
    };

    const timer = setTimeout(() => {
      timedOut = true;
      child.kill("SIGTERM");
    }, timeoutMs);

    child.stdout?.setEncoding("utf8");
    child.stderr?.setEncoding("utf8");
    child.stdout?.on("data", (chunk) => {
      stdout += chunk;
    });
    child.stderr?.on("data", (chunk) => {
      stderr += chunk;
    });
    child.on("error", (error) => {
      clearTimeout(timer);
      options.signal?.removeEventListener("abort", abort);
      reject(error);
    });
    child.on("close", (code) => {
      finish({ code, stdout, stderr, timedOut });
    });

    if (options.signal?.aborted) {
      abort();
    } else {
      options.signal?.addEventListener("abort", abort, { once: true });
    }
  });
}

export async function commandExists(command: string, signal?: AbortSignal): Promise<boolean> {
  const checker = process.platform === "win32" ? "where" : "which";
  try {
    const result = await execFile(checker, [command], { timeoutMs: 3000, signal });
    return result.code === 0;
  } catch {
    return false;
  }
}

export function shellQuote(value: string): string {
  return `'${value.replace(/'/g, `'\\''`)}'`;
}

export function ensureSuccess(command: string, result: ExecResult): string {
  if (result.code !== 0) {
    const detail = result.stderr.trim() || result.stdout.trim() || `exit code ${result.code}`;
    throw new Error(`${command} failed: ${detail}`);
  }
  return result.stdout.trim();
}
