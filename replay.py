"""
replay.py — Replay a recorded trajectory or saved skill, no LLM in the loop.

Two modes:

  python replay.py <path-to-trace.jsonl>
      Re-runs every tool_call recorded in the trace, in order.

  python replay.py --skill <name>
      Looks up <name> in the skill store and runs its steps.

The replay layer dispatches through the same ToolRegistry the agent uses,
so platform-specific backends (Windows / macOS / Linux / WSL) are picked up
automatically.

Use --speed to compress or stretch the inter-step delay; --continue-on-error
to push past failures instead of stopping at the first one.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from pathlib import Path

logger = logging.getLogger(__name__)


def _load_trace_steps(trace_path: Path) -> list[dict]:
    """Extract tool_call records from a trace JSONL into replayable steps."""
    steps: list[dict] = []
    for line in trace_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            evt = json.loads(line)
        except json.JSONDecodeError:
            continue
        if evt.get("kind") != "tool_call":
            continue
        data = evt.get("data") or {}
        tool = data.get("tool_name")
        args = data.get("args") or {}
        if tool:
            steps.append({"tool": tool, "args": args})
    return steps


async def _run_steps(
    steps: list[dict],
    speed: float = 1.0,
    continue_on_error: bool = False,
    drift_check: bool = False,
):
    """
    Replay ``steps`` through a fresh ToolRegistry.

    When ``drift_check`` is True, after each step the replay also takes a
    perceptual-hash snapshot of the screen and a window-list snapshot,
    then compares them to the same observations recorded the first time
    the skill ran (when available — captured under step['drift_anchor']).
    Steps whose post-state has drifted materially are flagged so the
    user knows the skill is no longer faithful to the world it was
    recorded against.
    """
    from skills import normalize_skill_steps
    from tools import ToolRegistry  # imported here to avoid circular import on module load

    steps = normalize_skill_steps(steps)

    cfg = {
        "tools": {
            "allowed_desktop": True,
            "allowed_shell": True,
            "allowed_filesystem": True,
        },
        "agent": {},
        "safety": {},
    }
    registry = ToolRegistry(cfg)

    drift_count = 0
    failures = 0
    for i, step in enumerate(steps, 1):
        tool = step["tool"]
        args = step.get("args", {}) or {}
        print(f"[{i}/{len(steps)}] {tool}({_short_args(args)})", flush=True)
        result_json = await registry.dispatch(tool, args)
        try:
            result = json.loads(result_json)
        except json.JSONDecodeError:
            result = {"raw": result_json[:200]}
        if "error" in result:
            failures += 1
            print(f"    error: {result['error']}", flush=True)
            if not continue_on_error:
                print("Stopping (use --continue-on-error to push through).", flush=True)
                return failures
        else:
            preview = {k: v for k, v in result.items() if k != "base64_png"}
            print(f"    ok: {_short(preview)}", flush=True)

        if drift_check:
            drifted = await _check_drift(step, registry)
            if drifted:
                drift_count += 1
                print(f"    drift: {drifted}", flush=True)

        if speed > 0:
            await asyncio.sleep(0.5 / speed)

    if drift_check and drift_count:
        print(f"Drift detected on {drift_count} step(s); the skill may be stale.",
              flush=True)
    return failures


async def _check_drift(step: dict, registry) -> str:
    """
    Compare the live post-state against ``step['drift_anchor']`` if one was
    recorded.  Returns a non-empty description when drift is detected.
    """
    anchor = step.get("drift_anchor")
    if not isinstance(anchor, dict):
        return ""

    notes: list[str] = []
    expected_titles = anchor.get("window_titles") or []
    if expected_titles and "desktop_list_windows" in registry.list_tools():
        try:
            raw = await registry.dispatch("desktop_list_windows", {})
            wins = json.loads(raw).get("windows") or []
            now_titles = [str(w.get("title", "")) for w in wins]
            missing = [t for t in expected_titles if t not in now_titles]
            if missing:
                notes.append(f"missing windows: {missing[:3]}")
        except Exception:
            pass

    expected_hash = anchor.get("screen_phash_b64")
    if expected_hash and "desktop_screenshot" in registry.list_tools():
        # The default fallback for visual_diff.diff() with a missing hash
        # is fraction_changed=0.0, which would silently report "no drift"
        # on systems without PIL or where the screenshot returned no
        # base64_png.  Track whether each side is actually available so
        # we can surface "screen hash unavailable" as a distinct note
        # instead of letting the user assume the screen looked identical.
        try:
            from visual_diff import diff as _vdiff, hash_b64 as _vhash
            import base64 as _b64
            try:
                expected = _b64.b64decode(expected_hash)
            except Exception:
                expected = None
            raw = await registry.dispatch("desktop_screenshot", {})
            shot = json.loads(raw)
            png_b64 = shot.get("base64_png", "")
            curr = _vhash(png_b64) if png_b64 else None
            if expected is None or curr is None:
                missing = []
                if expected is None:
                    missing.append("anchor")
                if curr is None:
                    missing.append("current")
                notes.append(
                    f"screen hash unavailable ({'/'.join(missing)} hash missing)"
                )
            else:
                d = _vdiff(expected, curr)
                if d.fraction_changed > 0.5:
                    notes.append(f"screen perceptual hash differs ({d.fraction_changed:.0%})")
        except Exception as e:
            notes.append(f"screen hash check failed: {type(e).__name__}")
    return "; ".join(notes)


def _short(obj) -> str:
    s = json.dumps(obj, default=str, ensure_ascii=False)
    return s if len(s) < 200 else s[:200] + "…"


def _short_args(args: dict) -> str:
    return ", ".join(f"{k}={_short(v)}" for k, v in args.items())


def main(argv: list[str] | None = None):
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    ap.add_argument("source", nargs="?", help="Path to trace JSONL")
    ap.add_argument("--skill", help="Replay a saved skill by name")
    ap.add_argument(
        "--skills-path", default="skills/skills.jsonl",
        help="Override skill store location",
    )
    ap.add_argument("--speed", type=float, default=1.0,
                    help="1.0 = default cadence; >1 faster, <1 slower")
    ap.add_argument("--continue-on-error", action="store_true",
                    help="Run remaining steps after a failure")
    ap.add_argument("--drift-check", action="store_true",
                    help="After each step, compare live state against the "
                         "step's drift_anchor (recorded windows + perceptual "
                         "screen hash) and flag drift.")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if args.skill:
        from skills import SkillStore
        store = SkillStore(args.skills_path)
        skill = store.get(args.skill)
        if not skill:
            print(f"No skill named {args.skill!r} in {store.path}", file=sys.stderr)
            return 1
        steps = skill.get("steps", [])
        if not steps:
            print(f"Skill {args.skill!r} has no steps", file=sys.stderr)
            return 1
        print(f"Replaying skill {args.skill!r} ({len(steps)} steps)…", flush=True)
        failures = asyncio.run(_run_steps(
            steps, speed=args.speed,
            continue_on_error=args.continue_on_error,
            drift_check=args.drift_check,
        ))
        if failures == 0:
            try:
                store.increment_success(args.skill)
            except Exception:
                pass
        return 0 if failures == 0 else 2

    if args.source:
        trace_path = Path(args.source).expanduser()
        if not trace_path.exists():
            print(f"Trace not found: {trace_path}", file=sys.stderr)
            return 1
        steps = _load_trace_steps(trace_path)
        if not steps:
            print(f"No tool_call events found in {trace_path}", file=sys.stderr)
            return 1
        print(f"Replaying {len(steps)} steps from {trace_path}…", flush=True)
        failures = asyncio.run(_run_steps(
            steps, speed=args.speed,
            continue_on_error=args.continue_on_error,
            drift_check=args.drift_check,
        ))
        return 0 if failures == 0 else 2

    ap.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
