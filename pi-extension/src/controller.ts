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

export interface PlanStep {
  id: string;
  goal: string;
  expected: string;
  toolsHint: string[];
  dependsOn: string[];
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
}

export function newPlan(): Plan {
  return { steps: [], created: Date.now() / 1000, revision: 0 };
}

export function planFromDict(data: Record<string, unknown>): Plan {
  const stepsData = (data["steps"] as Array<Record<string, unknown>>) ?? [];
  const steps: PlanStep[] = stepsData.map((sd) => ({
    id: String(sd["id"] ?? ""),
    goal: String(sd["goal"] ?? ""),
    expected: String(sd["expected"] ?? ""),
    toolsHint: Array.isArray(sd["tools_hint"]) ? (sd["tools_hint"] as string[]).map(String) : [],
    dependsOn: Array.isArray(sd["depends_on"]) ? (sd["depends_on"] as string[]).map(String) : [],
    status: parseStatus(String(sd["status"] ?? "pending")),
    attempts: Number(sd["attempts"] ?? 0),
    lastError: String(sd["last_error"] ?? ""),
    artifacts: Array.isArray(sd["artifacts"]) ? (sd["artifacts"] as string[]).map(String) : [],
    notes: String(sd["notes"] ?? ""),
  }));
  return {
    steps,
    created: Number(data["created"] ?? Date.now() / 1000),
    revision: Number(data["revision"] ?? 0),
  };
}

export function planToDict(plan: Plan): Record<string, unknown> {
  return {
    steps: plan.steps.map((s) => ({
      id: s.id, goal: s.goal, expected: s.expected,
      tools_hint: s.toolsHint, depends_on: s.dependsOn,
      status: s.status, attempts: s.attempts,
      last_error: s.lastError, artifacts: s.artifacts, notes: s.notes,
    })),
    created: plan.created,
    revision: plan.revision,
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
    if (s.toolsHint.length) extras.push(`tools: ${s.toolsHint.join(", ")}`);
    if (s.dependsOn.length) extras.push(`depends: ${s.dependsOn.join(", ")}`);
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
      dependsOn: [], status: StepStatus.PENDING, attempts: 0,
      lastError: "", artifacts: [], notes: "",
    });
  }
  return { steps, created: Date.now() / 1000, revision: 0 };
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
  return { steps: merged, created: current.created, revision: current.revision + 1 };
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
