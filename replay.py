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


async def _run_steps(steps: list[dict], speed: float = 1.0, continue_on_error: bool = False):
    from tools import ToolRegistry  # imported here to avoid circular import on module load

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
        if speed > 0:
            await asyncio.sleep(0.5 / speed)
    return failures


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
        "--skills-path", default="~/.autogui/skills.jsonl",
        help="Override skill store location",
    )
    ap.add_argument("--speed", type=float, default=1.0,
                    help="1.0 = default cadence; >1 faster, <1 slower")
    ap.add_argument("--continue-on-error", action="store_true",
                    help="Run remaining steps after a failure")
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
            steps, speed=args.speed, continue_on_error=args.continue_on_error
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
            steps, speed=args.speed, continue_on_error=args.continue_on_error
        ))
        return 0 if failures == 0 else 2

    ap.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
