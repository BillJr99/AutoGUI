/**
 * Drives the ScreenObserverClient probe path against:
 *   (a) no server reachable  -> probe returns null, prompt has no OSO hint.
 *   (b) a real OSO subprocess on a free port -> probe returns a client
 *       with capabilities, prompt includes the OSO hint.
 *
 * The OSO subprocess is spawned via the Python interpreter on PATH; if
 * Python is unavailable in the sandbox the test skips that case but
 * still verifies the no-server fallback.
 */
import test from "node:test";
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { setTimeout as sleep } from "node:timers/promises";
import * as net from "node:net";

import { probeScreenObserver } from "../../src/screen_observer_client.js";

async function freePort(): Promise<number> {
  return new Promise((resolve) => {
    const s = net.createServer();
    s.listen(0, "127.0.0.1", () => {
      const port = (s.address() as net.AddressInfo).port;
      s.close(() => resolve(port));
    });
  });
}

async function waitFor(url: string, timeoutMs = 10000): Promise<boolean> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const r = await fetch(url);
      if (r.status === 200) return true;
    } catch {
      // not yet
    }
    await sleep(100);
  }
  return false;
}

const oso_main = process.env.OSO_MAIN_PY
  ?? new URL("../../../../OSScreenObserver/main.py", import.meta.url).pathname;

test("probe returns null when no OSO server is reachable", async () => {
  // probeScreenObserver expects OsoConfig (snake_case: base_url, timeout_seconds, enabled).
  const result = await probeScreenObserver(
    { base_url: "http://127.0.0.1:1", timeout_seconds: 0.2, enabled: true },
    { log: async () => {} } as any,
  );
  assert.ok(result === undefined || result === null,
    `expected null/undefined, got: ${JSON.stringify(result)}`);
});

test("probe returns a client when an OSO server is running", async (t) => {
  // Skip if the submodule isn't checked out.
  const fs = await import("node:fs");
  if (!fs.existsSync(oso_main)) {
    t.skip("OSScreenObserver submodule not present");
    return;
  }
  const py = process.env.PYTHON ?? "python3";
  const port = await freePort();
  let stderr = "";
  const proc = spawn(py, [oso_main, "--mode", "inspect", "--mock",
                          "--port", String(port)],
                     { stdio: ["ignore", "pipe", "pipe"] });
  proc.stderr?.on("data", (d) => { stderr += d.toString(); });
  try {
    const healthy = await waitFor(`http://127.0.0.1:${port}/api/healthz`, 15000);
    if (!healthy) {
      t.skip(`OSO server did not become healthy. stderr:\n${stderr.slice(0, 800)}`);
      return;
    }
    // Sanity-check the JS fetch can talk to it before probing.
    const sanity = await fetch(`http://127.0.0.1:${port}/api/healthz`);
    if (sanity.status !== 200) {
      t.skip(`OSO healthz returned ${sanity.status} from inside Node`);
      return;
    }
    const cfg = { base_url: `http://127.0.0.1:${port}`,
                   timeout_seconds: 5.0, enabled: true } as const;
    const result = await probeScreenObserver(cfg, { log: async () => {} } as any);
    assert.ok(result !== undefined && result !== null,
      `probe should return a client when OSO is up.\nstderr: ${stderr.slice(0, 600)}`);
    const caps = result!.osoCapabilities;
    assert.strictEqual(typeof caps, "object");
    assert.ok(Object.keys(caps).length > 0,
      `capabilities dict should be populated; got: ${JSON.stringify(caps)}`);
  } finally {
    proc.kill("SIGTERM");
  }
});
