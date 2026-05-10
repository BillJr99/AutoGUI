/**
 * controller.ts — Typed plan helpers for the pi extension.
 *
 * The pi extension runs inside Pi's own agent loop, so it doesn't drive
 * a step-by-step executor itself.  Instead the controller exposes the
 * Plan / PlanStep types as data structures and tools the LLM can read +
 * mutate, so the model can self-orchestrate using the typed plan and
 * the Pi runtime can recover progress across sessions.
 */

export enum StepStatus {
  PENDING = "pending",
  RUNNING = "running",
  DONE = "done",
  FAILED = "failed",
  SKIPPED = "skipped",
  BLOCKED = "blocked",
}

/** A typed post-condition predicate (mirrors the Python predicates module).
 *  Stored loose-typed so it survives JSON round-trips without forcing a
 *  controller change every time the predicate vocabulary grows. */
export type PlanPredicate = Record<string, unknown>;

/** A free-form preflight check the planner attaches to the plan
 *  itself (apps that need to be installed, files that need to exist,
 *  URLs that need to be reachable). */
export type PlanPreflight = Record<string, unknown>;

export interface PlanStep {
  id: string;
  goal: string;
  expected: string;
  /** Optional typed predicate evaluated by the controller after STEP_DONE. */
  predicate?: PlanPredicate;
  toolsHint: string[];
  dependsOn: string[];
  /** Pre-mortem risk notes the planner attached to the step. */
  risks: string[];
  status: StepStatus;
  attempts: number;
  lastError: string;
  artifacts: string[];
  notes: string;
}

export interface Plan {
  steps: PlanStep[];
  created: number;
  revision: number;
  /** Plan-level preflight checks (apps/files/URLs/tools/commands). */
  preflight: PlanPreflight[];
}

export function newPlan(): Plan {
  return { steps: [], created: Date.now() / 1000, revision: 0, preflight: [] };
}

export function planFromDict(data: Record<string, unknown>): Plan {
  const stepsData = (data["steps"] as Array<Record<string, unknown>>) ?? [];
  const steps: PlanStep[] = stepsData.map((sd) => {
    const rawPred = sd["predicate"];
    const predicate = (rawPred && typeof rawPred === "object" && !Array.isArray(rawPred))
      ? (rawPred as PlanPredicate)
      : undefined;
    const risks = Array.isArray(sd["risks"])
      ? (sd["risks"] as unknown[]).map((r) => String(r))
      : [];
    return {
      id: String(sd["id"] ?? ""),
      goal: String(sd["goal"] ?? ""),
      expected: String(sd["expected"] ?? ""),
      predicate,
      toolsHint: Array.isArray(sd["tools_hint"]) ? (sd["tools_hint"] as string[]).map(String) : [],
      dependsOn: Array.isArray(sd["depends_on"]) ? (sd["depends_on"] as string[]).map(String) : [],
      risks,
      status: parseStatus(String(sd["status"] ?? "pending")),
      attempts: Number(sd["attempts"] ?? 0),
      lastError: String(sd["last_error"] ?? ""),
      artifacts: Array.isArray(sd["artifacts"]) ? (sd["artifacts"] as string[]).map(String) : [],
      notes: String(sd["notes"] ?? ""),
    };
  });
  const rawPreflight = data["preflight"];
  const preflight: PlanPreflight[] = Array.isArray(rawPreflight)
    ? (rawPreflight as unknown[])
        .filter((p): p is Record<string, unknown> =>
          !!p && typeof p === "object" && !Array.isArray(p))
    : [];
  return {
    steps,
    created: Number(data["created"] ?? Date.now() / 1000),
    revision: Number(data["revision"] ?? 0),
    preflight,
  };
}

export function planToDict(plan: Plan): Record<string, unknown> {
  return {
    steps: plan.steps.map((s) => {
      const out: Record<string, unknown> = {
        id: s.id,
        goal: s.goal,
        expected: s.expected,
        tools_hint: s.toolsHint,
        depends_on: s.dependsOn,
        risks: s.risks,
        status: s.status,
        attempts: s.attempts,
        last_error: s.lastError,
        artifacts: s.artifacts,
        notes: s.notes,
      };
      // Only emit `predicate` when present so a step without one doesn't
      // pollute the wire payload with a null.  Mirrors the Python
      // controller.py behaviour.
      if (s.predicate) out.predicate = s.predicate;
      return out;
    }),
    created: plan.created,
    revision: plan.revision,
    preflight: plan.preflight,
  };
}

export function nextRunnable(plan: Plan): PlanStep | undefined {
  const doneIds = new Set(plan.steps.filter((s) => s.status === StepStatus.DONE).map((s) => s.id));
  for (const s of plan.steps) {
    if (s.status !== StepStatus.PENDING) continue;
    if (s.dependsOn.every((d) => doneIds.has(d))) return s;
  }
  return undefined;
}

export function progressSummary(plan: Plan): string {
  const counts: Record<string, number> = {};
  for (const s of plan.steps) counts[s.status] = (counts[s.status] ?? 0) + 1;
  return Object.entries(counts).sort().map(([k, v]) => `${k}=${v}`).join(", ");
}

export function renderForPrompt(plan: Plan): string {
  const marker: Record<StepStatus, string> = {
    [StepStatus.DONE]: "[x]",
    [StepStatus.RUNNING]: "[~]",
    [StepStatus.FAILED]: "[!]",
    [StepStatus.SKIPPED]: "[-]",
    [StepStatus.BLOCKED]: "[#]",
    [StepStatus.PENDING]: "[ ]",
  };
  return plan.steps.map((s, i) => {
    const head = `${marker[s.status]} ${i + 1}. (${s.id}) ${s.goal}`;
    const extras: string[] = [];
    if (s.expected) extras.push(`expected: ${s.expected}`);
    if (s.predicate) extras.push(`predicate: ${JSON.stringify(s.predicate)}`);
    if (s.toolsHint.length) extras.push(`tools: ${s.toolsHint.join(", ")}`);
    if (s.dependsOn.length) extras.push(`depends: ${s.dependsOn.join(", ")}`);
    if (s.risks.length) extras.push(`risks: ${s.risks.slice(0, 3).join("; ")}`);
    if (s.lastError && s.status === StepStatus.FAILED) extras.push(`last_error: ${s.lastError.slice(0, 80)}`);
    return extras.length ? `${head}\n     ${extras.join("; ")}` : head;
  }).join("\n");
}

export function parsePlan(raw: string): Plan {
  const stripped = (raw ?? "").trim().replace(/^```[a-z]*\n?/i, "").replace(/```$/, "").trim();
  if (!stripped) return newPlan();
  try {
    const data = JSON.parse(stripped);
    if (data && typeof data === "object" && Array.isArray((data as { steps?: unknown[] }).steps)) {
      return planFromDict(data as Record<string, unknown>);
    }
    if (Array.isArray(data)) return planFromDict({ steps: data });
  } catch {
    // fallthrough to numbered-list parser
  }
  const steps: PlanStep[] = [];
  let i = 0;
  for (const lineRaw of stripped.split("\n")) {
    const line = lineRaw.trim();
    if (!line) continue;
    i += 1;
    const m = line.match(/^\s*(?:\d+[.)]|[-*])\s+(.*)$/);
    const text = m ? m[1] : line;
    steps.push({
      id: `s${i}`, goal: text, expected: "", toolsHint: [],
      dependsOn: [], risks: [], status: StepStatus.PENDING, attempts: 0,
      lastError: "", artifacts: [], notes: "",
    });
  }
  return { steps, created: Date.now() / 1000, revision: 0, preflight: [] };
}

export function mergeRevisedPlan(current: Plan, revised: Plan): Plan {
  const byId = new Map(current.steps.map((s) => [s.id, s]));
  const merged: PlanStep[] = [];
  const seen = new Set<string>();
  for (const ns of revised.steps) {
    seen.add(ns.id);
    const prev = byId.get(ns.id);
    if (prev && (prev.status === StepStatus.DONE || prev.status === StepStatus.SKIPPED)) merged.push(prev);
    else merged.push(ns);
  }
  for (const old of current.steps) {
    if (!seen.has(old.id) && old.status === StepStatus.DONE) merged.push(old);
  }
  // Carry forward the revised plan's preflight when present, otherwise
  // keep the existing checks so a replan that doesn't restate them
  // doesn't silently drop them.
  const preflight = revised.preflight.length ? revised.preflight : current.preflight;
  return {
    steps: merged,
    created: current.created,
    revision: current.revision + 1,
    preflight,
  };
}

export interface StepOutcome { verdict: "done" | "blocked" | "failed"; reason: string; }

export function parseStepOutcome(text: string): StepOutcome {
  if (!text) return { verdict: "failed", reason: "" };
  const doneMatch = /^\s*STEP_DONE\s*:\s*(.+)$/im.exec(text);
  if (doneMatch) return { verdict: "done", reason: doneMatch[1].trim() };
  const blockedMatch = /^\s*STEP_BLOCKED\s*:\s*(.+)$/im.exec(text);
  if (blockedMatch) return { verdict: "blocked", reason: blockedMatch[1].trim() };
  return { verdict: "done", reason: text.trim().slice(0, 200) };
}

function parseStatus(s: string): StepStatus {
  switch (s) {
    case "running": return StepStatus.RUNNING;
    case "done": return StepStatus.DONE;
    case "failed": return StepStatus.FAILED;
    case "skipped": return StepStatus.SKIPPED;
    case "blocked": return StepStatus.BLOCKED;
    default: return StepStatus.PENDING;
  }
}
