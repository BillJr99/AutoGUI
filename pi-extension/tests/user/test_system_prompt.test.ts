/**
 * Snapshot-style tests for buildAutoGuiPrompt.
 *
 * The OSO branch must change the prompt body when osoAttached=true and
 * leave it unchanged when osoAttached=false.  No other input variation
 * should leak the OSO-specific instructions.
 */
import test from "node:test";
import assert from "node:assert/strict";

import { buildAutoGuiPrompt } from "../../src/index.js";
import { makeFakeBackend, makeDefaultConfig } from "./helpers/fakeBackend.js";

const OSO_HINT_SUBSTRING = "OS Screen Observer (OSO) is attached";
const OSO_TOOLS_HINT = "desktop_describe_screen";

test("prompt omits OSO branch when osoAttached=false", () => {
  const prompt = buildAutoGuiPrompt(
    makeDefaultConfig(),
    makeFakeBackend({ findElement: true }),
    /* osoAttached */ false,
  );
  assert.ok(!prompt.includes(OSO_HINT_SUBSTRING),
    "OSO branch leaked into prompt with osoAttached=false");
  assert.ok(!prompt.includes(OSO_TOOLS_HINT),
    "desktop_describe_screen leaked into prompt with osoAttached=false");
});

test("prompt includes OSO branch when osoAttached=true", () => {
  const prompt = buildAutoGuiPrompt(
    makeDefaultConfig(),
    makeFakeBackend({ findElement: true }),
    /* osoAttached */ true,
  );
  assert.ok(prompt.includes(OSO_HINT_SUBSTRING),
    "OSO branch missing when osoAttached=true");
  assert.ok(prompt.includes("desktop_describe_screen"),
    "desktop_describe_screen tool advertised in OSO branch is missing");
  assert.ok(prompt.includes("desktop_get_window_tree"),
    "desktop_get_window_tree advertised in OSO branch is missing");
});

test("default also defaults osoAttached to false", () => {
  const promptDefault = buildAutoGuiPrompt(
    makeDefaultConfig(), makeFakeBackend({ findElement: true }),
  );
  assert.ok(!promptDefault.includes(OSO_HINT_SUBSTRING));
});

test("OSO branch is added under Rules section (not duplicated)", () => {
  const prompt = buildAutoGuiPrompt(
    makeDefaultConfig(), makeFakeBackend({ findElement: true }),
    /* osoAttached */ true,
  );
  // Should appear exactly once.
  const occurrences = prompt.split(OSO_HINT_SUBSTRING).length - 1;
  assert.strictEqual(occurrences, 1,
    `OSO branch should be in prompt exactly once; saw ${occurrences}`);
});

test("disabling browser still produces a prompt with click ladder", () => {
  const cfg = makeDefaultConfig({ allowedBrowser: false });
  const prompt = buildAutoGuiPrompt(cfg, makeFakeBackend({ findElement: true }), false);
  assert.ok(prompt.includes("desktop_click_element"));
  assert.ok(!prompt.includes("browser_click for any element"));
});

test("planner-only prompt (controller off) still includes plan-first instruction", () => {
  const cfg = makeDefaultConfig({ controllerEnabled: false, plannerEnabled: true });
  const prompt = buildAutoGuiPrompt(cfg, makeFakeBackend({ findElement: true }), false);
  assert.ok(prompt.includes("Planning protocol"));
});
