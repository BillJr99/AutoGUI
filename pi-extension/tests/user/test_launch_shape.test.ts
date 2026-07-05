/**
 * Locks the backend `launch()` return-shape parity with the Python mainline.
 *
 * Mainline standardised every backend's launch() on
 *   {success, application, args, pid, method}
 * (see backends/base.py, linux_x11.py, macos.py, windows.py, wsl.py).
 * This test asserts the Linux backend port emits the same keys so drift
 * between the two implementations is one diff away from being noticed.
 *
 * Only the Linux path is exercised directly (this is where a real pid is
 * observable); macOS/PowerShell return pid=null by design and are covered
 * by the typechecker + code review.
 */
import test from "node:test";
import assert from "node:assert/strict";

import { execFile } from "../../src/process.js";
import { LinuxBackend } from "../../src/backends/linux.js";
import type { PlatformInfo } from "../../src/types.js";

const HEADLESS_LINUX: PlatformInfo = {
  system: "linux",
  isWsl: false,
  isWayland: false,
  isX11: false,
  hasDisplay: false,
  summary: "linux (test-headless)",
};

test("execFile surfaces the spawned child pid", async () => {
  const result = await execFile("true", [], { timeoutMs: 5000 });
  assert.equal(result.code, 0);
  assert.equal(typeof result.pid, "number");
  assert.ok((result.pid ?? 0) > 0);
});

test("LinuxBackend.launch returns {success, application, args, pid, method}", async () => {
  const backend = new LinuxBackend(HEADLESS_LINUX);
  const out = await backend.launch("true", []);
  assert.equal(out.success, true);
  assert.equal(out.application, "true");
  assert.deepEqual(out.args, []);
  assert.equal(out.method, "subprocess");
  assert.equal(typeof out.pid, "number");
});
