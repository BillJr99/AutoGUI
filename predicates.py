"""
predicates.py — Typed post-conditions for plan steps.

A predicate is a structured, deterministically-checkable assertion about
desktop / file / browser state that a plan step claims will hold once it
finishes.  Promoting `expected` from free text to a typed predicate lets
the controller verify a step's success without an LLM round-trip and
fire ``PREDICATE_NOT_MET`` failure classification automatically when it
doesn't hold.

Predicate kinds
---------------
  window_title_contains   — any visible window title contains a substring
  window_active_app       — currently focused window matches an app name
  file_exists             — a path resolves to an existing file
  file_absent             — a path does NOT resolve to a file
  file_contains           — a file's body contains a substring
  url_contains            — the browser's current URL contains a substring
  text_visible            — a visible label is on screen (a11y or OCR)
  process_running         — a process matching a name pattern is running
  shell_returns           — a probe shell command exits 0 with optional stdout match

The controller reads ``step.predicate`` (when present) after the step's
``STEP_DONE`` marker fires and calls ``check_predicate``.  A predicate
miss converts the verdict to ``BLOCKED`` so the standard replan / retry
machinery kicks in.

Predicates are stored as plain JSON-serialisable dicts so they survive
the Plan.to_dict() round-trip and replay.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class PredicateResult:
    ok: bool
    kind: str
    detail: str = ""
    observed: Any = None


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

_KNOWN_KINDS = frozenset({
    "window_title_contains",
    "window_active_app",
    "file_exists",
    "file_absent",
    "file_contains",
    "url_contains",
    "text_visible",
    "process_running",
    "shell_returns",
})


def normalize(predicate: Any) -> dict | None:
    """
    Coerce a free-form predicate spec into a canonical dict.  Returns
    None when the input is not a usable predicate (so callers can fall
    back to LLM-judged ``expected`` text).
    """
    if not isinstance(predicate, dict):
        return None
    kind = predicate.get("kind") or predicate.get("type")
    if kind not in _KNOWN_KINDS:
        return None
    out = dict(predicate)
    out["kind"] = kind
    out.pop("type", None)
    return out


def render(predicate: dict) -> str:
    """One-line human-readable description for prompts and traces."""
    kind = predicate.get("kind", "?")
    if kind == "window_title_contains":
        return f"a window with title containing {predicate.get('value')!r}"
    if kind == "window_active_app":
        return f"the focused window's app matches {predicate.get('value')!r}"
    if kind == "file_exists":
        return f"file exists: {predicate.get('path')!r}"
    if kind == "file_absent":
        return f"file does NOT exist: {predicate.get('path')!r}"
    if kind == "file_contains":
        return f"file {predicate.get('path')!r} contains {predicate.get('value')!r}"
    if kind == "url_contains":
        return f"browser URL contains {predicate.get('value')!r}"
    if kind == "text_visible":
        return f"text visible on screen: {predicate.get('value')!r}"
    if kind == "process_running":
        return f"process matching {predicate.get('value')!r} is running"
    if kind == "shell_returns":
        cmd = predicate.get("command", "")
        m = predicate.get("stdout_contains")
        return f"`{cmd[:60]}` exits 0" + (f" and stdout contains {m!r}" if m else "")
    return kind


# ---------------------------------------------------------------------------
# Checking
# ---------------------------------------------------------------------------

async def check_predicate(predicate: dict, registry) -> PredicateResult:
    """
    Evaluate ``predicate`` against the live system using ``registry`` for
    tool dispatch.  Never raises — failure modes return ``ok=False`` with
    an explanatory ``detail``.

    The registry is the standalone-agent ToolRegistry; this function calls
    desktop_list_windows / desktop_get_active_window / fs_read /
    desktop_find_text / browser_eval / shell_run as appropriate.
    """
    p = normalize(predicate)
    if p is None:
        return PredicateResult(False, "?", "predicate normalised to None")
    kind = p["kind"]

    try:
        if kind == "window_title_contains":
            return await _check_window_title(p, registry)
        if kind == "window_active_app":
            return await _check_active_app(p, registry)
        if kind == "file_exists":
            return _check_file_presence(p, must_exist=True)
        if kind == "file_absent":
            return _check_file_presence(p, must_exist=False)
        if kind == "file_contains":
            return _check_file_contains(p)
        if kind == "url_contains":
            return await _check_url(p, registry)
        if kind == "text_visible":
            return await _check_text_visible(p, registry)
        if kind == "process_running":
            return await _check_process(p, registry)
        if kind == "shell_returns":
            return await _check_shell(p, registry)
    except Exception as e:
        logger.warning("[predicates] check %s raised: %s", kind, e)
        return PredicateResult(False, kind, f"check raised: {e}")

    return PredicateResult(False, kind, "no checker for this kind")


# ---------------------------------------------------------------------------
# Per-kind checkers
# ---------------------------------------------------------------------------

async def _check_window_title(p: dict, registry) -> PredicateResult:
    needle = str(p.get("value") or "")
    if not needle:
        return PredicateResult(False, p["kind"], "empty value")
    if "desktop_list_windows" not in set(registry.list_tools()):
        return PredicateResult(False, p["kind"], "backend has no list_windows")
    raw = await registry.dispatch("desktop_list_windows", {})
    try:
        result = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return PredicateResult(False, p["kind"], "window listing unparsable")
    # Surface dispatch-level errors directly so the model sees a clear
    # diagnostic instead of "no window contains …" when the real issue
    # is "tool returned an error".
    if isinstance(result, dict) and "error" in result:
        return PredicateResult(False, p["kind"],
                               f"list_windows error: {result['error']}",
                               result)
    wins = result.get("windows") if isinstance(result, dict) else []
    wins = wins or []
    for w in wins:
        title = str(w.get("title", "") or "")
        if needle.lower() in title.lower():
            return PredicateResult(True, p["kind"], f"matched {title!r}", w)
    return PredicateResult(False, p["kind"],
                           f"no window title contains {needle!r}",
                           {"windows": [w.get("title", "") for w in wins]})


async def _check_active_app(p: dict, registry) -> PredicateResult:
    target = str(p.get("value") or "").lower()
    if not target:
        return PredicateResult(False, p["kind"], "empty value")
    if "desktop_get_active_window" not in set(registry.list_tools()):
        return PredicateResult(False, p["kind"], "backend has no get_active_window")
    raw = await registry.dispatch("desktop_get_active_window", {})
    try:
        info = json.loads(raw)
    except json.JSONDecodeError:
        return PredicateResult(False, p["kind"], "active-window listing unparsable")
    if not info or not info.get("found"):
        return PredicateResult(False, p["kind"], "no active window detected")
    win = info.get("window") or info
    app = str(win.get("app", "") or "").lower()
    title = str(win.get("title", "") or "").lower()
    if target in app or target in title:
        return PredicateResult(True, p["kind"], f"active app/title matched {target!r}", win)
    return PredicateResult(False, p["kind"],
                           f"active app={app!r} title={title!r}", win)


def _check_file_presence(p: dict, *, must_exist: bool) -> PredicateResult:
    path = str(p.get("path") or "")
    if not path:
        return PredicateResult(False, p["kind"], "empty path")
    # ``file_exists`` / ``file_absent`` are *file* predicates — a directory
    # at the path should NOT satisfy file_exists, and a directory should
    # NOT make file_absent fail.  Use is_file so both cases ignore
    # directories the same way callers naturally read the kind name.
    expanded = Path(path).expanduser()
    is_file = expanded.is_file()
    if is_file == must_exist:
        return PredicateResult(True, p["kind"],
                               f"file_{'exists' if is_file else 'absent'} satisfied", path)
    # Distinguish "missing" from "is a directory" so the failure detail
    # tells the caller which of those is actually true; a generic
    # "unexpectedly absent" message hides the latter case.
    if must_exist:
        if expanded.is_dir():
            detail = f"path is a directory, not a file: {path}"
        else:
            detail = f"file not found: {path}"
    else:
        # must_exist=False but is_file=True
        detail = f"file unexpectedly present: {path}"
    return PredicateResult(False, p["kind"], detail)


def _check_file_contains(p: dict) -> PredicateResult:
    path = str(p.get("path") or "")
    needle = str(p.get("value") or "")
    if not path or not needle:
        return PredicateResult(False, p["kind"], "empty path or value")
    f = Path(path).expanduser()
    if not f.is_file():
        return PredicateResult(False, p["kind"], f"file missing: {path}")
    try:
        body = f.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return PredicateResult(False, p["kind"], f"read failed: {e}")
    if needle in body:
        return PredicateResult(True, p["kind"], "substring present", path)
    # Try regex if the value looks like one.
    if needle.startswith("/") and needle.endswith("/") and len(needle) > 2:
        pat = needle[1:-1]
        try:
            if re.search(pat, body):
                return PredicateResult(True, p["kind"], "regex matched", path)
        except re.error:
            pass
    return PredicateResult(False, p["kind"],
                           f"substring {needle!r} not in file ({len(body)}B)")


async def _check_url(p: dict, registry) -> PredicateResult:
    needle = str(p.get("value") or "")
    if not needle:
        return PredicateResult(False, p["kind"], "empty value")
    if "browser_eval" not in set(registry.list_tools()):
        return PredicateResult(False, p["kind"],
                               "browser tools unavailable; cannot verify URL")
    raw = await registry.dispatch("browser_eval", {"expression": "window.location.href"})
    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        return PredicateResult(False, p["kind"], "browser_eval result unparsable")
    if "error" in result:
        return PredicateResult(False, p["kind"], f"browser_eval error: {result['error']}")
    href = str(result.get("value", "") or "")
    if needle.lower() in href.lower():
        return PredicateResult(True, p["kind"], f"URL matched {href}", href)
    return PredicateResult(False, p["kind"],
                           f"URL {href!r} does not contain {needle!r}", href)


async def _check_text_visible(p: dict, registry) -> PredicateResult:
    needle = str(p.get("value") or "")
    if not needle:
        return PredicateResult(False, p["kind"], "empty value")
    tools = set(registry.list_tools())
    if "desktop_find_text" in tools:
        raw = await registry.dispatch("desktop_find_text", {"text": needle})
        try:
            result = json.loads(raw)
        except json.JSONDecodeError:
            result = {}
        if result.get("found"):
            return PredicateResult(True, p["kind"], f"text {needle!r} visible",
                                   result.get("match"))
        return PredicateResult(False, p["kind"], f"text {needle!r} not on screen", result)
    return PredicateResult(False, p["kind"],
                           "no OCR backend available for text-visible check")


async def _check_process(p: dict, registry) -> PredicateResult:
    needle = str(p.get("value") or "")
    if not needle:
        return PredicateResult(False, p["kind"], "empty value")
    if "shell_run" not in set(registry.list_tools()):
        return PredicateResult(False, p["kind"], "shell unavailable; cannot list processes")
    import platform as _p
    cmd = (
        f'tasklist /FI "IMAGENAME eq {needle}*"' if _p.system() == "Windows"
        else f'pgrep -lf {needle!r}'
    )
    raw = await registry.dispatch("shell_run", {"command": cmd})
    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        return PredicateResult(False, p["kind"], "shell result unparsable")
    out = (result.get("stdout") or "")
    if needle.lower() in out.lower():
        return PredicateResult(True, p["kind"], "process found", out[:160])
    return PredicateResult(False, p["kind"],
                           f"no process matching {needle!r} found", out[:160])


async def _check_shell(p: dict, registry) -> PredicateResult:
    cmd = str(p.get("command") or "")
    expect = p.get("stdout_contains")
    if not cmd:
        return PredicateResult(False, p["kind"], "empty command")
    if "shell_run" not in set(registry.list_tools()):
        return PredicateResult(False, p["kind"], "shell unavailable")
    raw = await registry.dispatch("shell_run", {"command": cmd})
    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        return PredicateResult(False, p["kind"], "shell result unparsable")
    if result.get("exit_code") not in (0, None):
        return PredicateResult(False, p["kind"],
                               f"exit_code={result.get('exit_code')}",
                               result.get("stderr", "")[:160])
    if expect:
        out = result.get("stdout") or ""
        if str(expect) not in out:
            return PredicateResult(False, p["kind"],
                                   f"stdout missing {expect!r}", out[:160])
    return PredicateResult(True, p["kind"], "shell probe satisfied",
                           (result.get("stdout") or "")[:160])


# ---------------------------------------------------------------------------
# Helper for offline (unit-test) predicate evaluation.
# ---------------------------------------------------------------------------

def check_filesystem_predicate_sync(predicate: dict) -> PredicateResult:
    """Subset of check_predicate that doesn't need a registry — for tests."""
    p = normalize(predicate)
    if p is None:
        return PredicateResult(False, "?", "invalid predicate")
    if p["kind"] == "file_exists":
        return _check_file_presence(p, must_exist=True)
    if p["kind"] == "file_absent":
        return _check_file_presence(p, must_exist=False)
    if p["kind"] == "file_contains":
        return _check_file_contains(p)
    return PredicateResult(False, p["kind"], "needs registry; use check_predicate")


__all__ = ["PredicateResult", "normalize", "render", "check_predicate",
           "check_filesystem_predicate_sync"]
