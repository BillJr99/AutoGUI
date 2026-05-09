import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";
import { createLogger } from "./logger.js";
import { createBackend } from "./platform.js";
import { commandExists, execFile, shellQuote } from "./process.js";
import { createDesktopTools } from "./tools.js";
import type { DesktopBackend } from "./types.js";

const extensionRoot = dirname(dirname(fileURLToPath(import.meta.url)));
const extensionEntry = fileURLToPath(import.meta.url);
const screenshotDir = join(extensionRoot, "runtime", "screenshots");
const logger = createLogger(extensionRoot);
const SCREENSHOT_DEGRADE_AFTER_PROVIDER_ERRORS = 3;

const AUTOGUI_PROMPT = `You are using the AutoGUI desktop automation extension.

Goal: complete the user's desktop task using Pi's normal agent workflow and the registered desktop_* tools.

Rules:
- Inspect current desktop state first with desktop_list_windows or desktop_screenshot.
- When screenshots are unavailable, degraded, or insufficient, use desktop_list_windows, desktop_active_window, and desktop_focus_window instead of guessing.
- Prefer window bounds and current screenshots over guessed coordinates.
- Before typing, focus the target window/control with desktop_focus_window when possible, then verify with desktop_active_window.
- Do not use alt+space, application menus, or window menus as a focus strategy.
- After launching apps or making visible changes, verify with desktop_list_windows or desktop_screenshot.
- Keep using Pi's built-in coding/filesystem tools for code and file work; use desktop_* tools only for desktop UI automation.
- If a desktop tool reports a missing permission or dependency, tell the user exactly what is missing and stop that desktop action.
`;

export default function autoGuiExtension(pi: ExtensionAPI) {
  let backendPromise: Promise<DesktopBackend> | undefined;
  let autoGuiActive = false;
  let autoGuiTask = "";
  let lastRetryableProviderStatus: number | undefined;
  let pendingRetryableProviderError: { status: number; errorMessage: string } | undefined;
  let retryAttempt = 0;
  let retryTimer: ReturnType<typeof setTimeout> | undefined;
  let screenshotProviderFailureCount = 0;
  let omitScreenshotImages = false;

  const getBackend = async () => {
    backendPromise ??= createBackend(logger);
    return await backendPromise;
  };

  const clearRetryTimer = () => {
    if (retryTimer) {
      clearTimeout(retryTimer);
      retryTimer = undefined;
    }
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
      attempt,
      delayMs,
      status: providerError.status,
      errorMessage: providerError.errorMessage,
    });
    ctx.ui.notify(`AutoGUI provider retry ${attempt} scheduled in ${Math.ceil(delayMs / 1000)}s. Use /autogui-abort to stop.`, "warning");

    const dispatchRetry = () => {
      retryTimer = undefined;
      if (!autoGuiActive || !pendingRetryableProviderError) return;
      if (!ctx.isIdle()) {
        retryTimer = setTimeout(dispatchRetry, 1000);
        return;
      }

      const retryMessage = `${AUTOGUI_PROMPT}

AutoGUI provider retry:
- The previous provider request failed with temporary status ${providerError.status}.
- Continue the same desktop task. Do not restart completed work.
- Inspect the current desktop state before the next action.
- If the provider fails again with 404 or 429, AutoGUI will keep retrying until the user runs /autogui-abort.
${omitScreenshotImages ? "- Screenshot inline images are temporarily disabled. Use desktop_list_windows, desktop_active_window, desktop_focus_window, and screenshot file paths instead of image inspection." : ""}

Original user desktop task:
${autoGuiTask}`;

      void logger.log("provider.autogui_retry_dispatch", {
        attempt,
        status: providerError.status,
        task: autoGuiTask,
      });
      pi.sendUserMessage(retryMessage);
    };

    retryTimer = setTimeout(dispatchRetry, delayMs);
  };

  for (const tool of createDesktopTools(getBackend, screenshotDir, logger, { omitScreenshotImages: () => omitScreenshotImages })) {
    pi.registerTool(tool);
  }

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
      const message = `${AUTOGUI_PROMPT}\n\nUser desktop task:\n${task}`;
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
      await logger.log("command.autogui-abort", {
        wasIdle: ctx.isIdle(),
        hadPendingMessages: ctx.hasPendingMessages(),
      });
      ctx.ui.notify("AutoGUI automation aborted.", "warning");
    },
  });

  pi.registerCommand("desktop-status", {
    description: "Show AutoGUI desktop backend status",
    handler: async (_args, ctx) => {
      try {
        await logger.log("command.desktop-status");
        const backend = await getBackend();
        const status = await backend.status(ctx.signal);
        ctx.ui.notify(JSON.stringify(status, null, 2), "info");
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
      const validatorPrompt = `${AUTOGUI_PROMPT}

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
    await logger.log("provider.request", {
      payloadPreview: JSON.stringify(event.payload).slice(0, 4000),
    });
  });

  pi.on("after_provider_response", async (event) => {
    const retryable = event.status === 429 || event.status === 404;
    if (autoGuiActive && retryable) {
      lastRetryableProviderStatus = event.status;
    }
    await logger.log("provider.response", {
      status: event.status,
      retryable,
      headers: event.headers,
      note: retryable
        ? "AutoGUI tags 404/429 provider errors as retryable at message_end so Pi core can apply its retry/backoff path."
        : undefined,
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

    if (!autoGuiActive || event.message.role !== "assistant" || event.message.stopReason !== "error") {
      return;
    }

    const errorMessage = event.message.errorMessage ?? "";
    const status = detectRetryableStatus(errorMessage, lastRetryableProviderStatus);
    lastRetryableProviderStatus = undefined;
    if (!status) return;

    screenshotProviderFailureCount++;
    if (!omitScreenshotImages && screenshotProviderFailureCount >= SCREENSHOT_DEGRADE_AFTER_PROVIDER_ERRORS) {
      omitScreenshotImages = true;
      await logger.log("provider.screenshot_degrade_enabled", {
        threshold: SCREENSHOT_DEGRADE_AFTER_PROVIDER_ERRORS,
        screenshotProviderFailureCount,
        status,
        errorMessage,
      });
    }

    pendingRetryableProviderError = { status, errorMessage };
    await logger.log("provider.retryable_error_tagged", {
      status,
      errorMessage,
      screenshotProviderFailureCount,
      omitScreenshotImages,
    });

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
      await logger.log("session_start");
      const backend = await getBackend();
      const status = await backend.status(ctx.signal);
      ctx.ui.setStatus("autogui", `AutoGUI: ${backend.name}`);
      ctx.ui.setWidget("autogui", [
        `AutoGUI desktop backend: ${backend.name}`,
        `Platform: ${backend.platform.summary}`,
        `Status: ${JSON.stringify(status)}`,
      ]);
    } catch (error) {
      await logger.log("session_start.failed", { error: String(error) });
      ctx.ui.setStatus("autogui", "AutoGUI: unavailable");
      ctx.ui.setWidget("autogui", [`AutoGUI unavailable: ${String(error)}`]);
    }
  });
}
