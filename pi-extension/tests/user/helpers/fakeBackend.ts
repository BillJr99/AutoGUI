/**
 * Minimal fake DesktopBackend stub for prompt-shape tests.
 */
import type { DesktopBackend } from "../../../src/types.js";

export function makeFakeBackend(opts: { findElement?: boolean; click?: boolean; type?: boolean } = {}): DesktopBackend {
  const caps = {
    findElement: opts.findElement ?? false,
    click: opts.click ?? true,
    type: opts.type ?? true,
    listWindows: true,
    getActiveWindow: true,
    screenshot: true,
    screenshotMarked: true,
    launch: true,
  } as any;
  return {
    backend: "linux-x11",
    capabilities: caps,
    summary: () => "fake",
    // The shape we care about for prompt building — other methods are
    // not exercised by these tests.
  } as unknown as DesktopBackend;
}

export function makeDefaultConfig(overrides: Record<string, unknown> = {}): any {
  return {
    skillsEnabled: false,
    skillsPath: "",
    recordTrace: true,
    traceDir: "",
    installDependencies: false,
    perceptionCacheTtlMs: 500,
    allowedBrowser: true,
    browser: { headless: false, screenshotDir: "", userDataDir: "", viewport: {} },
    dryRun: false,
    allowedApps: [],
    blockedWindowTitles: [],
    plannerEnabled: true,
    controllerEnabled: true,
    artifactsEnabled: true,
    artifactsDir: "runtime/artifacts",
    progressEnabled: true,
    progressDir: "runtime/progress",
    memoryEnabled: false,
    memoryDir: "",
    budget: { maxToolCalls: 0, maxSeconds: 0 },
    visionEnabled: true,
    validateAfterAutogui: true,
    screenRecord: { enabled: false, fps: 5, bufferSeconds: 5, maxWidth: 960, outDir: "" },
    screenObserver: {
      disabled: false,
      baseUrl: "http://127.0.0.1:5001",
      timeoutMs: 2000,
      mcpServerName: "screen-observer",
      textObservation: { enabled: false },
    },
    ...overrides,
  };
}
