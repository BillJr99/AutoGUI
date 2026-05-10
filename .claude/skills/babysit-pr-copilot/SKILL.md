---
name: babysit-pr-copilot
description: Poll a GitHub PR for new (unaddressed) Copilot review comments, address them, push fixes, and request a fresh Copilot review on a fixed cadence. Default cadence is every 3 minutes for 2 hours (40 ticks) with a 150-minute persistence cap. Reopens the PR off main if it has been closed/merged.
---

# babysit-pr-copilot

Run a self-paced loop that polls a PR for Copilot review feedback, fixes
each new (unaddressed, non-outdated) thread, pushes a fix commit, and
requests a fresh Copilot review. Runs to natural completion — Copilot
can take several minutes between when a review is requested and when
new threads appear, so a "no new threads" tick is not a stopping
condition; future ticks may still find feedback.

## Invocation

`/babysit-pr-copilot <repo> [pr#]`

- `<repo>` — `owner/name` (e.g. `billjr99/autogui`).
- `[pr#]` — optional. If omitted, use the most recent open PR on the
  branch you're currently working on (`mcp__github__list_pull_requests`
  filtered by `head=<current-branch>`).

If `<repo>` is missing, ask the user. Don't guess.

## Defaults

- **Tick cadence:** 3 minutes between polls.
- **Tick count:** 40 ticks (≈ 2 hours of polling).
- **Persistence cap:** 150 minutes — stop the monitor at that wall-clock
  budget regardless of remaining ticks.  The 30-minute buffer over the
  120-minute polling window covers tick-processing time (each fix-and-
  push iteration spends a couple of minutes outside `sleep 180`).
- **Branch:** the PR's head branch. Never push to `main`/`master`.

The user can override any of these in the invocation prompt.

## Loop structure (per tick)

1. **Pull review threads** with
   `mcp__github__pull_request_read(method="get_review_comments",
   perPage=100)`. Cursor pagination is needed only when totalCount > 100;
   the default page is fine for normal PRs.
2. **Filter to actionable** threads: `is_resolved=false` AND
   `is_outdated=false`. Drop threads whose timestamps are older than the
   most recent commit you pushed in a prior tick (those are the ones you
   already addressed — Copilot just hasn't re-reviewed yet).
3. **If no actionable threads on this tick:** emit a one-line "no new
   threads" status and yield to the next tick. Do NOT end the loop —
   Copilot's re-review can take several minutes after a request, and a
   later tick may still find feedback. Only the natural `loop-done`
   line, the persistence cap, or an unrecoverable error stops the loop.
4. **For each actionable thread:** read the file, fix the issue, mark
   the thread as addressed in your todo list. Group related fixes into a
   single commit when they touch the same module.
5. **Verify**: run `python -m pytest tests/` and `cd pi-extension &&
   npm run typecheck` (or whichever test/type commands the repo uses —
   detect from CLAUDE.md or `package.json`/`pytest.ini`).
6. **Commit & push** to the PR's head branch. Use a commit message of
   the form `controller: address Nth Copilot review pass`. Increment N
   per round so the history reads cleanly.
7. **Request review**: `mcp__github__request_copilot_review`.
8. **Yield** back to the Monitor tool to wait for the next tick.

## Closed/merged PR handling

If `pull_request_read(method="get")` returns `state="closed"`:

1. Branch off `origin/main` with a name like
   `claude/babysit-followup-<short-hash>`.
2. Cherry-pick the unpushed local fixes onto that branch (or just push
   them if the working tree is the source of truth).
3. Open a new PR with `mcp__github__create_pull_request` referencing
   the closed one in the body ("follow-up to #<old-pr>").
4. Continue the loop against the new PR number.

Don't ask before doing this — the user pre-authorized "open a new PR off
main" when they configured this skill.

## Stopping conditions

End the loop and report when **any** of these is true:

- Natural loop completion — the monitor emits `loop-done` after the
  configured number of ticks.
- Persistence cap hit (150 min / 2.5 h wall clock).
- A push fails with a non-network error (auth/permission/branch
  protection) — surface the error and stop instead of retrying blindly.
- Tests or typecheck fail and you can't repair them within the tick —
  push a WIP commit *if* you're confident, otherwise stop and ask.

A "no actionable threads" tick is **not** a stopping condition — the
loop runs the full window because Copilot's re-review can lag the
request by several minutes. There is no `TaskStop` available to cut a
monitor short; it always runs to completion.

## Self-pacing the ticks

Use the **Monitor** tool, NOT `Bash sleep`. Pattern:

```
Monitor(command="for i in $(seq 1 40); do echo \"tick-$i $(date -u +%H:%M:%SZ)\"; sleep 180; done; echo loop-done", description="PR #<n> 3-minute poll ticks (40 iterations)", timeout=9000000)
```

Each `tick-N` line wakes you up; do one full pass through the loop
above; then end your message and let the next tick wake you. The
`loop-done` line marks natural end.

If the user said "no need to start a new loop", honor that — only spin
up the monitor when this skill is explicitly invoked.

## Safety

- **NEVER** push to `main`/`master` even if the PR's head branch is
  somehow set to one of those.
- **NEVER** force-push.
- **NEVER** skip hooks (`--no-verify`).
- If a Copilot comment asks for a destructive change (drop a table,
  delete a file you can't easily recover), use `AskUserQuestion`
  before doing it.
- If you're uncertain how to address a comment, leave it for the user
  and continue with the others. Note the skipped one in your tick
  summary.

## Reporting

After each tick, output **one short line**:

> Tick N — addressed M new threads (paths: …); pushed `<sha>`; requested
> Copilot review.

After loop end, one short summary block listing all commits pushed and
any threads you skipped.
