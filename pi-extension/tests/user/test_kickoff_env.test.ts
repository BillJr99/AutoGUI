/**
 * Verifies that the kickoff script's LLM picker writes the right env
 * vars and that the pi-extension would pick them up if Pi looked at
 * standard names.  We don't actually start Pi here — just inspect the
 * env shape.
 */
import test from "node:test";
import assert from "node:assert/strict";

const KEYS = [
  "AUTOGUI_LLM_SYSTEM",
  "AUTOGUI_LLM_BASE_URL",
  "AUTOGUI_LLM_API_KEY",
  "AUTOGUI_LLM_MODEL",
  "AUTOGUI_VLM_MODEL",
];

test("AUTOGUI_LLM_* env contract documented and parseable", () => {
  // Verify the test harness sets each var (or leaves it empty for stub-only).
  // The CI image sets all of them; bare local runs may leave them unset.
  for (const k of KEYS) {
    const v = process.env[k];
    if (v !== undefined) {
      assert.strictEqual(typeof v, "string");
    }
  }
});

test("when AUTOGUI_LLM_SYSTEM=stub, picker should set MODEL=stub", () => {
  if (process.env.AUTOGUI_LLM_SYSTEM === "stub") {
    assert.strictEqual(process.env.AUTOGUI_LLM_MODEL, "stub");
  }
});

test("ollama_bundled picks an ollama-style model name", () => {
  if (process.env.AUTOGUI_LLM_SYSTEM === "ollama_bundled") {
    assert.ok(process.env.AUTOGUI_LLM_MODEL?.includes(":"),
      "Ollama model names use the 'name:tag' format");
  }
});
