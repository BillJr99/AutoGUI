"""
preflight.py — Resource preflight checks before plan execution.

A "step zero" for the controller: given a plan, infer the resources it
needs (apps installed, files exist, network reachable, browser tools
available) and verify them up front.  When something is missing the
controller bails before touching any UI, returning a structured report
the caller can show to the user.

Checks supported
----------------
  app           — `which`/`where` an executable resolves
  file          — a file exists at a path
  url           — the URL's host:port is TCP-reachable (no HTTP request issued)
  tool          — a tool name is registered with the registry
  command       — a shell command exits 0

Specs are inferred from a plan's tools_hint + risks fields, plus an
explicit ``preflight`` array on the plan itself.  Callers can also
construct a list manually and call ``run_preflight`` directly.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import platform as _platform
import shutil
import socket
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


@dataclass
class PreflightCheck:
    kind: str           # app | file | url | tool | command
    target: str
    note: str = ""


@dataclass
class PreflightResult:
    check: PreflightCheck
    ok: bool
    detail: str = ""


@dataclass
class PreflightReport:
    results: list[PreflightResult] = field(default_factory=list)

    @property
    def all_passed(self) -> bool:
        return all(r.ok for r in self.results)

    def failures(self) -> list[PreflightResult]:
        return [r for r in self.results if not r.ok]

    def render(self) -> str:
        lines = []
        for r in self.results:
            mark = "✓" if r.ok else "✗"
            lines.append(f"  {mark} {r.check.kind}={r.check.target} {('— ' + r.detail) if r.detail else ''}")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "all_passed": self.all_passed,
            "results": [
                {"kind": r.check.kind, "target": r.check.target,
                 "ok": r.ok, "detail": r.detail, "note": r.check.note}
                for r in self.results
            ],
        }


# ---------------------------------------------------------------------------
# Single-check implementations
# ---------------------------------------------------------------------------

async def _check_app(target: str) -> PreflightResult:
    """Verify ``target`` resolves to an executable on PATH."""
    if not target:
        return PreflightResult(PreflightCheck("app", target), False, "empty target")
    # On Windows, models often pass 'edge' instead of 'msedge.exe'; try
    # a couple of common variants before giving up.
    candidates = [target]
    if _platform.system() == "Windows" and not target.lower().endswith(".exe"):
        candidates.append(target + ".exe")
    for c in candidates:
        path = shutil.which(c)
        if path:
            return PreflightResult(PreflightCheck("app", target), True,
                                   f"resolved to {path}")
    return PreflightResult(PreflightCheck("app", target), False,
                           "not on PATH")


async def _check_file(target: str) -> PreflightResult:
    if not target:
        return PreflightResult(PreflightCheck("file", target), False, "empty path")
    expanded = os.path.expanduser(target)
    # The ``file`` check kind is documented as "a file exists at a path".
    # ``os.path.exists`` would also pass for directories, so use isfile
    # to avoid treating a directory as a satisfied file requirement.
    if os.path.isfile(expanded):
        return PreflightResult(PreflightCheck("file", target), True,
                               f"resolved to {expanded}")
    if os.path.isdir(expanded):
        return PreflightResult(PreflightCheck("file", target), False,
                               f"path is a directory, not a file: {expanded}")
    return PreflightResult(PreflightCheck("file", target), False,
                           f"missing: {expanded}")


async def _check_url(target: str) -> PreflightResult:
    if not target:
        return PreflightResult(PreflightCheck("url", target), False, "empty url")
    try:
        parsed = urlparse(target)
    except ValueError as e:
        return PreflightResult(PreflightCheck("url", target), False, f"bad url: {e}")
    host = parsed.hostname
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if not host:
        return PreflightResult(PreflightCheck("url", target), False, "no host")
    # We only verify TCP reachability — a successful connect is good
    # enough; we don't want to perform a full HTTP request from here.
    loop = asyncio.get_event_loop()
    try:
        await loop.run_in_executor(None, lambda: _tcp_probe(host, port, 4.0))
    except OSError as e:
        return PreflightResult(PreflightCheck("url", target), False,
                               f"unreachable: {e}")
    return PreflightResult(PreflightCheck("url", target), True,
                           f"{host}:{port} reachable")


def _tcp_probe(host: str, port: int, timeout: float) -> None:
    with socket.create_connection((host, port), timeout=timeout):
        pass


async def _check_tool(target: str, registry) -> PreflightResult:
    if registry is None:
        return PreflightResult(PreflightCheck("tool", target), False, "no registry")
    if target in registry.list_tools():
        return PreflightResult(PreflightCheck("tool", target), True, "registered")
    return PreflightResult(PreflightCheck("tool", target), False, "not registered")


async def _check_command(target: str, registry) -> PreflightResult:
    if registry is None or "shell_run" not in registry.list_tools():
        return PreflightResult(PreflightCheck("command", target), False,
                               "shell unavailable")
    raw = await registry.dispatch("shell_run", {"command": target})
    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        return PreflightResult(PreflightCheck("command", target), False,
                               "shell result unparsable")
    if result.get("exit_code") in (0, None):
        return PreflightResult(PreflightCheck("command", target), True, "ok")
    return PreflightResult(PreflightCheck("command", target), False,
                           f"exit {result.get('exit_code')}: "
                           f"{(result.get('stderr') or '')[:120]}")


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------

async def run_preflight(checks: list[PreflightCheck], registry=None) -> PreflightReport:
    """Evaluate every check in parallel; return a ``PreflightReport``."""
    if not checks:
        return PreflightReport()
    coros = []
    for c in checks:
        if c.kind == "app":
            coros.append(_check_app(c.target))
        elif c.kind == "file":
            coros.append(_check_file(c.target))
        elif c.kind == "url":
            coros.append(_check_url(c.target))
        elif c.kind == "tool":
            coros.append(_check_tool(c.target, registry))
        elif c.kind == "command":
            coros.append(_check_command(c.target, registry))
        else:
            async def _bad(kind=c.kind, t=c.target):
                return PreflightResult(PreflightCheck(kind, t), False,
                                       f"unknown preflight kind: {kind}")
            coros.append(_bad())
    results = await asyncio.gather(*coros, return_exceptions=False)
    return PreflightReport(results=list(results))


def infer_checks_from_plan(plan_dict: dict, *, registry=None) -> list[PreflightCheck]:
    """
    Inspect a plan's typed payload for hints about needed resources.

    Looks at:
      * ``plan.preflight``               — explicit list of {kind,target,note}
      * each step's ``tools_hint``       — proposed tool requires a registered name
      * each step's ``predicate.path``   — file_exists / file_contains imply an existing path

    Duplicates are merged.  Returns a possibly-empty list.
    """
    seen: set[tuple[str, str]] = set()
    out: list[PreflightCheck] = []

    explicit = plan_dict.get("preflight") if isinstance(plan_dict, dict) else None
    if isinstance(explicit, list):
        for entry in explicit:
            if not isinstance(entry, dict):
                continue
            kind = str(entry.get("kind") or "")
            target = str(entry.get("target") or "")
            note = str(entry.get("note") or "")
            if kind and target and (kind, target) not in seen:
                seen.add((kind, target))
                out.append(PreflightCheck(kind, target, note))

    available_tools = set(registry.list_tools()) if registry is not None else set()
    for step in plan_dict.get("steps") or []:
        if not isinstance(step, dict):
            continue
        for hint in step.get("tools_hint") or []:
            tname = str(hint)
            if not tname or (("tool", tname) in seen):
                continue
            # Worth checking whenever we have any registry — even an
            # empty one — so the truthiness of available_tools doesn't
            # silently drop hinted tools when the caller's registry
            # happens to expose nothing yet.
            if registry is not None and tname not in available_tools:
                seen.add(("tool", tname))
                out.append(PreflightCheck("tool", tname,
                                          note=f"step {step.get('id')} hints {tname}"))
        pred = step.get("predicate") if isinstance(step, dict) else None
        if isinstance(pred, dict) and pred.get("kind") in (
            "file_exists", "file_contains"
        ):
            target = str(pred.get("path") or "")
            if target and ("file", target) not in seen:
                seen.add(("file", target))
                out.append(PreflightCheck("file", target,
                                          note=f"step {step.get('id')} predicate"))
    return out


__all__ = [
    "PreflightCheck", "PreflightResult", "PreflightReport",
    "run_preflight", "infer_checks_from_plan",
]
