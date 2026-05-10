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

Specs are inferred from three sources on the plan: an explicit
``preflight`` array on the plan itself, each step's ``tools_hint``
list (proposed tool name → tool check), and predicate paths on
``file_exists`` / ``file_contains`` post-conditions (path → file
check).  ``risks`` is a free-form pre-mortem field surfaced to the
planner; it is NOT consumed by inference.  Callers can also
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

def _is_wsl() -> bool:
    """Linux kernel release advertises 'microsoft' under WSL2 / WSL1.

    Cached at module level via lru_cache would be nicer but this runs at
    most once per preflight check, so the read is cheap."""
    if _platform.system() != "Linux":
        return False
    try:
        with open("/proc/version", "r", encoding="utf-8") as f:
            return "microsoft" in f.read().lower()
    except OSError:
        return False


async def _check_app(target: str) -> PreflightResult:
    """Verify ``target`` resolves to an executable on PATH.

    On WSL the model often hints Windows-only apps (notepad, excel,
    chrome) that don't appear on the Linux PATH but ARE reachable
    through WSL interop.  We try shutil.which first, then fall back to
    ``where.exe`` so a Windows-side resolution still passes the check.
    """
    if not target:
        return PreflightResult(PreflightCheck("app", target), False, "empty target")
    # Models often pass bare names like 'edge' / 'notepad' / 'excel';
    # try the bare name first then the .exe variant on Windows AND WSL.
    candidates = [target]
    is_windowsy = _platform.system() == "Windows" or _is_wsl()
    if is_windowsy and not target.lower().endswith(".exe"):
        candidates.append(target + ".exe")
    for c in candidates:
        path = shutil.which(c)
        if path:
            return PreflightResult(PreflightCheck("app", target), True,
                                   f"resolved to {path}")
    # WSL fallback: ask Windows where the executable lives.  This finds
    # apps installed under C:\\Program Files / C:\\Windows that aren't
    # on the WSL PATH but are perfectly launchable through interop.
    if _is_wsl() and shutil.which("where.exe"):
        for c in candidates:
            try:
                # Run synchronously in a thread so the event loop stays
                # responsive even when 'where.exe' is slow.
                import subprocess
                result = await asyncio.to_thread(
                    subprocess.run,
                    ["where.exe", c],
                    capture_output=True, text=True, timeout=5,
                )
                if result.returncode == 0 and result.stdout.strip():
                    first = result.stdout.strip().splitlines()[0].strip()
                    return PreflightResult(
                        PreflightCheck("app", target), True,
                        f"resolved via where.exe: {first}",
                    )
            except (OSError, subprocess.SubprocessError):
                pass
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
    # Use asyncio.to_thread (Py3.9+) instead of get_event_loop() —
    # the latter is deprecated and can pick the wrong loop under
    # alternative loop policies.
    try:
        await asyncio.to_thread(_tcp_probe, host, port, 4.0)
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
    # The per-kind checkers build a fresh PreflightCheck(kind, target) for
    # the result, dropping any caller-supplied ``note`` — so the user's
    # explanation (e.g. "step s3 hints xdg-open") never made it into
    # PreflightReport.to_dict().  Copy the original ``note`` back onto each
    # result so reports keep the context the planner / inferrer attached.
    for original, result in zip(checks, results):
        if original.note:
            result.check.note = original.note
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
