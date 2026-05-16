/**
 * predicates.ts — Typed post-conditions for plan steps.
 *
 * Mirror of Python ``predicates.py``.  The pi extension uses these
 * primarily as a planning vocabulary the model writes into the typed
 * plan, so the standalone agent (mainline) can verify them
 * deterministically when the same plan replays through its
 * controller.  The extension also exposes a ``check_predicate`` tool
 * for in-loop verification when Pi's agent wants to verify a
 * post-condition explicitly.
 */

import { existsSync, readFileSync, statSync } from "node:fs";
import type { DesktopBackend } from "./types.js";

function isRegularFile(path: string): boolean {
  // Match Python predicates._check_file_presence: directories must NOT
  // satisfy file_exists and must NOT make file_absent fail.  Use stat
  // and gracefully degrade to "not a file" on any I/O error.
  try {
    return statSync(path).isFile();
  } catch {
    return false;
  }
}

function shellQuote(s: string): string {
  // POSIX shell single-quote escape: surround with single quotes and
  // replace any embedded ' with '\'' (close-quote, escaped-quote,
  // re-open-quote).  Mirrors Python's shlex.quote for non-empty input.
  return `'${s.replace(/'/g, "'\\''")}'`;
}

export const PREDICATE_KINDS = [
  "window_title_contains",
  "window_active_app",
  "file_exists",
  "file_absent",
  "file_contains",
  "url_contains",
  "text_visible",
  // Model-generated synonym — handled as text_visible.  Mirrors the
  // mainline alias in predicates.py.
  "text_in_window",
  "process_running",
  "shell_returns",
] as const;

export type PredicateKind = (typeof PREDICATE_KINDS)[number];

export interface Predicate {
  kind: PredicateKind;
  value?: string;
  path?: string;
  command?: string;
  stdout_contains?: string;
}

export interface PredicateResult {
  ok: boolean;
  kind: string;
  detail: string;
  observed?: unknown;
}

export function normalizePredicate(p: unknown): Predicate | undefined {
  if (!p || typeof p !== "object") return undefined;
  const obj = p as Record<string, unknown>;
  const kind = obj["kind"] ?? obj["type"];
  if (typeof kind !== "string" || !PREDICATE_KINDS.includes(kind as PredicateKind)) {
    return undefined;
  }
  const out: Predicate = { kind: kind as PredicateKind };
  for (const k of ["value", "path", "command", "stdout_contains"] as const) {
    const v = obj[k];
    if (typeof v === "string") (out as unknown as Record<string, unknown>)[k] = v;
  }
  return out;
}

export function renderPredicate(p: Predicate): string {
  switch (p.kind) {
    case "window_title_contains": return `a window with title containing ${JSON.stringify(p.value)}`;
    case "window_active_app":     return `the focused window's app matches ${JSON.stringify(p.value)}`;
    case "file_exists":           return `file exists: ${JSON.stringify(p.path)}`;
    case "file_absent":           return `file does NOT exist: ${JSON.stringify(p.path)}`;
    case "file_contains":         return `file ${JSON.stringify(p.path)} contains ${JSON.stringify(p.value)}`;
    case "url_contains":          return `browser URL contains ${JSON.stringify(p.value)}`;
    case "text_visible":
    case "text_in_window":        return `text visible on screen: ${JSON.stringify(p.value)}`;
    case "process_running":       return `process matching ${JSON.stringify(p.value)} running`;
    case "shell_returns": {
      const m = p.stdout_contains ? ` and stdout contains ${JSON.stringify(p.stdout_contains)}` : "";
      return `\`${(p.command ?? "").slice(0, 60)}\` exits 0${m}`;
    }
    default: return p.kind;
  }
}

/** Filesystem predicates that need no backend.  Useful in tests + offline checks. */
export function checkFilesystemPredicateSync(p: Predicate): PredicateResult {
  switch (p.kind) {
    case "file_exists": {
      const path = p.path ?? "";
      if (!path) return { ok: false, kind: p.kind, detail: "empty path" };
      if (isRegularFile(path)) {
        return { ok: true, kind: p.kind, detail: `path=${path}` };
      }
      const exists = existsSync(path);
      return {
        ok: false,
        kind: p.kind,
        detail: exists
          ? `path is a directory, not a file: ${path}`
          : `missing: ${path}`,
      };
    }
    case "file_absent": {
      const path = p.path ?? "";
      if (!path) return { ok: false, kind: p.kind, detail: "empty path" };
      // file_absent passes for both "no entry" and "directory at this path"
      // — only a regular file should count as "present".
      if (!isRegularFile(path)) {
        return { ok: true, kind: p.kind, detail: `path=${path}` };
      }
      return { ok: false, kind: p.kind, detail: `unexpectedly present: ${path}` };
    }
    case "file_contains": {
      const path = p.path ?? "";
      const needle = p.value ?? "";
      if (!path || !needle) return { ok: false, kind: p.kind, detail: "empty path or value" };
      try {
        const body = readFileSync(path, "utf8");
        return body.includes(needle)
          ? { ok: true, kind: p.kind, detail: "substring present" }
          : { ok: false, kind: p.kind, detail: `substring ${JSON.stringify(needle)} not in file` };
      } catch (e) {
        return { ok: false, kind: p.kind, detail: `read failed: ${(e as Error).message}` };
      }
    }
    default:
      return { ok: false, kind: p.kind, detail: "needs runtime; use checkPredicate" };
  }
}

/**
 * Live predicate check.  ``backend`` is required for desktop-related
 * checks; ``runShell`` is an optional shell-runner closure that the
 * caller (tools.ts) can supply when shell access is allowed.
 */
export async function checkPredicate(
  predicate: Predicate,
  backend: DesktopBackend | undefined,
  helpers: {
    runShell?: (cmd: string) => Promise<{ stdout: string; stderr: string; exitCode: number }>;
    browserEval?: (expr: string) => Promise<unknown>;
    findText?: (text: string) => Promise<{ found: boolean }>;
  },
): Promise<PredicateResult> {
  switch (predicate.kind) {
    case "file_exists":
    case "file_absent":
    case "file_contains":
      return checkFilesystemPredicateSync(predicate);
    case "window_title_contains": {
      if (!backend) return { ok: false, kind: predicate.kind, detail: "no backend" };
      const needle = (predicate.value ?? "").toLowerCase();
      if (!needle) return { ok: false, kind: predicate.kind, detail: "empty value" };
      try {
        const r = await backend.listWindows();
        const match = r.windows.find((w) => (w.title ?? "").toLowerCase().includes(needle));
        return match
          ? { ok: true, kind: predicate.kind, detail: `matched ${JSON.stringify(match.title)}`, observed: match }
          : { ok: false, kind: predicate.kind, detail: `no window contains ${JSON.stringify(predicate.value)}` };
      } catch (e) {
        return { ok: false, kind: predicate.kind, detail: `listWindows failed: ${(e as Error).message}` };
      }
    }
    case "window_active_app": {
      if (!backend) return { ok: false, kind: predicate.kind, detail: "no backend" };
      const target = (predicate.value ?? "").toLowerCase();
      if (!target) return { ok: false, kind: predicate.kind, detail: "empty value" };
      try {
        const info = await backend.activeWindow();
        if (!info.found || !info.window) return { ok: false, kind: predicate.kind, detail: "no active window" };
        const app = (info.window.app ?? "").toLowerCase();
        const title = (info.window.title ?? "").toLowerCase();
        return (app.includes(target) || title.includes(target))
          ? { ok: true, kind: predicate.kind, detail: `active app/title contains ${target}`, observed: info.window }
          : { ok: false, kind: predicate.kind, detail: `active app=${app} title=${title}`, observed: info.window };
      } catch (e) {
        return { ok: false, kind: predicate.kind, detail: `activeWindow failed: ${(e as Error).message}` };
      }
    }
    case "url_contains": {
      const needle = (predicate.value ?? "").toLowerCase();
      if (!needle) return { ok: false, kind: predicate.kind, detail: "empty value" };
      if (!helpers.browserEval) return { ok: false, kind: predicate.kind, detail: "browser unavailable" };
      try {
        const value = await helpers.browserEval("window.location.href");
        const href = String(value ?? "").toLowerCase();
        return href.includes(needle)
          ? { ok: true, kind: predicate.kind, detail: `URL ${href}`, observed: href }
          : { ok: false, kind: predicate.kind, detail: `URL ${href} does not contain ${needle}`, observed: href };
      } catch (e) {
        return { ok: false, kind: predicate.kind, detail: `browser_eval failed: ${(e as Error).message}` };
      }
    }
    case "text_visible":
    case "text_in_window": {
      const needle = predicate.value ?? "";
      if (!needle) return { ok: false, kind: predicate.kind, detail: "empty value" };
      if (!helpers.findText) return { ok: false, kind: predicate.kind, detail: "OCR unavailable" };
      try {
        const r = await helpers.findText(needle);
        return r.found
          ? { ok: true, kind: predicate.kind, detail: `text visible: ${needle}` }
          : { ok: false, kind: predicate.kind, detail: `text ${JSON.stringify(needle)} not on screen` };
      } catch (e) {
        return { ok: false, kind: predicate.kind, detail: `find_text failed: ${(e as Error).message}` };
      }
    }
    case "process_running": {
      if (!helpers.runShell) return { ok: false, kind: predicate.kind, detail: "shell unavailable" };
      const needle = predicate.value ?? "";
      if (!needle) return { ok: false, kind: predicate.kind, detail: "empty value" };
      // The needle reaches /bin/sh -c (or cmd /C) verbatim.  JSON.stringify
      // is a JS string literal, NOT shell-safe quoting — it leaves single
      // quotes / $() / backticks untouched and so allowed both broken
      // matching and command injection.  Single-quote escape on POSIX,
      // and on Windows reject obvious shell metacharacters since
      // tasklist's /FI takes a double-quoted string with no portable
      // escape for embedded double quotes.
      let cmd: string;
      if (process.platform === "win32") {
        if (/["&|<>%^]/.test(needle)) {
          return {
            ok: false, kind: predicate.kind,
            detail: `refusing to query process: needle ${JSON.stringify(needle)} contains shell metacharacters`,
          };
        }
        cmd = `tasklist /FI "IMAGENAME eq ${needle}*"`;
      } else {
        // `--` defangs a needle that starts with `-` so pgrep treats it
        // as a pattern instead of an unknown option.  shellQuote still
        // handles the value's metacharacters.  Mirrors the Python
        // predicates._check_process fix.
        cmd = `pgrep -lf -- ${shellQuote(needle)}`;
      }
      try {
        const r = await helpers.runShell(cmd);
        const found = r.stdout.toLowerCase().includes(needle.toLowerCase());
        return found
          ? { ok: true, kind: predicate.kind, detail: "process found" }
          : { ok: false, kind: predicate.kind, detail: `no process matching ${needle} found` };
      } catch (e) {
        return { ok: false, kind: predicate.kind, detail: `shell failed: ${(e as Error).message}` };
      }
    }
    case "shell_returns": {
      if (!helpers.runShell) return { ok: false, kind: predicate.kind, detail: "shell unavailable" };
      const cmd = predicate.command ?? "";
      if (!cmd) return { ok: false, kind: predicate.kind, detail: "empty command" };
      try {
        const r = await helpers.runShell(cmd);
        if (r.exitCode !== 0) return { ok: false, kind: predicate.kind, detail: `exit ${r.exitCode}` };
        if (predicate.stdout_contains && !r.stdout.includes(predicate.stdout_contains)) {
          return { ok: false, kind: predicate.kind, detail: `stdout missing ${predicate.stdout_contains}` };
        }
        return { ok: true, kind: predicate.kind, detail: "shell probe satisfied" };
      } catch (e) {
        return { ok: false, kind: predicate.kind, detail: `shell failed: ${(e as Error).message}` };
      }
    }
  }
}
