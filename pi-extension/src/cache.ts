/**
 * cache.ts — Short-lived perception cache.
 *
 * Pi may invoke `desktop_screenshot` and `desktop_list_windows` back-to-back
 * during a single agentic step; without a cache we re-grab the framebuffer
 * and re-shell wmctrl every time.  A small TTL cache turns those repeats
 * into pointer copies.  Invalidated by every state-changing tool call.
 */

interface Entry<T> {
  value: T;
  expiresAt: number;
}

export class PerceptionCache {
  private readonly entries = new Map<string, Entry<unknown>>();
  private ttlMs: number;

  constructor(ttlMs = 500) {
    this.ttlMs = Math.max(0, ttlMs);
  }

  configure(ttlMs: number): void {
    this.ttlMs = Math.max(0, ttlMs);
  }

  /**
   * Return the cached value for `key` if it's fresh, otherwise undefined.
   */
  get<T>(key: string): T | undefined {
    if (this.ttlMs <= 0) return undefined;
    const entry = this.entries.get(key);
    if (!entry) return undefined;
    if (Date.now() > entry.expiresAt) {
      this.entries.delete(key);
      return undefined;
    }
    return entry.value as T;
  }

  set<T>(key: string, value: T): void {
    if (this.ttlMs <= 0) return;
    this.entries.set(key, { value, expiresAt: Date.now() + this.ttlMs });
  }

  /** Drop every cached entry.  Cheap; the map is normally tiny. */
  invalidate(): void {
    this.entries.clear();
  }
}
