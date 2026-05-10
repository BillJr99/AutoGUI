/**
 * budget.ts — Per-task cost telemetry + hard ceilings.
 *
 * Mirror of Python ``budget.py`` adapted to the pi-extension flow:
 * Pi owns the LLM loop, so we can't intercept token counts directly.
 * Instead, the extension increments tool-call and wall-time counters
 * inside the wrap() helper in tools.ts, and surfaces a snapshot via
 * the budget_status tool.  The model can then voluntarily wrap up
 * when fraction_used grows too high.
 */

export interface BudgetSnapshot {
  elapsedSeconds: number;
  toolCalls: number;
  fractionUsed: number;
  exceeded: boolean;
  note: string;
}

export class BudgetTracker {
  readonly maxToolCalls: number;
  readonly maxSeconds: number;
  readonly started: number;
  toolCalls = 0;

  constructor(opts: { maxToolCalls?: number; maxSeconds?: number } = {}) {
    this.maxToolCalls = opts.maxToolCalls ?? 0;
    this.maxSeconds = opts.maxSeconds ?? 0;
    this.started = Date.now() / 1000;
  }

  recordTool(): void {
    this.toolCalls += 1;
  }

  get elapsed(): number {
    return Date.now() / 1000 - this.started;
  }

  get exceeded(): boolean {
    if (this.maxToolCalls && this.toolCalls > this.maxToolCalls) return true;
    if (this.maxSeconds && this.elapsed > this.maxSeconds) return true;
    return false;
  }

  reason(): string {
    const parts: string[] = [];
    if (this.maxToolCalls && this.toolCalls > this.maxToolCalls) {
      parts.push(`tool_calls=${this.toolCalls}>${this.maxToolCalls}`);
    }
    if (this.maxSeconds && this.elapsed > this.maxSeconds) {
      parts.push(`elapsed=${this.elapsed.toFixed(1)}s>${this.maxSeconds}s`);
    }
    return parts.join("; ") || "within budget";
  }

  snapshot(note = ""): BudgetSnapshot {
    const fractions: number[] = [];
    if (this.maxToolCalls) fractions.push(this.toolCalls / this.maxToolCalls);
    if (this.maxSeconds) fractions.push(this.elapsed / this.maxSeconds);
    const frac = fractions.length ? Math.max(...fractions) : 0;
    return {
      elapsedSeconds: Math.round(this.elapsed * 100) / 100,
      toolCalls: this.toolCalls,
      fractionUsed: Math.round(frac * 1000) / 1000,
      exceeded: this.exceeded,
      note,
    };
  }

  reset(): void {
    this.toolCalls = 0;
    (this as { started: number }).started = Date.now() / 1000;
  }
}
