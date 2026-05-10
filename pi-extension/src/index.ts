import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";
import { ArtifactStore } from "./artifacts.js";
import { BrowserBackend } from "./browser_backend.js";
import { PerceptionCache } from "./cache.js";
import { loadConfig, type ExtensionConfig } from "./config.js";
import type { Plan } from "./controller.js";
import { ProgressStore, type TaskProgress } from "./progress.js";
import { runInstaller } from "./install_runner.js";
import { createLogger } from "./logger.js";
import { createBackend } from "./platform.js";
import { commandExists, execFile, shellQuote } from "./process.js";
import { ScreenRecorder } from "./screen_record.js";
import { SkillStore, type SkillStep } from "./skills.js";
import { createDesktopTools } from "./tools.js";
import { TraceWriter } from "./trace.js";
import type { DesktopBackend, Mark } from "./types.js";

const extensionRoot = dirname(dirname(fileURLToPath(import.meta.url)));
const extensionEntry = fileURLToPath(import.meta.url);
const screenshotDir = join(extensionRoot, "runtime", "screenshots");
const logger = createLogger(extensionRoot);
const SCREENSHOT_DEGRADE_AFTER_PROVIDER_ERRORS = 3;

/**
 * Build the AutoGUI system prompt the `/autogui` command injects into Pi.
 * The prompt is dynamic now: it advertises whichever extra tools are
 * available (browser, click_element, click_text), and includes the
 * planner instructions when the planner is enabled in config.
 */
function buildAutoGuiPrompt(cfg: ExtensionConfig, backend: DesktopBackend | undefined): string {
  const browserOn = cfg.allowedBrowser;
  const a11yOn = backend?.capabilities?.findElement === true;
  const planner = cfg.plannerEnabled;
  const controllerOn = cfg.controllerEnabled;
  const artifactsOn = !!cfg.artifactsDir;
  const progressOn = !!cfg.progressDir;
  return `You are using the AutoGUI desktop automation extension.

Goal: complete the user's desktop task using Pi's normal agent workflow and the registered desktop_*${browserOn ? "/browser_*" : ""} tools.

${controllerOn ? `Typed-plan protocol (REQUIRED):
- Your FIRST tool call must be plan_set with a JSON plan whose schema is:
    { "steps": [
        { "id": "s1", "goal": "...", "expected": "<observable post-condition>",
          "tools_hint": ["browser_navigate"], "depends_on": [] },
        ...
    ] }
- 3 to 8 steps; each maps to ONE observable post-condition.
- After each step finishes, call plan_update_step(id, status="done") so progress is persisted.
- If a step is blocked, call plan_update_step(id, status="blocked", notes="why"), then revise the plan with another plan_set call.
- Use plan_get when you need to see current statuses.
- For long-running tasks, call checkpoint(label, data) after non-trivial milestones so a crash / abort can resume from the marker.
- Use desktop_wait_for after desktop_launch / browser_navigate / any action that triggers a slow UI transition. Never click a window that may not be drawn yet.
- For pure read-only lookups (\"which file mentions X\", \"summarise this stdout\"), prefer get_artifact / list_artifacts to keep history small.
- Classify any persistent failure with classify_failure to decide between retry / wait / replan / escalate.

` : (planner ? `Planning protocol:
- BEFORE taking any state-changing action, your FIRST assistant message must be a numbered plan of 3 to 8 high-level steps that will accomplish the task.
- Steps describe goals ("open Edge", "navigate to weather page"), not specific clicks.
- Then execute the plan step by step. After each step, verify with desktop_screenshot, desktop_list_windows, or desktop_active_window.
- Adapt the plan if the screen state diverges from what a step expects.
- For trivial single-step tasks, the plan may be a single line.

` : "")}${artifactsOn ? `Large observations (file bodies, page text, command stdout > 4KB) are auto-stored as artifacts.  The tool result will contain a preview plus an `+"`artifact_id`"+` you can fetch later with get_artifact.  Use list_artifacts before re-reading a file you already read.

` : ""}${progressOn ? `This task has a persistent progress record. Re-running the same task text will resume — call plan_get on session start to see what was already done.

` : ""}Click ladder — pick the strongest available method for each click:
1. ${browserOn ? "browser_click for any element on a web page (most reliable for web).\n2. " : ""}desktop_click_element for any native UI control with a visible name/label (uses the OS accessibility API; survives DPI scaling, window moves, async redraws). ${a11yOn ? "" : "(Limited support on the current backend.)\n"}
${a11yOn ? `${browserOn ? "3" : "2"}. ` : `${browserOn ? "2" : "1"}. `}desktop_click_text — find a visible label via OCR and click it (slower but no a11y dependency).
${a11yOn ? `${browserOn ? "4" : "3"}. ` : `${browserOn ? "3" : "2"}. `}desktop_screenshot_marked + desktop_click_mark — Set-of-Mark grounding when you need to point at something without a clean name.
${a11yOn ? `${browserOn ? "5" : "4"}. ` : `${browserOn ? "4" : "3"}. `}desktop_click(x, y) — last resort, only when none of the above can identify the target.

Rules:
- Inspect current desktop state first with desktop_list_windows, desktop_active_window, or desktop_screenshot.
- When screenshots are unavailable, degraded, or insufficient, use desktop_list_windows and desktop_active_window/desktop_focus_window instead of guessing.
- Before typing, focus the target window/control with desktop_focus_window or desktop_click_element, then verify with desktop_active_window.
- Do not use alt+space, application menus, or window menus as a focus strategy.
- After launching apps or making visible changes, verify with desktop_list_windows or desktop_screenshot.
- Keep using Pi's built-in coding/filesystem tools for code and file work; use desktop_*${browserOn ? "/browser_*" : ""} tools only for desktop UI automation.
- If a desktop tool reports a missing permission or dependency, tell the user exactly what is missing and stop that desktop action.
- When you finish a task that worked well, consider calling skill_save with descriptive keywords so the procedure can be replayed via skill_run later.
${browserOn
  ? "- For any task involving a web page or URL, prefer browser_navigate and browser_* tools over launching a browser via desktop_launch.\n- browser_navigate(url) navigates the Playwright-managed Chromium page in place; it does not open the user's existing browser window.\n"
  : "- Browser tools are NOT enabled. Do NOT attempt desktop_launch with browser names (Edge, Chrome, Firefox, etc.) — those paths are unreliable. If a web task is requested, tell the user to set allowedBrowser=true in pi-extension/config.json and re-run the install script to set up Playwright + Chromium.\n"}`;
}

export default function autoGuiExtension(pi: ExtensionAPI) {
  let backendPromise: Promise<DesktopBackend> | undefined;
  let cfgPromise: Promise<ExtensionConfig> | undefined;
  let autoGuiActive = false;
  let autoGuiTask = "";
  let lastRetryableProviderStatus: number | undefined;
  let pendingRetryableProviderError: { status: number; errorMessage: string } | undefined;
  let retryAttempt = 0;
  let retryTimer: ReturnType<typeof setTimeout> | undefined;
  let screenshotProviderFailureCount = 0;
  let omitScreenshotImages = false;

  // Per-session state for skills + Set-of-Mark + trace.
  const sessionSteps: SkillStep[] = [];
  const lastMarks: { value: Mark[] } = { value: [] };
  // Mutable holders so tools.ts can read/write the current plan +
  // progress record over the lifetime of an /autogui task.
  const planSlot: { value: Plan | undefined } = { value: undefined };
  const progressSlot: { value: TaskProgress | undefined } = { value: undefined };

  const getConfig = async () => {
    cfgPromise ??= loadConfig(extensionRoot);
    return await cfgPromise;
  };

  const getBackend = async () => {
    backendPromise ??= createBackend(logger);
    return await backendPromise;
  };

  // Build singletons that depend on the loaded config.  `init` is awaited at
  // session_start; the tools are registered eagerly with placeholders and
  // wired up after `init` resolves.
  const cache = new PerceptionCache();
  let skillStore: SkillStore | undefined;
  let trace: TraceWriter | undefined;
  let recorder: ScreenRecorder | undefined;
  let browser: BrowserBackend | undefined;
  let cfg: ExtensionConfig | undefined;
  let artifactStore: ArtifactStore | undefined;
  let progressStore: ProgressStore | undefined;

  const init = (async () => {
    cfg = await getConfig();
    omitScreenshotImages = !cfg.visionEnabled;
    cache.configure(cfg.perceptionCacheTtlMs);
    skillStore = new SkillStore(cfg.skillsPath);
    if (cfg.artifactsDir) {
      artifactStore = new ArtifactStore(cfg.artifactsDir);
    }
    if (cfg.progressDir) {
      progressStore = new ProgressStore(cfg.progressDir);
    }
    if (cfg.recordTrace) {
      trace = new TraceWriter(cfg.traceDir);
      void trace.writeMeta({ event: "session_start" });
    } else {
      // Stub writer so the rest of the code can call .writeEvent without checks.
      trace = new TraceWriter(cfg.traceDir);
      // Block writes by emptying path — simpler to just create one anyway since
      // writes are best-effort and skip on error.
    }
    // Optional one-shot dependency install — invokes the same scripts/
    // shell scripts the user can run by hand.  Runs BEFORE BrowserBackend
    // construction so Playwright + Chromium can be installed first.
    if (cfg.installDependencies) {
      const result = await runInstaller(logger);
      if (!result.ok) {
        await logger.log("init.install_runner.failed", { result });
      }
    }
    if (cfg.allowedBrowser) {
      browser = new BrowserBackend({
        headless: cfg.browser.headless,
        screenshotDir: cfg.browser.screenshotDir,
        userDataDir: cfg.browser.userDataDir,
        viewport: cfg.browser.viewport,
      });
      // BrowserBackend lazily imports playwright on first use; if it's
      // missing the user gets a clear "please install" error pointing
      // at scripts/install-dependencies.*.
    }
    if (cfg.screenRecord.enabled) {
      recorder = new ScreenRecorder(cfg.screenRecord, getBackend, logger);
      void recorder.start();
    }
  })();

  const clearRetryTimer = () => {
    if (retryTimer) { clearTimeout(retryTimer); retryTimer = undefined; }
  };

  const resetAutoGuiState = () => {
    autoGuiActive = false;
    autoGuiTask = "";
    lastRetryableProviderStatus = undefined;
    pendingRetryableProviderError = undefined;
    retryAttempt = 0;
    screenshotProviderFailureCount = 0;
    omitScreenshotImages = false;
    clearRetryTimer();
  };

  const detectRetryableStatus = (errorMessage: string, fallback?: number): number | undefined => {
    if (fallback === 404 || fallback === 429) return fallback;
    if (/\b429\b|rate.?limit|too many requests/i.test(errorMessage)) return 429;
    if (/\b404\b|no endpoints? (?:found|available)|no endpoints available matching/i.test(errorMessage)) return 404;
    return undefined;
  };

  const stripScreenshotImages = (messages: unknown[]): unknown[] => messages.map((message) => {
    if (!message || typeof message !== "object" || (message as { role?: unknown }).role !== "toolResult") return message;
    const toolResult = message as { toolName?: unknown; content?: unknown; details?: unknown };
    if (toolResult.toolName !== "desktop_screenshot" || !Array.isArray(toolResult.content)) return message;
    const contentWithoutImages = toolResult.content.filter((part) => !part || typeof part !== "object" || (part as { type?: unknown }).type !== "image");
    if (contentWithoutImages.length === toolResult.content.length) return message;
    const details = toolResult.details && typeof toolResult.details === "object"
      ? { ...toolResult.details as Record<string, unknown>, imageOmitted: true, omittedReason: "AutoGUI screenshot degrade mode" }
      : { imageOmitted: true, omittedReason: "AutoGUI screenshot degrade mode" };
    return { ...toolResult, content: contentWithoutImages, details };
  });

  const scheduleProviderRetry = async (ctx: ExtensionContext) => {
    if (!autoGuiActive || !pendingRetryableProviderError || retryTimer) return;
    const delayMs = Math.min(60000, 5000 * 2 ** retryAttempt);
    const attempt = retryAttempt + 1;
    retryAttempt = attempt;
    const providerError = pendingRetryableProviderError;
    await logger.log("provider.autogui_retry_scheduled", {
      attempt, delayMs, status: providerError.status, errorMessage: providerError.errorMessage,
    });
    ctx.ui.notify(`AutoGUI provider retry ${attempt} scheduled in ${Math.ceil(delayMs / 1000)}s. Use /autogui-abort to stop.`, "warning");

    const dispatchRetry = async () => {
      retryTimer = undefined;
      if (!autoGuiActive || !pendingRetryableProviderError) return;
      if (!ctx.isIdle()) {
        retryTimer = setTimeout(() => { void dispatchRetry(); }, 1000);
        return;
      }
      const backend = await getBackend().catch(() => undefined);
      const c = await getConfig();
      const retryMessage = `${buildAutoGuiPrompt(c, backend)}

AutoGUI provider retry:
- The previous provider request failed with temporary status ${providerError.status}.
- Continue the same desktop task. Do not restart completed work.
- Inspect the current desktop state before the next action.
- If the provider fails again with 404 or 429, AutoGUI will keep retrying until the user runs /autogui-abort.
${omitScreenshotImages ? "- Screenshot inline images are temporarily disabled. Use desktop_list_windows, desktop_active_window, desktop_focus_window, and screenshot file paths instead of image inspection." : ""}

Original user desktop task:
${autoGuiTask}`;

      void logger.log("provider.autogui_retry_dispatch", { attempt, status: providerError.status, task: autoGuiTask });
      pi.sendUserMessage(retryMessage);
    };

    retryTimer = setTimeout(() => { void dispatchRetry(); }, delayMs);
  };

  // Tool registration is deferred until after init resolves so the tool
  // closures see the real config / skill store / browser.
  void init.then(async () => {
    if (!cfg) return;
    if (!skillStore) skillStore = new SkillStore(cfg.skillsPath);
    if (!trace) trace = new TraceWriter(cfg.traceDir);
    const tools = createDesktopTools(getBackend, screenshotDir, logger, {
      omitScreenshotImages: () => omitScreenshotImages,
      config: cfg,
      cache,
      skillStore,
      trace,
      recorder,
      browser,
      sessionSteps,
      lastMarks,
      artifactStore,
      progressStore,
      plan: planSlot,
      progressRecord: progressSlot,
    });
    for (const tool of tools) {
      pi.registerTool(tool);
    }
  }).catch(async (err) => {
    await logger.log("init.failed", { error: String(err) });
  });

  pi.registerCommand("autogui", {
    description: "Start a desktop automation task with AutoGUI tools",
    handler: async (args, ctx) => {
      const task = args.trim();
      await logger.log("command.autogui", { task });
      if (!task) {
        ctx.ui.notify("Usage: /autogui <desktop task>", "warning");
        return;
      }
      clearRetryTimer();
      autoGuiActive = true;
      autoGuiTask = task;
      lastRetryableProviderStatus = undefined;
      pendingRetryableProviderError = undefined;
      retryAttempt = 0;
      screenshotProviderFailureCount = 0;
      omitScreenshotImages = false;
      // Reset per-task session state so skill_save snapshots only this task.
      sessionSteps.length = 0;
      lastMarks.value = [];
      planSlot.value = undefined;
      // Open (or resume) a progress record for this task so progress
      // markers + plan snapshots survive crashes and aborts.
      if (progressStore) {
        try {
          progressSlot.value = await progressStore.openTask(task);
        } catch {
          progressSlot.value = undefined;
        }
      }

      const c = await getConfig();
      let backend: DesktopBackend | undefined;
      try { backend = await getBackend(); } catch { /* prompt builds without backend caps */ }
      let prompt = buildAutoGuiPrompt(c, backend);

      // Skill suggestion: top-3 candidate skills whose keywords match the task.
      try {
        const candidates = (await (skillStore ?? new SkillStore(c.skillsPath)).search(task, 3));
        if (candidates.length) {
          const lines = candidates.map((s) => `- ${JSON.stringify(s.name)} (app=${s.app || "?"}, steps=${s.steps.length}, successes=${s.success_count})`);
          prompt += `\n\nCandidate saved skills (call skill_run if one matches):\n${lines.join("\n")}`;
        }
      } catch { /* best-effort */ }

      const message = `${prompt}\n\nUser desktop task:\n${task}`;
      if (ctx.isIdle()) {
        pi.sendUserMessage(message);
      } else {
        pi.sendUserMessage(message, { deliverAs: "followUp" });
        ctx.ui.notify("AutoGUI task queued as a follow-up.", "info");
      }
    },
  });

  pi.registerCommand("autogui-abort", {
    description: "Abort the current AutoGUI desktop automation task",
    handler: async (_args, ctx) => {
      resetAutoGuiState();
      ctx.abort();
      await logger.log("command.autogui-abort", { wasIdle: ctx.isIdle(), hadPendingMessages: ctx.hasPendingMessages() });
      ctx.ui.notify("AutoGUI automation aborted.", "warning");
    },
  });

  pi.registerCommand("desktop-status", {
    description: "Show AutoGUI desktop backend status",
    handler: async (_args, ctx) => {
      try {
        await logger.log("command.desktop-status");
        const c = await getConfig();
        const backend = await getBackend();
        const status = await backend.status(ctx.signal);
        ctx.ui.notify(JSON.stringify({ ...status, capabilities: backend.capabilities, config: { allowedBrowser: c.allowedBrowser, dryRun: c.dryRun, plannerEnabled: c.plannerEnabled, installDependencies: c.installDependencies } }, null, 2), "info");
      } catch (error) {
        ctx.ui.notify(`AutoGUI status failed: ${String(error)}`, "error");
      }
    },
  });

  pi.registerCommand("autogui-validate", {
    description: "Spawn a read-only AutoGUI validator Pi in tmux",
    handler: async (args, ctx) => {
      const task = args.trim();
      await logger.log("command.autogui-validate", { task });
      if (!task) {
        ctx.ui.notify("Usage: /autogui-validate <task or expected desktop state>", "warning");
        return;
      }
      if (!await commandExists("tmux", ctx.signal)) {
        ctx.ui.notify("tmux is not installed or not on PATH; cannot spawn validator.", "error");
        return;
      }

      const sessionName = `autogui-validator-${Date.now()}`;
      const c = await getConfig();
      let backend: DesktopBackend | undefined;
      try { backend = await getBackend(); } catch { /* fall through */ }
      const validatorPrompt = `${buildAutoGuiPrompt(c, backend)}

Validator mode:
- You are a read-only validator running in a separate Pi process.
- Do not click, type, launch apps, move the mouse, scroll, edit files, or run shell commands.
- Use only desktop_screenshot, desktop_list_windows, and desktop_active_window.
- Report whether the current desktop state appears consistent with the requested task.

Task or expected state to validate:
${task}`;

      const command = [
        "cd", shellQuote(ctx.cwd), "&&",
        "pi",
        "--no-session",
        "--no-builtin-tools",
        "--tools", shellQuote("desktop_screenshot,desktop_list_windows,desktop_active_window"),
        "-e", shellQuote(extensionEntry),
        "-p", shellQuote(validatorPrompt),
        ";",
        "printf", shellQuote("\\nValidator finished. Press Enter to close this tmux pane.\\n"),
        ";",
        "read", "-r", "_",
      ].join(" ");

      const result = await execFile("tmux", ["new-session", "-d", "-s", sessionName, command], { timeoutMs: 5000, signal: ctx.signal });
      await logger.log("command.autogui-validate.spawn", { sessionName, code: result.code, stdout: result.stdout, stderr: result.stderr, timedOut: result.timedOut });
      if (result.code !== 0) {
        ctx.ui.notify(`Failed to spawn tmux validator: ${result.stderr || result.stdout}`, "error");
        return;
      }
      ctx.ui.notify(`Spawned validator in tmux session: ${sessionName}`, "info");
    },
  });

  pi.on("before_provider_request", async (event) => {
    await logger.log("provider.request", { payloadPreview: JSON.stringify(event.payload).slice(0, 4000) });
  });

  pi.on("after_provider_response", async (event) => {
    const retryable = event.status === 429 || event.status === 404;
    if (autoGuiActive && retryable) lastRetryableProviderStatus = event.status;
    await logger.log("provider.response", {
      status: event.status, retryable, headers: event.headers,
      note: retryable ? "AutoGUI tags 404/429 provider errors as retryable at message_end." : undefined,
    });
  });

  pi.on("context", async (event) => {
    if (!autoGuiActive || !omitScreenshotImages) return;
    const messages = stripScreenshotImages(event.messages) as typeof event.messages;
    await logger.log("context.screenshot_images_stripped", { messageCount: messages.length });
    return { messages };
  });

  pi.on("message_end", async (event) => {
    if (autoGuiActive && event.message.role === "assistant" && (event.message.stopReason === "stop" || event.message.stopReason === "aborted")) {
      resetAutoGuiState();
      return;
    }
    if (!autoGuiActive || event.message.role !== "assistant" || event.message.stopReason !== "error") return;

    const errorMessage = event.message.errorMessage ?? "";
    const status = detectRetryableStatus(errorMessage, lastRetryableProviderStatus);
    lastRetryableProviderStatus = undefined;
    if (!status) return;

    screenshotProviderFailureCount++;
    if (!omitScreenshotImages && screenshotProviderFailureCount >= SCREENSHOT_DEGRADE_AFTER_PROVIDER_ERRORS) {
      omitScreenshotImages = true;
      await logger.log("provider.screenshot_degrade_enabled", { threshold: SCREENSHOT_DEGRADE_AFTER_PROVIDER_ERRORS, screenshotProviderFailureCount, status, errorMessage });
    }

    pendingRetryableProviderError = { status, errorMessage };
    await logger.log("provider.retryable_error_tagged", { status, errorMessage, screenshotProviderFailureCount, omitScreenshotImages });

    return {
      message: {
        ...event.message,
        errorMessage: `Provider returned error ${status}; retryable by AutoGUI. ${omitScreenshotImages ? "Screenshot inline images are disabled for subsequent retries. " : ""}${errorMessage}`,
      },
    };
  });

  pi.on("agent_end", async (_event, ctx) => {
    await scheduleProviderRetry(ctx);
  });

  pi.on("session_start", async (_event, ctx) => {
    try {
      await init;
      await logger.log("session_start");
      const backend = await getBackend();
      const status = await backend.status(ctx.signal);
      const c = cfg ?? await getConfig();
      ctx.ui.setStatus("autogui", `AutoGUI: ${backend.name}`);
      ctx.ui.setWidget("autogui", [
        `AutoGUI desktop backend: ${backend.name}`,
        `Platform: ${backend.platform.summary}`,
        `Capabilities: ${JSON.stringify(backend.capabilities ?? {})}`,
        `Status: ${JSON.stringify(status)}`,
        `Browser: ${c.allowedBrowser ? (browser ? "ready" : "not installed") : "disabled"}`,
        `Recorder: ${recorder ? "running" : "off"}`,
        `Planner: ${c.plannerEnabled ? "on" : "off"}, dry-run: ${c.dryRun ? "on" : "off"}`,
      ]);
    } catch (error) {
      await logger.log("session_start.failed", { error: String(error) });
      ctx.ui.setStatus("autogui", "AutoGUI: unavailable");
      ctx.ui.setWidget("autogui", [`AutoGUI unavailable: ${String(error)}`]);
    }
  });
}
