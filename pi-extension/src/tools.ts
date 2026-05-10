import { Type } from "@earendil-works/pi-ai";
import { defineTool, type ToolDefinition } from "@earendil-works/pi-coding-agent";
import type { AppMemory } from "./app_memory.js";
import type { ArtifactStore } from "./artifacts.js";
import type { BrowserBackend } from "./browser_backend.js";
import type { BudgetTracker } from "./budget.js";
import { PerceptionCache } from "./cache.js";
import type { ExtensionConfig } from "./config.js";
import {
  type Plan,
  StepStatus,
  parsePlan,
  planToDict,
  progressSummary,
  renderForPrompt,
} from "./controller.js";
import { classifyFailure } from "./failures.js";
import {
  type Predicate,
  checkPredicate,
  normalizePredicate,
  renderPredicate,
} from "./predicates.js";
import {
  inferChecksFromPlan,
  runPreflight,
  type PreflightCheck,
} from "./preflight.js";
import type { ProgressStore, TaskProgress } from "./progress.js";
import { ScreenRecorder } from "./screen_record.js";
import { normalizeSkillSteps, type SkillStep, type SkillStore } from "./skills.js";
import { annotateScreenshot } from "./som.js";
import { findText } from "./tesseract.js";
import type { TraceWriter } from "./trace.js";
import {
  type BackendLogger,
  type DesktopBackend,
  type Mark,
  type Rect,
  type ScreenshotResult,
  type WindowInfo,
} from "./types.js";
import { waitFor } from "./wait_for.js";

type BackendProvider = () => Promise<DesktopBackend>;

export interface DesktopToolOptions {
  omitScreenshotImages?: () => boolean;
  config: ExtensionConfig;
  cache: PerceptionCache;
  /** Optional — only present when config.skillsEnabled=true.  When
   *  undefined, skill_save / skill_list / skill_run are not registered
   *  and sessionSteps stays in memory but is never persisted. */
  skillStore?: SkillStore;
  trace: TraceWriter;
  recorder?: ScreenRecorder;
  browser?: BrowserBackend;
  /** Snapshot of completed `(tool, args)` pairs for skill_save. */
  sessionSteps: SkillStep[];
  /** Mutable last-marks list for desktop_click_mark resolution. */
  lastMarks: { value: Mark[] };
  /** Optional content-addressed store for large observations. */
  artifactStore?: ArtifactStore;
  /** Optional persistent task progress store. */
  progressStore?: ProgressStore;
  /** Mutable holder for the active plan (set when user starts a task). */
  plan?: { value: Plan | undefined };
  /** Mutable holder for the active progress record. */
  progressRecord?: { value: TaskProgress | undefined };
  /** Optional per-app memory store (failure histograms, success counts, notes). */
  appMemory?: AppMemory;
  /** Optional cost-telemetry tracker; reset at /autogui task start. */
  budget?: BudgetTracker;
}

const Region = Type.Object({
  x: Type.Number({ description: "Absolute screen x coordinate" }),
  y: Type.Number({ description: "Absolute screen y coordinate" }),
  width: Type.Number({ description: "Region width in pixels" }),
  height: Type.Number({ description: "Region height in pixels" }),
});

const MODAL_REGEX =
  /\b(error|warning|sign[ -]?in|password|allow|permission|are you sure|confirm|update available)\b/i;

function textResult(text: string, details: Record<string, unknown>) {
  return { content: [{ type: "text" as const, text }], details };
}

function screenshotResult(result: ScreenshotResult, omitImage = false) {
  const text = omitImage
    ? `Screenshot captured: ${result.width}x${result.height}, saved to ${result.path}. Inline image omitted because AutoGUI is in screenshot degrade mode.`
    : `Screenshot captured: ${result.width}x${result.height}, saved to ${result.path}${result.annotated ? " (with marks)" : ""}`;
  return {
    content: omitImage
      ? [{ type: "text" as const, text }]
      : [
          { type: "text" as const, text },
          { type: "image" as const, data: result.data, mimeType: result.mimeType },
        ],
    details: {
      path: result.path,
      width: result.width,
      height: result.height,
      mimeType: result.mimeType,
      monitors: result.monitors,
      imageOmitted: omitImage,
      ...(result.marks ? { marks: result.marks } : {}),
      ...(result.annotated ? { annotated: true } : {}),
    },
  };
}

/** Tools that change visible state — auto-verify, recording flush, cache invalidation hang off this set. */
const STATE_CHANGING = new Set([
  "desktop_click", "desktop_click_mark", "desktop_click_text", "desktop_click_element",
  "desktop_type", "desktop_hotkey", "desktop_scroll",
  "desktop_launch", "desktop_focus_window", "desktop_mouse_move",
  "browser_navigate", "browser_back", "browser_forward", "browser_reload",
  "browser_click", "browser_fill", "browser_press",
  "skill_run",
]);

/** Tools whose execution doesn't really change anything; skill recording skips them. */
const META_TOOLS = new Set([
  "desktop_screenshot", "desktop_screenshot_marked", "desktop_list_windows",
  "desktop_active_window", "desktop_get_cursor_pos", "desktop_get_window_text",
  "desktop_find_text", "skill_save", "skill_list", "browser_screenshot",
  "browser_get_text", "browser_eval",
]);

export function createDesktopTools(
  getBackend: BackendProvider,
  saveDir: string,
  logger?: BackendLogger,
  options: DesktopToolOptions = defaultOptions(),
): ToolDefinition[] {
  const cfg = options.config;

  /** Active-window check used by the action-scoping policy.  Returns a
   * structured-error string when the action should be refused, undefined
   * when it can proceed. */
  const checkScope = async (toolName: string, params: Record<string, unknown>, signal?: AbortSignal): Promise<string | undefined> => {
    if (cfg.allowedApps.length === 0 && cfg.blockedWindowTitles.length === 0) return undefined;
    if (toolName === "desktop_launch") {
      if (cfg.allowedApps.length === 0) return undefined;
      const app = String(params["application"] ?? "").toLowerCase();
      const stem = app.split(/[\\/.]/).filter(Boolean).pop() ?? app;
      const ok = cfg.allowedApps.some((entry) => app.includes(entry.toLowerCase()) || stem.includes(entry.toLowerCase()));
      return ok ? undefined : `Action scope: application ${JSON.stringify(app)} not in allowedApps ${JSON.stringify(cfg.allowedApps)}`;
    }
    // GUI actions: require active-window check.
    if (!STATE_CHANGING.has(toolName) || toolName.startsWith("browser_")) return undefined;
    try {
      const backend = await getBackend();
      const info = await backend.activeWindow(signal);
      if (!info.found || !info.window) return undefined;
      const title = (info.window.title ?? "").toLowerCase();
      const app = (info.window.app ?? "").toLowerCase();
      for (const pat of cfg.blockedWindowTitles) {
        try {
          if (new RegExp(pat, "i").test(title)) {
            return `Action scope: active window title ${JSON.stringify(title)} matches blockedWindowTitles ${JSON.stringify(pat)}`;
          }
        } catch { /* invalid regex — skip */ }
      }
      if (cfg.allowedApps.length) {
        const ok = cfg.allowedApps.some((entry) => app.includes(entry.toLowerCase()) || title.includes(entry.toLowerCase()));
        if (!ok) return `Action scope: active window app=${JSON.stringify(app)} title=${JSON.stringify(title)} not in allowedApps ${JSON.stringify(cfg.allowedApps)}`;
      }
    } catch {
      // Best-effort scoping — failing to check shouldn't block the action.
    }
    return undefined;
  };

  /** Capture window-list state before a tool dispatch, used by Phase 4 diff. */
  const snapshotWindows = async (signal?: AbortSignal): Promise<WindowInfo[] | undefined> => {
    try {
      const cached = options.cache.get<WindowInfo[]>("windows");
      if (cached) return cached;
      const backend = await getBackend();
      const r = await backend.listWindows(signal);
      options.cache.set("windows", r.windows);
      return r.windows;
    } catch {
      return undefined;
    }
  };

  const wrap = <T extends Record<string, unknown>>(
    toolName: string,
    fn: (params: T, signal?: AbortSignal) => Promise<ReturnType<typeof textResult> | ReturnType<typeof screenshotResult>>,
  ) => async (_id: string, params: T, signal?: AbortSignal) => {
    await logger?.log("tool.start", { toolName, params });
    void options.trace.writeEvent("tool_call", `→ ${toolName}`, { tool_name: toolName, args: params });
    options.budget?.recordTool();

    // Dry-run: state-changing tools return a stub result.
    if (cfg.dryRun && STATE_CHANGING.has(toolName)) {
      const stub = { dry_run: true, would_execute: { tool: toolName, args: params } };
      await logger?.log("tool.dry_run", { toolName, params });
      return textResult(`[dry-run] would execute ${toolName}`, stub);
    }

    // Action scoping.
    const scopeBlock = await checkScope(toolName, params, signal);
    if (scopeBlock) {
      await logger?.log("tool.scope_block", { toolName, reason: scopeBlock });
      return textResult(`[blocked] ${scopeBlock}`, { error: scopeBlock });
    }

    const preWindows = STATE_CHANGING.has(toolName) ? await snapshotWindows(signal) : undefined;

    try {
      const result = await fn(params, signal);

      // Skill recording.
      if (!META_TOOLS.has(toolName) && !toolName.startsWith("skill_")) {
        options.sessionSteps.push({ tool: toolName, args: { ...params } });
      }

      // State diff (window-set).  Only meaningful for desktop_* state-changers.
      if (preWindows && STATE_CHANGING.has(toolName) && !toolName.startsWith("browser_")) {
        try {
          options.cache.invalidate();
          const backend = await getBackend();
          const post = await backend.listWindows(signal);
          const before = new Set(preWindows.map((w) => `${w.id ?? ""}|${w.title ?? ""}`));
          const after = new Set(post.windows.map((w) => `${w.id ?? ""}|${w.title ?? ""}`));
          const added = post.windows.filter((w) => !before.has(`${w.id ?? ""}|${w.title ?? ""}`));
          const removed = preWindows.filter((w) => !after.has(`${w.id ?? ""}|${w.title ?? ""}`));
          (result.details as Record<string, unknown>).stateDiff = {
            added: added.map((w) => w.title ?? w.app ?? ""),
            removed: removed.map((w) => w.title ?? w.app ?? ""),
            preCount: preWindows.length,
            postCount: post.windows.length,
            unchanged: added.length === 0 && removed.length === 0,
          };
          const modal = added.find((w) => w.title && MODAL_REGEX.test(w.title));
          if (modal) {
            (result.details as Record<string, unknown>).unexpectedModal = modal.title;
            const firstPart = result.content[0];
            if (firstPart && firstPart.type === "text") {
              firstPart.text = `[UNEXPECTED MODAL: ${modal.title}] ${firstPart.text}`;
            }
          }
        } catch {
          // diff is best-effort
        }
      } else {
        options.cache.invalidate();
      }

      // Auto-capture: if a read-heavy tool returned a large body, store it
      // as an artifact and replace the inline body with a preview + id.
      if (options.artifactStore) {
        try {
          await maybeCaptureArtifact(toolName, params, result, options.artifactStore);
        } catch {
          // best-effort; never let artifact capture break tool dispatch
        }
      }

      void options.trace.writeEvent("tool_result", `OK ${toolName}`, { tool_name: toolName, details: result.details });
      await logger?.log("tool.success", { toolName, details: result.details });
      return result;
    } catch (error) {
      const errorMessage = error instanceof Error ? error.message : String(error);
      await logger?.log("tool.failure", {
        toolName, params, error: errorMessage,
        stack: error instanceof Error ? error.stack : undefined,
        details: typeof error === "object" && error !== null && "details" in error ? (error as { details?: unknown }).details : undefined,
      });
      void options.trace.writeEvent("tool_failure", `FAIL ${toolName}: ${errorMessage}`, { tool_name: toolName, error: errorMessage });
      // Flush rolling screen buffer to a GIF for post-mortem.
      if (options.recorder) {
        try {
          const gifPath = await options.recorder.flush(`${toolName}_failure`);
          if (gifPath) {
            void options.trace.writeEvent("failure_recording", `Saved failure recording: ${gifPath}`, { tool_name: toolName, path: gifPath });
            await logger?.log("tool.failure_recording", { toolName, path: gifPath });
          }
        } catch { /* swallow */ }
      }
      throw error;
    }
  };

  const definitions: ToolDefinition[] = [
    defineTool({
      name: "desktop_screenshot",
      label: "Screenshot",
      description: "Capture the current desktop as a PNG image. Use this before clicking and after actions to verify the screen state. For labelled UI elements, prefer desktop_screenshot_marked + desktop_click_mark, or desktop_click_element.",
      promptSnippet: "desktop_screenshot: capture the current desktop and return an image.",
      promptGuidelines: [
        "Use desktop_screenshot before any coordinate-based desktop action unless you already have current window bounds.",
        "Use desktop_screenshot after desktop actions when visual verification matters.",
      ],
      parameters: Type.Object({ region: Type.Optional(Region) }),
      executionMode: "sequential",
      execute: wrap("desktop_screenshot", async (params, signal) => {
        const backend = await getBackend();
        const region = params.region ? normalizeRect(params.region) : undefined;
        const cacheKey = region ? "" : "screenshot:full";
        if (cacheKey) {
          const cached = options.cache.get<ScreenshotResult>(cacheKey);
          if (cached) return screenshotResult(cached, Boolean(options.omitScreenshotImages?.()));
        }
        const r = await backend.screenshot({ region, saveDir }, signal);
        if (cacheKey) options.cache.set(cacheKey, r);
        return screenshotResult(r, Boolean(options.omitScreenshotImages?.()));
      }),
    }),

    defineTool({
      name: "desktop_screenshot_marked",
      label: "Screenshot (marked)",
      description: "Capture a screenshot with numbered Set-of-Mark boxes drawn over detected UI elements. Use BEFORE attempting to click any named element — then call desktop_click_mark(mark_id) using one of the ids returned in the 'marks' list.",
      promptSnippet: "desktop_screenshot_marked: take a screenshot with numbered overlay boxes for SoM-style clicking.",
      promptGuidelines: [
        "Prefer desktop_screenshot_marked + desktop_click_mark over guessing pixel coordinates.",
      ],
      parameters: Type.Object({}),
      executionMode: "sequential",
      execute: wrap("desktop_screenshot_marked", async (_params, signal) => {
        const backend = await getBackend();
        const shot = await backend.screenshot({ saveDir }, signal);
        const marks = backend.getMarks ? await backend.getMarks(signal) : [];
        options.lastMarks.value = marks;
        if (!marks.length) {
          return screenshotResult({ ...shot, marks: [], annotated: false }, Boolean(options.omitScreenshotImages?.()));
        }
        const annotated = await annotateScreenshot(shot.path, marks, logger, signal);
        return screenshotResult({
          path: annotated.path,
          width: shot.width,
          height: shot.height,
          mimeType: "image/png",
          data: annotated.data,
          monitors: shot.monitors,
          marks,
          annotated: annotated.annotated,
        }, Boolean(options.omitScreenshotImages?.()));
      }),
    }),

    defineTool({
      name: "desktop_click",
      label: "Click",
      description: "Click the mouse at absolute screen coordinates. Last-resort click — prefer desktop_click_element / desktop_click_text / desktop_click_mark whenever the target has a label or visible text.",
      promptSnippet: "desktop_click: click absolute screen coordinates (last resort).",
      promptGuidelines: [
        "For desktop_click, derive coordinates from desktop_screenshot or desktop_list_windows; do not guess.",
      ],
      parameters: Type.Object({
        x: Type.Number({ description: "Absolute screen x coordinate" }),
        y: Type.Number({ description: "Absolute screen y coordinate" }),
        button: Type.Optional(Type.Union([Type.Literal("left"), Type.Literal("right"), Type.Literal("middle")])),
        clicks: Type.Optional(Type.Number({ description: "Number of clicks" })),
      }),
      executionMode: "sequential",
      execute: wrap("desktop_click", async (params, signal) => {
        const backend = await getBackend();
        const result = await backend.click(Math.max(1, Math.round(params.x)), Math.max(1, Math.round(params.y)), params.button ?? "left", Math.max(1, Math.round(params.clicks ?? 1)), signal);
        return textResult("Clicked desktop.", result);
      }),
    }),

    defineTool({
      name: "desktop_click_mark",
      label: "Click Mark",
      description: "Click a previously-marked element by its mark id. Requires a recent desktop_screenshot_marked. Resolves the id to the centre of the recorded rect and dispatches a real click. Refresh marks if the screen has changed materially.",
      promptSnippet: "desktop_click_mark: click an element by its Set-of-Mark id.",
      parameters: Type.Object({
        mark_id: Type.Number({ description: "Numeric id from the marks list." }),
      }),
      executionMode: "sequential",
      execute: wrap("desktop_click_mark", async (params, signal) => {
        const backend = await getBackend();
        const id = Math.round(params.mark_id);
        const mark = options.lastMarks.value.find((m) => m.id === id);
        if (!mark) {
          return textResult(`No mark with id ${id} in the last marked screenshot. Call desktop_screenshot_marked first.`, { error: "unknown_mark", mark_id: id });
        }
        const cx = Math.round(mark.x + Math.max(1, mark.width / 2));
        const cy = Math.round(mark.y + Math.max(1, mark.height / 2));
        const r = await backend.click(cx, cy, "left", 1, signal);
        return textResult(`Clicked mark ${id} (${mark.name ?? "?"}).`, { ...r, mark_id: id, resolved_to: { x: cx, y: cy, name: mark.name } });
      }),
    }),

    defineTool({
      name: "desktop_click_text",
      label: "Click Text",
      description: "Find a visible text label on screen via OCR (Tesseract) and click its centre. Most reliable for unlabelled or pixel-clicked targets when no a11y-tree handle is exposed. Requires Tesseract — run `bash scripts/install-dependencies.sh` (or set `installDependencies: true` in config.json).",
      promptSnippet: "desktop_click_text: click an element by its visible text.",
      promptGuidelines: [
        "Prefer desktop_click_element first (no OCR needed); use desktop_click_text only when a11y lookup isn't available.",
      ],
      parameters: Type.Object({
        text: Type.String({ description: "Visible label to click (case-insensitive)." }),
        occurrence: Type.Optional(Type.Number({ description: "0-based index when multiple matches." })),
      }),
      executionMode: "sequential",
      execute: wrap("desktop_click_text", async (params, signal) => {
        // findText reports tesseract-missing errors itself; no separate
        // pre-check needed.  Install pipeline lives in scripts/.
        const backend = await getBackend();
        const shot = await backend.screenshot({ saveDir }, signal);
        const found = await findText(shot.path, params.text, params.occurrence ?? 0, signal);
        if (found.error) return textResult(found.error, { error: found.error });
        if (!found.found || !found.match) {
          return textResult(`No visible text matching ${JSON.stringify(params.text)} found via OCR.`, { found: false, totalMatches: found.totalMatches, error: found.error });
        }
        const m = found.match;
        const cx = Math.round(m.x + Math.max(1, m.width / 2));
        const cy = Math.round(m.y + Math.max(1, m.height / 2));
        const click = await backend.click(cx, cy, "left", 1, signal);
        return textResult(`Clicked text ${JSON.stringify(m.text)} at (${cx},${cy}).`, { ...click, occurrence: found.occurrence, totalMatches: found.totalMatches, method: "ocr" });
      }),
    }),

    defineTool({
      name: "desktop_find_text",
      label: "Find Text",
      description: "Locate visible text on screen via OCR and return its bounding rect, without clicking.",
      promptSnippet: "desktop_find_text: locate a text label on screen.",
      parameters: Type.Object({
        text: Type.String(),
        occurrence: Type.Optional(Type.Number()),
      }),
      executionMode: "sequential",
      execute: wrap("desktop_find_text", async (params, signal) => {
        const backend = await getBackend();
        const shot = await backend.screenshot({ saveDir }, signal);
        const found = await findText(shot.path, params.text, params.occurrence ?? 0, signal);
        return textResult(
          found.found && found.match
            ? `Found ${JSON.stringify(found.match.text)} at (${found.match.x},${found.match.y}).`
            : (found.error ?? "No matches."),
          { found: found.found, match: found.match, occurrence: found.occurrence, totalMatches: found.totalMatches, error: found.error },
        );
      }),
    }),

    defineTool({
      name: "desktop_click_element",
      label: "Click Element (a11y)",
      description: "Find a UI element via the OS accessibility API and click it. PREFER this whenever the target has a visible name/label — it talks to the actual control instead of guessing pixel positions, so it survives DPI scaling, window moves, and async UI redraws. Falls back gracefully on platforms where the a11y backend isn't available; in that case, use desktop_click_text or desktop_click_mark.",
      promptSnippet: "desktop_click_element: click a UI element by accessibility name/role.",
      promptGuidelines: [
        "Always prefer desktop_click_element when a control has a visible label.",
      ],
      parameters: Type.Object({
        name: Type.String({ description: "Element name or label (partial match)." }),
        control_type: Type.Optional(Type.String({ description: "Control type filter, e.g. 'button', 'edit', 'window'." })),
        window_title: Type.Optional(Type.String({ description: "Restrict the search to this window's subtree." })),
        index: Type.Optional(Type.Number({ description: "0-based index when multiple match." })),
        button: Type.Optional(Type.Union([Type.Literal("left"), Type.Literal("right"), Type.Literal("middle")])),
        clicks: Type.Optional(Type.Number({ description: "1=single, 2=double" })),
      }),
      executionMode: "sequential",
      execute: wrap("desktop_click_element", async (params, signal) => {
        const backend = await getBackend();
        if (!backend.findElement) {
          return textResult(
            `desktop_click_element is not supported on this backend (${backend.name}). ` +
            `Try desktop_click_text or desktop_click_mark instead.`,
            { error: "find_element not supported", backend: backend.name },
          );
        }
        const el = await backend.findElement({
          name: params.name,
          controlType: params.control_type,
          windowTitle: params.window_title,
          index: params.index,
        }, signal);
        const r = el.rect;
        const cx = Math.round(r.x + Math.max(1, r.width / 2));
        const cy = Math.round(r.y + Math.max(1, r.height / 2));
        const click = await backend.click(cx, cy, params.button ?? "left", Math.max(1, Math.round(params.clicks ?? 1)), signal);
        return textResult(`Clicked a11y element ${JSON.stringify(el.name ?? params.name)}.`, {
          ...click, method: "a11y",
          resolved_to: { x: cx, y: cy, name: el.name, controlType: el.controlType },
        });
      }),
    }),

    defineTool({
      name: "desktop_type",
      label: "Type",
      description: "Type text into the currently focused window.",
      promptSnippet: "desktop_type: type text into the focused desktop window.",
      promptGuidelines: [
        "Before desktop_type, focus the target with desktop_focus_window or desktop_click_element.",
      ],
      parameters: Type.Object({ text: Type.String({ description: "Text to type" }) }),
      executionMode: "sequential",
      execute: wrap("desktop_type", async (params, signal) => {
        const backend = await getBackend();
        const result = await backend.typeText(params.text, signal);
        return textResult(`Typed ${params.text.length} characters.`, result);
      }),
    }),

    defineTool({
      name: "desktop_hotkey",
      label: "Hotkey",
      description: "Press a keyboard shortcut such as ['ctrl','l'] or ['alt','f4'].",
      promptSnippet: "desktop_hotkey: press a desktop keyboard shortcut.",
      parameters: Type.Object({ keys: Type.Array(Type.String(), { description: "Keys in order, e.g. ['ctrl','l']" }) }),
      executionMode: "sequential",
      execute: wrap("desktop_hotkey", async (params, signal) => {
        const backend = await getBackend();
        const result = await backend.hotkey(params.keys, signal);
        return textResult(`Pressed hotkey ${params.keys.join("+")}.`, result);
      }),
    }),

    defineTool({
      name: "desktop_scroll",
      label: "Scroll",
      description: "Scroll the focused window. Each 'clicks' value scrolls one page (Page Down/Up) on Windows/WSL/macOS, or one notch on Linux X11. Call desktop_focus_window first. x and y are optional: when both > 0 they focus the window at that position before scrolling; pass 0 (or omit) to scroll the currently active window.",
      promptSnippet: "desktop_scroll: scroll the focused window by page.",
      parameters: Type.Object({
        x: Type.Optional(Type.Number()),
        y: Type.Optional(Type.Number()),
        clicks: Type.Optional(Type.Number()),
        direction: Type.Optional(Type.Union([Type.Literal("up"), Type.Literal("down")])),
      }),
      executionMode: "sequential",
      execute: wrap("desktop_scroll", async (params, signal) => {
        const backend = await getBackend();
        const x = Math.round(params.x ?? 0);
        const y = Math.round(params.y ?? 0);
        const result = await backend.scroll(x, y, Math.max(1, Math.round(params.clicks ?? 3)), params.direction ?? "down", signal);
        return textResult("Scrolled desktop.", result);
      }),
    }),

    defineTool({
      name: "desktop_get_window_text",
      label: "Get Window Text",
      description: "Extract visible text from the focused window via clipboard select-all + copy. The clipboard is restored after reading.",
      promptSnippet: "desktop_get_window_text: read all visible text from the focused window.",
      parameters: Type.Object({ max_chars: Type.Optional(Type.Number()) }),
      executionMode: "sequential",
      execute: wrap("desktop_get_window_text", async (params, signal) => {
        const backend = await getBackend();
        const result = await backend.getWindowText(params.max_chars ?? 50000, signal);
        const preview = result.text.slice(0, 200).replace(/\s+/g, " ").trim();
        return textResult(`Got ${result.length} characters of window text${result.truncated ? " (truncated)" : ""}. Preview: ${preview}`, result);
      }),
    }),

    defineTool({
      name: "desktop_list_windows",
      label: "List Windows",
      description: "List visible desktop windows with titles and bounds.",
      promptSnippet: "desktop_list_windows: list visible windows and screen bounds.",
      parameters: Type.Object({}),
      executionMode: "sequential",
      execute: wrap("desktop_list_windows", async (_params, signal) => {
        const cached = options.cache.get<{ windows: WindowInfo[]; count: number }>("listWindows");
        if (cached) return textResult(`Found ${cached.count} visible windows. (cached)`, cached);
        const backend = await getBackend();
        const result = await backend.listWindows(signal);
        options.cache.set("listWindows", result);
        return textResult(`Found ${result.count} visible windows.`, result);
      }),
    }),

    defineTool({
      name: "desktop_active_window",
      label: "Active Window",
      description: "Return the currently focused desktop window with title, app, pid, and bounds when available.",
      promptSnippet: "desktop_active_window: identify the currently focused desktop window.",
      parameters: Type.Object({}),
      executionMode: "sequential",
      execute: wrap("desktop_active_window", async (_params, signal) => {
        const backend = await getBackend();
        const result = await backend.activeWindow(signal);
        return textResult(result.found && result.window ? `Active window: ${result.window.title || result.window.app || result.window.id || "unknown"}.` : "No active window found.", result);
      }),
    }),

    defineTool({
      name: "desktop_focus_window",
      label: "Focus Window",
      description: "Focus a desktop window by id, pid, title substring, or app/process name.",
      promptSnippet: "desktop_focus_window: focus a window before typing.",
      parameters: Type.Object({
        id: Type.Optional(Type.String()),
        pid: Type.Optional(Type.Number()),
        title: Type.Optional(Type.String()),
        app: Type.Optional(Type.String()),
      }),
      executionMode: "sequential",
      execute: wrap("desktop_focus_window", async (params, signal) => {
        const backend = await getBackend();
        const target = {
          id: typeof params.id === "string" ? params.id : undefined,
          pid: typeof params.pid === "number" ? Math.round(params.pid) : undefined,
          title: typeof params.title === "string" ? params.title : undefined,
          app: typeof params.app === "string" ? params.app : undefined,
        };
        const result = await backend.focusWindow(target, signal);
        return textResult("Focused window.", result);
      }),
    }),

    defineTool({
      name: "desktop_launch",
      label: "Launch",
      description: "Launch an application by executable name or app name.",
      promptSnippet: "desktop_launch: launch a desktop application.",
      parameters: Type.Object({
        application: Type.String(),
        args: Type.Optional(Type.Array(Type.String())),
      }),
      executionMode: "sequential",
      execute: wrap("desktop_launch", async (params, signal) => {
        const backend = await getBackend();
        const result = await backend.launch(params.application, params.args ?? [], signal);
        return textResult(`Launched ${params.application}.`, result);
      }),
    }),

    defineTool({
      name: "desktop_get_cursor_pos",
      label: "Cursor Position",
      description: "Return the current mouse cursor position in screen pixels.",
      promptSnippet: "desktop_get_cursor_pos: get the current mouse position.",
      parameters: Type.Object({}),
      executionMode: "sequential",
      execute: wrap("desktop_get_cursor_pos", async (_params, signal) => {
        const backend = await getBackend();
        const result = await backend.getCursorPos(signal);
        return textResult(`Cursor is at ${result.x}, ${result.y}.`, result);
      }),
    }),

    defineTool({
      name: "desktop_mouse_move",
      label: "Mouse Move",
      description: "Move the mouse by a relative offset and optionally click.",
      promptSnippet: "desktop_mouse_move: move the mouse by a relative offset.",
      parameters: Type.Object({
        dx: Type.Number(), dy: Type.Number(),
        click: Type.Optional(Type.Boolean()),
      }),
      executionMode: "sequential",
      execute: wrap("desktop_mouse_move", async (params, signal) => {
        const backend = await getBackend();
        const result = await backend.mouseMove(Math.round(params.dx), Math.round(params.dy), Boolean(params.click), signal);
        return textResult("Moved cursor.", result);
      }),
    }),

  ];

  // ── Skills ──────────────────────────────────────────────────────────
  // skill_list and skill_run are read-only: registered whenever a
  // SkillStore exists so an existing library remains usable even when
  // creation is disabled.  skill_save is gated behind cfg.skillsEnabled
  // because it writes new records to disk.
  if (options.skillStore) {
    const store = options.skillStore;

    if (cfg.skillsEnabled) {
      definitions.push(
        defineTool({
          name: "skill_save",
          label: "Save Skill",
          description: "Save the sequence of tool calls completed in this session as a named, replayable skill. Provide keywords describing when this skill applies. Call after the task has succeeded.",
          promptSnippet: "skill_save: persist the recipe of what worked as a named skill.",
          parameters: Type.Object({
            name: Type.String(),
            keywords: Type.Optional(Type.Array(Type.String())),
            app: Type.Optional(Type.String()),
          }),
          executionMode: "sequential",
          execute: wrap("skill_save", async (params, _signal) => {
            if (!options.sessionSteps.length) {
              return textResult("No successful steps in this session yet to save.", { error: "no steps" });
            }
            const skill = await store.save({
              name: params.name,
              keywords: params.keywords ?? [],
              app: params.app ?? "",
              steps: [...options.sessionSteps],
            });
            return textResult(`Saved skill ${JSON.stringify(skill.name)} with ${skill.steps.length} steps.`, {
              name: skill.name,
              keywords: skill.keywords,
              app: skill.app,
              step_count: skill.steps.length,
              created: skill.created,
            });
          }),
        }),
      );
    }

    definitions.push(
      defineTool({
        name: "skill_list",
        label: "List Skills",
        description: "List saved skills, optionally filtered by a keyword query.",
        promptSnippet: "skill_list: enumerate saved skills.",
        parameters: Type.Object({
          query: Type.Optional(Type.String()),
          limit: Type.Optional(Type.Number()),
        }),
        executionMode: "sequential",
        execute: wrap("skill_list", async (params, _signal) => {
          const skills = await store.search(params.query ?? "", params.limit ?? 5);
          const summary = skills.map((s) => ({ name: s.name, app: s.app, keywords: s.keywords, step_count: s.steps.length, success_count: s.success_count }));
          return textResult(`Found ${skills.length} skills.`, { skills: summary, count: skills.length });
        }),
      }),
      defineTool({
        name: "skill_run",
        label: "Run Skill",
        description: "Replay every step of a previously saved skill in order.",
        promptSnippet: "skill_run: replay a saved skill by name.",
        parameters: Type.Object({ name: Type.String() }),
        executionMode: "sequential",
        execute: wrap("skill_run", async (params, signal) => {
          const skill = await store.get(params.name);
          if (!skill) return textResult(`No skill named ${JSON.stringify(params.name)}.`, { error: "not_found" });
          const backend = await getBackend();
          const executed: Array<{ tool: string; ok: boolean; error?: string }> = [];
          for (const step of normalizeSkillSteps(skill.steps)) {
            try {
              await replayStep(backend, options, step, signal);
              executed.push({ tool: step.tool, ok: true });
            } catch (e) {
              executed.push({ tool: step.tool, ok: false, error: (e as Error).message });
              await options.trace.writeEvent("skill_run.fail", `${skill.name} stopped at ${step.tool}`, { skill: skill.name, step });
              return textResult(`Skill ${skill.name} stopped at ${step.tool}: ${(e as Error).message}`, { executed, stopped_at: step.tool, error: (e as Error).message });
            }
          }
          await store.incrementSuccess(skill.name);
          return textResult(`Replayed skill ${skill.name} (${executed.length} steps).`, { executed, success: true });
        }),
      }),
    );
  }

  // ── Browser tools ────────────────────────────────────────────────────
  if (cfg.allowedBrowser && options.browser) {
    const b = options.browser;
    definitions.push(
      defineTool({
        name: "browser_navigate",
        label: "Browser Navigate",
        description: "Open a URL in the dedicated Playwright-driven Chromium browser. Prefer browser_* tools for web tasks.",
        promptSnippet: "browser_navigate: open a URL.",
        parameters: Type.Object({ url: Type.String() }),
        executionMode: "sequential",
        execute: wrap("browser_navigate", async (params) => textResult(`Navigated.`, await b.navigate(params.url))),
      }),
      defineTool({
        name: "browser_back", label: "Browser Back",
        description: "Go back one history entry in the browser.",
        promptSnippet: "browser_back: navigate back.",
        parameters: Type.Object({}),
        executionMode: "sequential",
        execute: wrap("browser_back", async () => textResult("Browser back.", await b.back())),
      }),
      defineTool({
        name: "browser_forward", label: "Browser Forward",
        description: "Go forward one history entry in the browser.",
        promptSnippet: "browser_forward: navigate forward.",
        parameters: Type.Object({}),
        executionMode: "sequential",
        execute: wrap("browser_forward", async () => textResult("Browser forward.", await b.forward())),
      }),
      defineTool({
        name: "browser_reload", label: "Browser Reload",
        description: "Reload the current page.",
        promptSnippet: "browser_reload: reload.",
        parameters: Type.Object({}),
        executionMode: "sequential",
        execute: wrap("browser_reload", async () => textResult("Browser reloaded.", await b.reload())),
      }),
      defineTool({
        name: "browser_click", label: "Browser Click",
        description: "Click an element matching a Playwright selector. Selectors: CSS, text=…, role=…, xpath=…",
        promptSnippet: "browser_click: click a Playwright selector.",
        parameters: Type.Object({ selector: Type.String() }),
        executionMode: "sequential",
        execute: wrap("browser_click", async (p) => textResult(`Clicked ${p.selector}.`, await b.click(p.selector))),
      }),
      defineTool({
        name: "browser_fill", label: "Browser Fill",
        description: "Fill an input/textarea with the given value.",
        promptSnippet: "browser_fill: type into a selector.",
        parameters: Type.Object({ selector: Type.String(), value: Type.String() }),
        executionMode: "sequential",
        execute: wrap("browser_fill", async (p) => textResult(`Filled ${p.selector}.`, await b.fill(p.selector, p.value))),
      }),
      defineTool({
        name: "browser_press", label: "Browser Press",
        description: "Press a key inside an element (selector required) or in the page focus (selector empty).",
        promptSnippet: "browser_press: press a key.",
        parameters: Type.Object({ selector: Type.Optional(Type.String()), key: Type.String() }),
        executionMode: "sequential",
        execute: wrap("browser_press", async (p) => textResult(`Pressed ${p.key}.`, await b.press(p.selector ?? "", p.key))),
      }),
      defineTool({
        name: "browser_get_text", label: "Browser Text",
        description: "Return the visible text of an element (selector) or the whole page body (no selector).",
        promptSnippet: "browser_get_text: read page text.",
        parameters: Type.Object({ selector: Type.Optional(Type.String()), max_chars: Type.Optional(Type.Number()) }),
        executionMode: "sequential",
        execute: wrap("browser_get_text", async (p) => {
          const r = await b.getText(p.selector, p.max_chars ?? 50000);
          return textResult(typeof r["text"] === "string" ? `Got ${(r as { length: number }).length} characters.` : "Browser text fetch.", r);
        }),
      }),
      defineTool({
        name: "browser_screenshot", label: "Browser Screenshot",
        description: "Capture a PNG of the current browser page. full_page=true captures the whole scrolled-out page.",
        promptSnippet: "browser_screenshot: capture the browser page.",
        parameters: Type.Object({ full_page: Type.Optional(Type.Boolean()) }),
        executionMode: "sequential",
        execute: wrap("browser_screenshot", async (p) => {
          const r = await b.screenshot(Boolean(p.full_page));
          if (typeof r["error"] === "string") return textResult(String(r["error"]), r);
          const data = String(r["data"] ?? "");
          const path = String(r["path"] ?? "");
          const width = typeof r["width"] === "number" ? r["width"] : 0;
          const height = typeof r["height"] === "number" ? r["height"] : 0;
          return screenshotResult(
            { path, width, height, mimeType: "image/png", data },
            Boolean(options.omitScreenshotImages?.()),
          );
        }),
      }),
      defineTool({
        name: "browser_eval", label: "Browser Eval",
        description: "Evaluate a JavaScript expression in the current page and return its value.",
        promptSnippet: "browser_eval: run JS in the browser.",
        parameters: Type.Object({ expression: Type.String() }),
        executionMode: "sequential",
        execute: wrap("browser_eval", async (p) => textResult(`Evaluated.`, await b.evalJs(p.expression))),
      }),
      defineTool({
        name: "browser_close", label: "Browser Close",
        description: "Shut down the browser instance and free resources.",
        promptSnippet: "browser_close: stop the browser.",
        parameters: Type.Object({}),
        executionMode: "sequential",
        execute: wrap("browser_close", async () => {
          await b.close();
          return textResult("Closed browser.", { closed: true });
        }),
      }),
    );
  }

  // ── desktop_wait_for ────────────────────────────────────────────────
  definitions.push(
    defineTool({
      name: "desktop_wait_for",
      label: "Wait For",
      description:
        "Block until a target becomes observable on the desktop, or the timeout elapses. Use this after desktop_launch / browser_navigate / any action that triggers a slow UI transition, instead of immediately clicking on something that might not be drawn yet. Provide ONE of: window_title (substring), element_name (a11y name), text (visible label via OCR), window_id.",
      promptSnippet: "desktop_wait_for: poll until a window/element/text appears.",
      promptGuidelines: [
        "Always call desktop_wait_for after desktop_launch before clicking inside the new window.",
      ],
      parameters: Type.Object({
        window_title: Type.Optional(Type.String()),
        element_name: Type.Optional(Type.String()),
        text: Type.Optional(Type.String()),
        window_id: Type.Optional(Type.String()),
        timeout: Type.Optional(Type.Number({ description: "Seconds to wait (default 15)." })),
      }),
      executionMode: "sequential",
      execute: wrap("desktop_wait_for", async (params, signal) => {
        const backend = await getBackend();
        const r = await waitFor(backend, {
          windowTitle: params.window_title,
          elementName: params.element_name,
          text: params.text,
          windowId: params.window_id,
          timeout: params.timeout,
        }, signal);
        const msg = r.found
          ? `Target ${r.target} matched after ${r.elapsed}s.`
          : (r.error ?? `Target not found within ${r.timeout}s; tried ${JSON.stringify(r.targets)}.`);
        return textResult(msg, r as unknown as Record<string, unknown>);
      }),
    }),
  );

  // ── Plan / artifact / checkpoint meta-tools ─────────────────────────
  if (options.artifactStore || options.progressStore || options.plan) {
    definitions.push(
      defineTool({
        name: "plan_get",
        label: "Plan: get",
        description: "Return the current typed plan (steps, ids, statuses). Use to remind yourself which step the controller expects you to work on. Returns null when no plan has been initialised this session.",
        promptSnippet: "plan_get: read the current typed plan.",
        parameters: Type.Object({}),
        executionMode: "sequential",
        execute: wrap("plan_get", async () => {
          const plan = options.plan?.value;
          if (!plan) return textResult("No structured plan in this session.", { plan: null });
          return textResult(`Plan revision ${plan.revision}: ${progressSummary(plan)}.`, {
            plan: planToDict(plan),
            rendered: renderForPrompt(plan),
          });
        }),
      }),
      defineTool({
        name: "plan_set",
        label: "Plan: set",
        description: "Initialise or replace the structured plan from a JSON document. The schema matches the typed planner output ({ steps: [{ id, goal, expected, tools_hint?, depends_on? }, ...] }). Use this once at the start of a complex multi-app task so plan_update_step / plan_get can track progress. Calling this again replaces the plan in place.",
        promptSnippet: "plan_set: install a typed plan for this task.",
        parameters: Type.Object({
          plan_json: Type.String({ description: "JSON object with a 'steps' array." }),
        }),
        executionMode: "sequential",
        execute: wrap("plan_set", async (params) => {
          if (!options.plan) return textResult("Plan slot not configured.", { error: "no_plan_slot" });
          const plan = parsePlan(params.plan_json);
          if (!plan.steps.length) {
            return textResult("Plan rejected — no steps parsed.", { error: "empty" });
          }
          options.plan.value = plan;
          if (options.progressStore && options.progressRecord?.value) {
            await options.progressStore.updatePlanSnapshot(options.progressRecord.value, planToDict(plan));
          }
          return textResult(`Plan installed with ${plan.steps.length} steps.`, {
            plan: planToDict(plan),
            rendered: renderForPrompt(plan),
          });
        }),
      }),
      defineTool({
        name: "plan_update_step",
        label: "Plan: update step",
        description: "Mark a plan step done/skipped/blocked, or attach notes. Use this when finishing a step so plan_get reflects progress accurately.",
        promptSnippet: "plan_update_step: change a step's status.",
        parameters: Type.Object({
          id: Type.String(),
          status: Type.Optional(Type.Union([
            Type.Literal("pending"),
            Type.Literal("running"),
            Type.Literal("done"),
            Type.Literal("failed"),
            Type.Literal("skipped"),
            Type.Literal("blocked"),
          ])),
          notes: Type.Optional(Type.String()),
        }),
        executionMode: "sequential",
        execute: wrap("plan_update_step", async (params) => {
          const plan = options.plan?.value;
          if (!plan) return textResult("No plan in this session.", { error: "no_plan" });
          const step = plan.steps.find((s) => s.id === params.id);
          if (!step) return textResult(`No step with id ${params.id}.`, { error: "no_step" });
          if (params.status) step.status = params.status as StepStatus;
          if (params.notes) step.notes = step.notes ? `${step.notes}\n${params.notes}` : params.notes;
          if (options.progressStore && options.progressRecord?.value) {
            if (params.status === "done") {
              await options.progressStore.markDone(options.progressRecord.value, step.id);
            } else if (params.status === "failed" || params.status === "blocked") {
              await options.progressStore.markFailed(options.progressRecord.value, step.id);
            }
            await options.progressStore.updatePlanSnapshot(options.progressRecord.value, planToDict(plan));
          }
          return textResult(`Step ${step.id} updated.`, { step });
        }),
      }),
    );
  }

  if (options.artifactStore) {
    const store = options.artifactStore;
    definitions.push(
      defineTool({
        name: "get_artifact",
        label: "Artifact: get",
        description: "Fetch the body of a previously stored artifact (file content, command output, OCR snippet) by id. Use this when the agent context only shows an artifact summary and you need the full text.",
        promptSnippet: "get_artifact: fetch a stored artifact by id.",
        parameters: Type.Object({
          id: Type.String({ description: "artifact://<id> or the bare id." }),
        }),
        executionMode: "sequential",
        execute: wrap("get_artifact", async (params) => {
          const art = await store.get(params.id);
          if (!art) return textResult(`Unknown artifact id: ${params.id}.`, { error: "unknown_id" });
          const body = (await store.getBody(params.id)) ?? "";
          return textResult(`Artifact ${art.id} [${art.kind}] ${art.bytesLen}B.`, {
            id: art.id,
            kind: art.kind,
            source: art.source,
            summary: art.summary,
            bytes: art.bytesLen,
            content: body,
          });
        }),
      }),
      defineTool({
        name: "list_artifacts",
        label: "Artifact: list",
        description: "List recent artifacts captured during this task. Use to find a previously-read file before re-reading it from disk.",
        promptSnippet: "list_artifacts: enumerate recent stored artifacts.",
        parameters: Type.Object({
          kind: Type.Optional(Type.String()),
          limit: Type.Optional(Type.Number()),
        }),
        executionMode: "sequential",
        execute: wrap("list_artifacts", async (params) => {
          const items = await store.listRecent({ kind: params.kind, limit: params.limit });
          return textResult(`Found ${items.length} artifacts.`, {
            count: items.length,
            artifacts: items.map((a) => ({
              id: a.id, kind: a.kind, source: a.source,
              summary: a.summary, bytes: a.bytesLen,
            })),
          });
        }),
      }),
    );
  }

  if (options.progressStore) {
    const ps = options.progressStore;
    definitions.push(
      defineTool({
        name: "checkpoint",
        label: "Checkpoint",
        description: "Persist a free-form progress marker so the task can resume after a crash or abort. Use after non-trivial milestones (\"finished tab 3 of 7\", \"wrote intermediate output\").",
        promptSnippet: "checkpoint: persist a progress marker.",
        parameters: Type.Object({
          label: Type.Optional(Type.String()),
          data: Type.Optional(Type.Record(Type.String(), Type.Unknown())),
        }),
        executionMode: "sequential",
        execute: wrap("checkpoint", async (params) => {
          const rec = options.progressRecord?.value;
          if (!rec) return textResult("No active progress record.", { saved: false });
          const payload: Record<string, unknown> = { ...(params.data ?? {}) };
          if (params.label) payload.label = params.label;
          await ps.updateCheckpoint(rec, payload);
          return textResult("Checkpoint saved.", { saved: true, checkpoint: payload });
        }),
      }),
    );
  }

  // Diagnostics: classify a failure message — useful when the model is
  // deciding whether to retry-with-wait, replan, or escalate.
  definitions.push(
    defineTool({
      name: "classify_failure",
      label: "Classify failure",
      description: "Classify an error message into one of {transient_io, app_not_ready, missing_element, permission, predicate_not_met, user_input_needed, unknown} and recommend a recovery action. Use after a tool failure when you're unsure whether to retry, wait, or replan.",
      promptSnippet: "classify_failure: get a recovery recommendation for an error string.",
      parameters: Type.Object({
        tool_name: Type.String(),
        error_message: Type.String(),
      }),
      executionMode: "sequential",
      execute: wrap("classify_failure", async (params) => {
        const v = classifyFailure({
          toolName: params.tool_name,
          errorMessage: params.error_message,
        });
        return textResult(`${v.cls} → ${v.action} (${v.reason})`, {
          class: v.cls, action: v.action, wait_seconds: v.waitSeconds, reason: v.reason,
        });
      }),
    }),
  );

  // ── check_predicate ─────────────────────────────────────────────────
  // Lets the model verify a typed post-condition deterministically
  // (window/file/url/text/process/shell) without round-tripping the
  // verdict back through the LLM.
  definitions.push(
    defineTool({
      name: "check_predicate",
      label: "Check predicate",
      description: "Evaluate a typed post-condition. Provide kind plus the relevant arg (value/path/command/stdout_contains). Returns {ok, detail, observed}. Use to verify a step's expected outcome before declaring STEP_DONE.",
      promptSnippet: "check_predicate: verify a typed post-condition.",
      parameters: Type.Object({
        kind: Type.Union([
          Type.Literal("window_title_contains"),
          Type.Literal("window_active_app"),
          Type.Literal("file_exists"),
          Type.Literal("file_absent"),
          Type.Literal("file_contains"),
          Type.Literal("url_contains"),
          Type.Literal("text_visible"),
          Type.Literal("process_running"),
          Type.Literal("shell_returns"),
        ]),
        value: Type.Optional(Type.String()),
        path: Type.Optional(Type.String()),
        command: Type.Optional(Type.String()),
        stdout_contains: Type.Optional(Type.String()),
      }),
      executionMode: "sequential",
      execute: wrap("check_predicate", async (params, signal) => {
        const pred = normalizePredicate(params);
        if (!pred) return textResult("Invalid predicate.", { ok: false, error: "invalid" });
        const backend = await getBackend().catch(() => undefined);
        const helpers: Parameters<typeof checkPredicate>[2] = {};
        if (options.browser) {
          helpers.browserEval = async (expr: string) => {
            const r = await options.browser!.evalJs(expr);
            return (r as { value?: unknown }).value;
          };
        }
        helpers.findText = async (text: string) => {
          const shot = await (await getBackend()).screenshot({ saveDir }, signal);
          const f = await findText(shot.path, text, 0, signal);
          return { found: !!f.found };
        };
        const result = await checkPredicate(pred, backend, helpers);
        return textResult(
          `${result.ok ? "ok" : "FAIL"}: ${renderPredicate(pred)} — ${result.detail}`,
          {
            ok: result.ok,
            kind: result.kind,
            detail: result.detail,
            observed: result.observed,
          },
        );
      }),
    }),
  );

  // ── budget_status ───────────────────────────────────────────────────
  if (options.budget) {
    const tracker = options.budget;
    definitions.push(
      defineTool({
        name: "budget_status",
        label: "Budget",
        description: "Return per-task cost telemetry: tool calls used, elapsed time, fraction of any configured ceiling consumed. Call periodically on long tasks to decide whether to wrap up early.",
        promptSnippet: "budget_status: read the cost-telemetry counters.",
        parameters: Type.Object({}),
        executionMode: "sequential",
        execute: wrap("budget_status", async () => {
          const snap = tracker.snapshot();
          return textResult(
            `${snap.toolCalls} tool calls, ${snap.elapsedSeconds}s elapsed, ` +
            `${(snap.fractionUsed * 100).toFixed(0)}% of any limit used` +
            (snap.exceeded ? " — EXCEEDED" : ""),
            snap as unknown as Record<string, unknown>,
          );
        }),
      }),
    );
  }

  // ── memory_get / memory_note ───────────────────────────────────────
  // memory_get is read-only and registers whenever the store exists,
  // so existing app-memory records remain queryable even when the
  // user has not opted in to creation.  memory_note (which writes new
  // records) is gated behind cfg.memoryEnabled / appMemory.writes,
  // mirroring how skill_save is gated behind skillsEnabled.
  if (options.appMemory) {
    const mem = options.appMemory;
    definitions.push(
      defineTool({
        name: "memory_get",
        label: "Memory: get",
        description: "Read the per-app memory record (failure histogram, success counts, recent notes). Pass an empty app to list every recorded app. Always available — reads are not gated by memoryEnabled.",
        promptSnippet: "memory_get: inspect what worked / failed for an app.",
        parameters: Type.Object({
          app: Type.Optional(Type.String()),
        }),
        executionMode: "sequential",
        execute: wrap("memory_get", async (params) => {
          if (!params.app) {
            const apps = await mem.listApps();
            return textResult(`Memory has ${apps.length} app(s).`, { apps });
          }
          const rec = await mem.get(params.app);
          return textResult(`Loaded memory for ${rec.app}.`,
                            rec as unknown as Record<string, unknown>);
        }),
      }),
    );
    if (mem.writes) {
      definitions.push(
        defineTool({
          name: "memory_note",
          label: "Memory: note",
          description: "Attach a free-form note (\"input box doesn't respond to ctrl+a\") to an app's memory record so future tasks see the warning.",
          promptSnippet: "memory_note: persist a per-app warning.",
          parameters: Type.Object({
            app: Type.String(),
            text: Type.String(),
            tag: Type.Optional(Type.String()),
          }),
          executionMode: "sequential",
          execute: wrap("memory_note", async (params) => {
            await mem.addNote({ app: params.app, text: params.text, tag: params.tag });
            return textResult(`Note saved for ${params.app}.`, { saved: true });
          }),
        }),
      );
    }
  }

  // ── preflight ──────────────────────────────────────────────────────
  // Always available — useful even when no plan is installed (the
  // model can run a one-off resource check before launching a flow).
  definitions.push(
    defineTool({
      name: "preflight",
      label: "Preflight",
      description: "Verify that resources are available before touching any UI: app on PATH, file present, URL reachable, tool registered, command exits 0. Pass an explicit list of checks; when omitted, derives checks from the active plan's tools_hint and predicates.",
      promptSnippet: "preflight: verify resources before acting.",
      parameters: Type.Object({
        checks: Type.Optional(Type.Array(Type.Object({
          kind: Type.Union([
            Type.Literal("app"),
            Type.Literal("file"),
            Type.Literal("url"),
            Type.Literal("tool"),
            Type.Literal("command"),
          ]),
          target: Type.String(),
          note: Type.Optional(Type.String()),
        }))),
      }),
      executionMode: "sequential",
      execute: wrap("preflight", async (params) => {
        const registered = new Set(definitions.map((d) => d.name));
        const explicit = (params.checks ?? []) as PreflightCheck[];
        const fromPlan = options.plan?.value
          ? inferChecksFromPlan(planToDict(options.plan.value), registered)
          : [];
        const checks = [...explicit, ...fromPlan];
        if (!checks.length) {
          return textResult("No preflight checks supplied.", { results: [], allPassed: true });
        }
        const report = await runPreflight(checks, {
          registeredTools: registered,
          // shell helper omitted; preflight `command` checks simply fail
          // with "shell unavailable" inside Pi (they aren't widely
          // used here, and Pi's own shell tools are out of our scope).
        });
        const summary = report.allPassed
          ? `Preflight passed (${report.results.length} checks).`
          : `Preflight FAILED: ${report.results.filter((r) => !r.ok).length}/${report.results.length} missing.`;
        return textResult(summary, report as unknown as Record<string, unknown>);
      }),
    }),
  );

  return definitions;
}

function defaultOptions(): DesktopToolOptions {
  throw new Error("createDesktopTools requires options");
}

function normalizeRect(rect: Rect): Rect {
  return { x: Math.round(rect.x), y: Math.round(rect.y), width: Math.max(1, Math.round(rect.width)), height: Math.max(1, Math.round(rect.height)) };
}

/**
 * Replay one previously-recorded SkillStep against the live backend.  The
 * skill format mirrors mainline so a recipe captured on either side can
 * be played back on either side.
 */
async function replayStep(backend: DesktopBackend, _opts: DesktopToolOptions, step: SkillStep, signal?: AbortSignal): Promise<void> {
  const a = step.args as Record<string, unknown>;
  switch (step.tool) {
    case "desktop_click":
      await backend.click(num(a, "x"), num(a, "y"), str(a, "button", "left") as "left" | "right" | "middle", num(a, "clicks", 1), signal);
      return;
    case "desktop_type":
      await backend.typeText(str(a, "text"), signal);
      return;
    case "desktop_hotkey":
      await backend.hotkey((a["keys"] as string[]) ?? [], signal);
      return;
    case "desktop_scroll":
      await backend.scroll(num(a, "x", 0), num(a, "y", 0), num(a, "clicks", 3), (a["direction"] as "up" | "down") ?? "down", signal);
      return;
    case "desktop_focus_window":
      await backend.focusWindow({
        id: a["id"] as string | undefined,
        title: a["title"] as string | undefined,
        pid: a["pid"] as number | undefined,
        app: a["app"] as string | undefined,
      }, signal);
      return;
    case "desktop_launch":
      await backend.launch(str(a, "application"), (a["args"] as string[]) ?? [], signal);
      return;
    case "desktop_mouse_move":
      await backend.mouseMove(num(a, "dx"), num(a, "dy"), Boolean(a["click"]), signal);
      return;
    default:
      throw new Error(`replay: tool ${step.tool} is not replayable`);
  }
}

/**
 * If a tool returned a long string body in its details (file content, page
 * text, command stdout), store it in the artifact store and replace the
 * inline body in the details object with a short preview and the artifact
 * id.  Mutates ``result.details`` in place.
 */
async function maybeCaptureArtifact(
  toolName: string,
  params: Record<string, unknown>,
  result: { content: Array<{ type: string; text?: string; data?: string; mimeType?: string }>; details: Record<string, unknown> },
  store: ArtifactStore,
): Promise<void> {
  let bodyKey: string | undefined;
  let source = "";
  switch (toolName) {
    case "fs_read":
      bodyKey = "content";
      source = String(params["path"] ?? "");
      break;
    case "desktop_get_window_text":
      bodyKey = "text";
      break;
    case "browser_get_text":
      bodyKey = "text";
      source = String(params["selector"] ?? "page");
      break;
    case "shell_run": {
      const stdout = result.details["stdout"];
      if (typeof stdout === "string" && stdout.length > 4096) {
        const captured = await store.maybeCapture(stdout, {
          kind: "shell_stdout",
          source: String(params["command"] ?? "").slice(0, 120),
        });
        if (captured) {
          result.details["stdout"] = stdout.slice(0, 400) + `\n...\n[stored as ${captured.id}]`;
          result.details["stdout_artifact_id"] = captured.id;
        }
      }
      return;
    }
    default:
      return;
  }
  if (!bodyKey) return;
  const body = result.details[bodyKey];
  if (typeof body !== "string" || body.length <= 4096) return;
  const captured = await store.maybeCapture(body, { kind: toolName, source });
  if (captured) {
    result.details[bodyKey] = captured.preview;
    result.details[bodyKey + "_artifact_id"] = captured.id;
  }
}

function num(o: Record<string, unknown>, k: string, fallback?: number): number {
  const v = o[k];
  if (typeof v === "number") return v;
  if (fallback !== undefined) return fallback;
  throw new Error(`Missing numeric arg ${k}`);
}
function str(o: Record<string, unknown>, k: string, fallback?: string): string {
  const v = o[k];
  if (typeof v === "string") return v;
  if (fallback !== undefined) return fallback;
  throw new Error(`Missing string arg ${k}`);
}
