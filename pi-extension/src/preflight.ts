/**
 * preflight.ts — Resource preflight checks.
 *
 * Mirror of Python ``preflight.py``.  Five check kinds: app on PATH,
 * file present, URL TCP-reachable, named tool registered with Pi, shell
 * command exits 0.  Each checker is async and returns a structured
 * ``PreflightResult`` so the report renders cleanly in either UI.
 */

import { existsSync } from "node:fs";
import { homedir } from "node:os";
import { resolve } from "node:path";
import { connect } from "node:net";
import { commandExists } from "./process.js";

export type PreflightKind = "app" | "file" | "url" | "tool" | "command";

export interface PreflightCheck {
  kind: PreflightKind;
  target: string;
  note?: string;
}

export interface PreflightResult {
  check: PreflightCheck;
  ok: boolean;
  detail: string;
}

export interface PreflightReport {
  results: PreflightResult[];
  allPassed: boolean;
}

function expand(p: string): string {
  if (!p.startsWith("~")) return resolve(p);
  return resolve(p.replace(/^~/, homedir()));
}

async function checkApp(target: string): Promise<PreflightResult> {
  if (!target) return { check: { kind: "app", target }, ok: false, detail: "empty target" };
  const candidates = [target];
  if (process.platform === "win32" && !target.toLowerCase().endsWith(".exe")) {
    candidates.push(target + ".exe");
  }
  for (const c of candidates) {
    if (await commandExists(c)) {
      return { check: { kind: "app", target }, ok: true, detail: `resolved on PATH (${c})` };
    }
  }
  return { check: { kind: "app", target }, ok: false, detail: "not on PATH" };
}

async function checkFile(target: string): Promise<PreflightResult> {
  if (!target) return { check: { kind: "file", target }, ok: false, detail: "empty path" };
  const path = expand(target);
  return existsSync(path)
    ? { check: { kind: "file", target }, ok: true, detail: `resolved to ${path}` }
    : { check: { kind: "file", target }, ok: false, detail: `missing: ${path}` };
}

async function checkUrl(target: string): Promise<PreflightResult> {
  if (!target) return { check: { kind: "url", target }, ok: false, detail: "empty url" };
  let host: string | null = null;
  let port = 80;
  try {
    const u = new URL(target);
    host = u.hostname;
    port = u.port ? Number(u.port) : (u.protocol === "https:" ? 443 : 80);
  } catch {
    return { check: { kind: "url", target }, ok: false, detail: "bad url" };
  }
  if (!host) return { check: { kind: "url", target }, ok: false, detail: "no host" };
  return await new Promise<PreflightResult>((resolve_) => {
    const sock = connect({ host, port, timeout: 4000 });
    let settled = false;
    const finish = (result: PreflightResult) => {
      if (settled) return;
      settled = true;
      try { sock.destroy(); } catch { /* ignore */ }
      resolve_(result);
    };
    sock.once("connect", () => finish({
      check: { kind: "url", target }, ok: true, detail: `${host}:${port} reachable`,
    }));
    sock.once("timeout", () => finish({
      check: { kind: "url", target }, ok: false, detail: "tcp timeout",
    }));
    sock.once("error", (e) => finish({
      check: { kind: "url", target }, ok: false, detail: `unreachable: ${e.message}`,
    }));
  });
}

async function checkTool(target: string, registeredTools: Set<string>): Promise<PreflightResult> {
  if (registeredTools.has(target)) {
    return { check: { kind: "tool", target }, ok: true, detail: "registered" };
  }
  return { check: { kind: "tool", target }, ok: false, detail: "not registered" };
}

async function checkCommand(
  target: string,
  runShell?: (cmd: string) => Promise<{ stdout: string; stderr: string; exitCode: number }>,
): Promise<PreflightResult> {
  if (!runShell) return { check: { kind: "command", target }, ok: false, detail: "shell unavailable" };
  if (!target) return { check: { kind: "command", target }, ok: false, detail: "empty command" };
  try {
    const r = await runShell(target);
    if (r.exitCode === 0) return { check: { kind: "command", target }, ok: true, detail: "ok" };
    return { check: { kind: "command", target }, ok: false, detail: `exit ${r.exitCode}: ${r.stderr.slice(0, 120)}` };
  } catch (e) {
    return { check: { kind: "command", target }, ok: false, detail: `shell failed: ${(e as Error).message}` };
  }
}

export async function runPreflight(
  checks: PreflightCheck[],
  helpers: {
    registeredTools: Set<string>;
    runShell?: (cmd: string) => Promise<{ stdout: string; stderr: string; exitCode: number }>;
  },
): Promise<PreflightReport> {
  const results = await Promise.all(checks.map(async (c) => {
    switch (c.kind) {
      case "app": return checkApp(c.target);
      case "file": return checkFile(c.target);
      case "url": return checkUrl(c.target);
      case "tool": return checkTool(c.target, helpers.registeredTools);
      case "command": return checkCommand(c.target, helpers.runShell);
      default: return {
        check: c, ok: false, detail: `unknown preflight kind: ${(c as { kind: string }).kind}`,
      };
    }
  }));
  return { results, allPassed: results.every((r) => r.ok) };
}

export function inferChecksFromPlan(planDict: Record<string, unknown> | undefined,
                                    registeredTools: Set<string>): PreflightCheck[] {
  if (!planDict) return [];
  const seen = new Set<string>();
  const out: PreflightCheck[] = [];
  const explicit = planDict["preflight"];
  if (Array.isArray(explicit)) {
    for (const e of explicit) {
      if (!e || typeof e !== "object") continue;
      const obj = e as Record<string, unknown>;
      const kind = String(obj["kind"] ?? "");
      const target = String(obj["target"] ?? "");
      if (!kind || !target) continue;
      const key = `${kind}:${target}`;
      if (seen.has(key)) continue;
      seen.add(key);
      out.push({ kind: kind as PreflightKind, target, note: String(obj["note"] ?? "") });
    }
  }
  const steps = planDict["steps"];
  if (Array.isArray(steps)) {
    for (const s of steps) {
      if (!s || typeof s !== "object") continue;
      const step = s as Record<string, unknown>;
      const hints = step["tools_hint"];
      if (Array.isArray(hints)) {
        for (const h of hints) {
          const tname = String(h);
          const key = `tool:${tname}`;
          if (!tname || seen.has(key) || registeredTools.has(tname)) continue;
          seen.add(key);
          out.push({ kind: "tool", target: tname, note: `step ${step["id"]} hints ${tname}` });
        }
      }
      const pred = step["predicate"];
      if (pred && typeof pred === "object") {
        const pobj = pred as Record<string, unknown>;
        const k = String(pobj["kind"] ?? "");
        if (k === "file_exists" || k === "file_contains") {
          const path = String(pobj["path"] ?? "");
          const key = `file:${path}`;
          if (path && !seen.has(key)) {
            seen.add(key);
            out.push({ kind: "file", target: path, note: `step ${step["id"]} predicate` });
          }
        }
      }
    }
  }
  return out;
}
