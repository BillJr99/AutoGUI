/**
 * browser_backend.ts — Playwright-driven browser automation.
 *
 * Lazy-imports `playwright` so the extension itself has no hard dep on it;
 * if the package isn't installed the backend reports a friendly error and
 * the user can either run the auto-installer or install manually.
 *
 * One Chromium instance, one context, one page.  Multi-tab juggling is
 * intentionally not exposed to the model — it adds surface area for
 * confusion without much practical benefit for typical desktop tasks.
 */

import { mkdir } from "node:fs/promises";
import { join } from "node:path";

export interface BrowserConfig {
  headless: boolean;
  screenshotDir: string;
  userDataDir: string;
  viewport: { width: number; height: number };
}

export class BrowserBackend {
  private playwright: any | undefined;
  private browser: any | undefined;
  private context: any | undefined;
  private page: any | undefined;
  private starting?: Promise<void>;

  constructor(private readonly cfg: BrowserConfig) {}

  /** Lazy bring-up.  Returns undefined on success, error message otherwise. */
  async ensure(): Promise<string | undefined> {
    if (this.page) return undefined;
    if (this.starting) {
      try { await this.starting; return undefined; } catch (e) { return (e as Error).message; }
    }
    this.starting = (async () => {
      let pw: any;
      try {
        // Dynamic-import indirection so tsc doesn't fail when the optional
        // dep isn't installed at type-check time.
        const mod: unknown = "playwright";
        pw = await import(mod as string);
      } catch {
        throw new Error(
          "Playwright is not installed. Run `bash scripts/install-dependencies.sh` " +
          "(or `scripts\\install-dependencies.cmd` on Windows), or set " +
          "`installDependencies: true` in pi-extension/config.json to have AutoGUI " +
          "run that script at session start.",
        );
      }
      this.playwright = pw;
      if (this.cfg.userDataDir) {
        await mkdir(this.cfg.userDataDir, { recursive: true });
        this.context = await pw.chromium.launchPersistentContext(this.cfg.userDataDir, {
          headless: this.cfg.headless,
          viewport: this.cfg.viewport,
        });
        const pages = this.context.pages();
        this.page = pages.length ? pages[0] : await this.context.newPage();
      } else {
        this.browser = await pw.chromium.launch({ headless: this.cfg.headless });
        this.context = await this.browser.newContext({ viewport: this.cfg.viewport });
        this.page = await this.context.newPage();
      }
      await mkdir(this.cfg.screenshotDir, { recursive: true });
    })();
    try {
      await this.starting;
      return undefined;
    } catch (e) {
      this.starting = undefined;
      return (e as Error).message;
    }
  }

  async close(): Promise<void> {
    try { await this.context?.close(); } catch { /* noop */ }
    try { await this.browser?.close(); } catch { /* noop */ }
    this.page = this.context = this.browser = undefined;
    this.starting = undefined;
  }

  async navigate(url: string): Promise<Record<string, unknown>> {
    const err = await this.ensure();
    if (err) return { error: err };
    const resp = await this.page.goto(url, { timeout: 30000, waitUntil: "domcontentloaded" });
    return {
      url: this.page.url(),
      status: resp?.status?.() ?? null,
      title: await this.page.title(),
    };
  }

  async back(): Promise<Record<string, unknown>> {
    const err = await this.ensure();
    if (err) return { error: err };
    await this.page.goBack({ waitUntil: "domcontentloaded" });
    return { url: this.page.url() };
  }

  async forward(): Promise<Record<string, unknown>> {
    const err = await this.ensure();
    if (err) return { error: err };
    await this.page.goForward({ waitUntil: "domcontentloaded" });
    return { url: this.page.url() };
  }

  async reload(): Promise<Record<string, unknown>> {
    const err = await this.ensure();
    if (err) return { error: err };
    await this.page.reload({ waitUntil: "domcontentloaded" });
    return { url: this.page.url() };
  }

  async click(selector: string): Promise<Record<string, unknown>> {
    const err = await this.ensure();
    if (err) return { error: err };
    await this.page.click(selector, { timeout: 10000 });
    return { clicked: selector, url: this.page.url() };
  }

  async fill(selector: string, value: string): Promise<Record<string, unknown>> {
    const err = await this.ensure();
    if (err) return { error: err };
    await this.page.fill(selector, value, { timeout: 10000 });
    return { filled: selector, length: value.length };
  }

  async press(selector: string, key: string): Promise<Record<string, unknown>> {
    const err = await this.ensure();
    if (err) return { error: err };
    if (selector) {
      await this.page.press(selector, key, { timeout: 10000 });
    } else {
      await this.page.keyboard.press(key);
    }
    return { pressed: key };
  }

  async getText(selector?: string, maxChars = 50000): Promise<Record<string, unknown>> {
    const err = await this.ensure();
    if (err) return { error: err };
    const sel = selector || "body";

    const pack = (txt: string, via?: string) => {
      const truncated = txt.length > maxChars;
      return {
        text: truncated ? txt.slice(0, maxChars) : txt,
        length: truncated ? maxChars : txt.length,
        truncated,
        url: this.page.url(),
        ...(via ? { via } : {}),
      };
    };

    // Attempt 1: Playwright innerText — honours CSS visibility.
    try {
      const txt = (await this.page.innerText(sel, { timeout: 10000 })) ?? "";
      return pack(txt);
    } catch { /* fall through */ }

    // Attempt 2: Clipboard — select all, copy, read via navigator.clipboard.
    // Grant permissions first so readText() doesn't prompt or throw.
    try {
      await this.page.context().grantPermissions(["clipboard-read", "clipboard-write"]);
      await this.page.keyboard.press("Control+a");
      await this.page.keyboard.press("Control+c");
      const txt: string = await this.page.evaluate(
        async () => { try { return await navigator.clipboard.readText(); } catch { return ""; } }
      );
      if (txt) return pack(txt, "clipboard");
    } catch { /* fall through */ }

    // Attempt 3: Plain JS evaluate — works even when DOM is unconventional.
    try {
      const txt: string = await this.page.evaluate(
        () => (document.body as HTMLElement).innerText || document.body.textContent || ""
      );
      return pack(txt, "evaluate");
    } catch (e) {
      return { error: `getText failed (all methods): ${e instanceof Error ? e.message : String(e)}`, url: this.page.url() };
    }
  }

  async screenshot(fullPage = false): Promise<Record<string, unknown>> {
    const err = await this.ensure();
    if (err) return { error: err };
    const stamp = new Date().toISOString().replace(/[:.]/g, "-");
    const path = join(this.cfg.screenshotDir, `browser_${stamp}.png`);
    const png = await this.page.screenshot({ path, fullPage });
    return {
      path,
      url: this.page.url(),
      title: await this.page.title(),
      data: Buffer.from(png).toString("base64"),
      mimeType: "image/png" as const,
    };
  }

  async evalJs(expression: string): Promise<Record<string, unknown>> {
    const err = await this.ensure();
    if (err) return { error: err };
    const value = await this.page.evaluate(expression);
    return { value };
  }
}
