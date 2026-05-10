/**
 * failures.ts — Structured failure classification.
 *
 * Mirror of Python ``failures.py``.  Same FailureClass + RecoveryAction
 * vocabulary so traces from either side talk about errors using the
 * same words.
 */

export enum FailureClass {
  TRANSIENT_IO = "transient_io",
  APP_NOT_READY = "app_not_ready",
  MISSING_ELEMENT = "missing_element",
  PERMISSION = "permission",
  PREDICATE_NOT_MET = "predicate_not_met",
  USER_INPUT_NEEDED = "user_input_needed",
  UNKNOWN = "unknown",
}

export enum RecoveryAction {
  RETRY = "retry",
  WAIT_AND_RETRY = "wait_and_retry",
  REPLAN = "replan",
  ESCALATE = "escalate",
  ABORT = "abort",
}

export interface FailureVerdict {
  cls: FailureClass;
  action: RecoveryAction;
  reason: string;
  waitSeconds: number;
}

const PATTERNS: Array<{ cls: FailureClass; re: RegExp }> = [
  { cls: FailureClass.PERMISSION,
    re: /\b(permission|access)\s+(denied|refused)\b|\beacces\b|\beperm\b|\boperation not permitted\b|\brun as administrator\b|\bauthorization (?:required|failed)\b/i },
  { cls: FailureClass.USER_INPUT_NEEDED,
    re: /\b(captcha|recaptcha|two[ -]?factor|2fa|verification code|sign in|signin|login required|consent|terms of service)\b/i },
  { cls: FailureClass.MISSING_ELEMENT,
    re: /\b(no (?:such )?element|selector .* (?:not found|did not match)|element not found|no marks? with id|find_element .* (?:no|none)|atspi.* not found|no visible text matching|no window matching|no candidates found)\b/i },
  { cls: FailureClass.APP_NOT_READY,
    re: /\b(?:not\s+(?:yet\s+)?(?:visible|loaded|ready|interactable)|stale element|element is detached|target closed|loading|waiting for .* to be ready|navigation interrupted)\b/i },
  { cls: FailureClass.TRANSIENT_IO,
    re: /\b(econnreset|econnrefused|etimedout|ehostunreach|enetunreach|temporarily unavailable|resource busy|ebusy|connection (?:reset|aborted|refused)|broken pipe|read timed? out|gateway timeout|503|502|504)\b/i },
];

const POLICY: Record<FailureClass, { action: RecoveryAction; wait: number }> = {
  [FailureClass.TRANSIENT_IO]: { action: RecoveryAction.WAIT_AND_RETRY, wait: 2.0 },
  [FailureClass.APP_NOT_READY]: { action: RecoveryAction.WAIT_AND_RETRY, wait: 1.5 },
  [FailureClass.MISSING_ELEMENT]: { action: RecoveryAction.REPLAN, wait: 0.0 },
  [FailureClass.PERMISSION]: { action: RecoveryAction.ESCALATE, wait: 0.0 },
  [FailureClass.USER_INPUT_NEEDED]: { action: RecoveryAction.ESCALATE, wait: 0.0 },
  [FailureClass.PREDICATE_NOT_MET]: { action: RecoveryAction.REPLAN, wait: 0.0 },
  [FailureClass.UNKNOWN]: { action: RecoveryAction.RETRY, wait: 0.0 },
};

export function classifyFailure(opts: {
  toolName: string;
  errorMessage: string;
  result?: Record<string, unknown>;
  predicateFailed?: boolean;
}): FailureVerdict {
  if (opts.predicateFailed) {
    const p = POLICY[FailureClass.PREDICATE_NOT_MET];
    return { cls: FailureClass.PREDICATE_NOT_MET, action: p.action, waitSeconds: p.wait, reason: "post-condition predicate did not hold" };
  }
  const msg = (opts.errorMessage || "").toLowerCase();
  if ((opts.result && (opts.result as { timed_out?: boolean }).timed_out) || /timed out|timeout/.test(msg)) {
    const p = POLICY[FailureClass.APP_NOT_READY];
    return { cls: FailureClass.APP_NOT_READY, action: p.action, waitSeconds: p.wait, reason: "tool reported timeout" };
  }
  for (const { cls, re } of PATTERNS) {
    if (re.test(msg)) {
      const p = POLICY[cls];
      return { cls, action: p.action, waitSeconds: p.wait, reason: `matched ${cls} pattern` };
    }
  }
  if (opts.toolName === "shell_run" && opts.result) {
    const ec = (opts.result as { exit_code?: number }).exit_code;
    if (ec !== undefined && ec !== 0) {
      const p = POLICY[FailureClass.TRANSIENT_IO];
      return { cls: FailureClass.TRANSIENT_IO, action: p.action, waitSeconds: p.wait, reason: `non-zero exit code (${ec})` };
    }
  }
  const p = POLICY[FailureClass.UNKNOWN];
  return { cls: FailureClass.UNKNOWN, action: p.action, waitSeconds: p.wait, reason: "no pattern matched" };
}

export function escalateAction(verdict: FailureVerdict, opts: { retryCount: number; maxRetries: number }): RecoveryAction {
  if (verdict.action === RecoveryAction.ESCALATE || verdict.action === RecoveryAction.ABORT) return verdict.action;
  if (opts.retryCount < opts.maxRetries) return verdict.action;
  if (verdict.action === RecoveryAction.RETRY || verdict.action === RecoveryAction.WAIT_AND_RETRY) return RecoveryAction.REPLAN;
  return RecoveryAction.ESCALATE;
}
