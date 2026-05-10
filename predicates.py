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

import json
import logging
import re
import shlex
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
# Public alias so external callers (agent.py, tests, dashboards) can
# read the supported set without reaching into a single-underscore
# private name.  Same frozenset object — no copy.
PREDICATE_KINDS = _KNOWN_KINDS


def normalize(predicate: Any) -> dict | None:
    """
    Coerce a free-form predicate spec into a canonical dict.  Returns
    None when the input is not a usable predicate (so callers can fall
    back to LLM-judged ``expected`` text).
    """
    if not isinstance(predicate, dict):
        return None
    kind = predicate.get("kind") or predicate.get("type")
    # The model can return non-string `kind` values (a dict, a list,
    # None) when output is malformed.  ``in _KNOWN_KINDS`` would raise
    # TypeError on an unhashable list/dict and crash the caller, so
    # gate on isinstance first.
    if not isinstance(kind, str) or kind not in _KNOWN_KINDS:
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
    needle_lc = needle.lower()
    ocr_detail = ""
    if "desktop_find_text" in tools:
        raw = await registry.dispatch("desktop_find_text", {"text": needle})
        try:
            result = json.loads(raw)
        except json.JSONDecodeError:
            result = {}
        if result.get("found"):
            return PredicateResult(True, p["kind"], f"text {needle!r} visible (OCR)",
                                   result.get("match"))
        ocr_detail = "OCR did not find it"
    # OCR fallback: a11y / Win32 GetWindowText reads the focused
    # window's text directly, which is far more reliable than OCR for
    # text fields (Notepad, Excel cells, browser address bars).
    # Fires whether or not OCR was tried — both passes increase the
    # chance of catching real "text on screen" cases that OCR misses.
    if "desktop_get_window_text" in tools:
        try:
            raw = await registry.dispatch("desktop_get_window_text", {})
            result = json.loads(raw)
        except (json.JSONDecodeError, Exception):
            result = {}
        # The tool returns either {"text": "..."} or {"windows": [{"text": "..."}, ...]}
        # depending on backend; check both shapes.
        candidates: list[str] = []
        if isinstance(result.get("text"), str):
            candidates.append(result["text"])
        for w in result.get("windows") or []:
            if isinstance(w, dict) and isinstance(w.get("text"), str):
                candidates.append(w["text"])
        for body in candidates:
            if needle_lc in body.lower():
                return PredicateResult(
                    True, p["kind"],
                    f"text {needle!r} visible (read via desktop_get_window_text)",
                    body[:160],
                )
        if not ocr_detail:
            ocr_detail = "no OCR backend available"
        return PredicateResult(
            False, p["kind"],
            f"text {needle!r} not on screen ({ocr_detail}; window-text read also missed)",
            {"ocr": ocr_detail, "window_text_candidates": len(candidates)},
        )
    if ocr_detail:
        return PredicateResult(False, p["kind"], f"text {needle!r} not on screen ({ocr_detail})",
                               {"ocr": ocr_detail})
    return PredicateResult(False, p["kind"],
                           "no OCR or window-text backend available for text-visible check")


def _is_wsl_runtime() -> bool:
    """True when running under WSL (kernel release contains 'microsoft')."""
    import platform as _p
    if _p.system() != "Linux":
        return False
    try:
        with open("/proc/version", "r", encoding="utf-8") as f:
            return "microsoft" in f.read().lower()
    except OSError:
        return False


async def _run_process_query(needle: str, cmd: str, registry, kind: str) -> tuple[bool, str]:
    """Dispatch ``cmd`` via shell_run and return (found, stdout_head).

    Helper for _check_process so we can probe multiple shells (POSIX
    pgrep + Windows tasklist via WSL interop) and stop at the first
    success without duplicating the dispatch / parse logic.
    """
    raw = await registry.dispatch("shell_run", {"command": cmd})
    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        return False, ""
    out = (result.get("stdout") or "")
    return needle.lower() in out.lower(), out[:160]


async def _check_process(p: dict, registry) -> PredicateResult:
    needle = str(p.get("value") or "")
    if not needle:
        return PredicateResult(False, p["kind"], "empty value")
    has_meta = any(ch in needle for ch in '"&|<>%^')
    tools = set(registry.list_tools())

    import platform as _p
    candidates = [needle]
    if not needle.lower().endswith(".exe"):
        candidates.append(needle + ".exe")
    is_windows = _p.system() == "Windows"
    is_wsl = _is_wsl_runtime()

    out_head = ""
    if "shell_run" in tools:
        if is_windows:
            if has_meta:
                return PredicateResult(
                    False, p["kind"],
                    f"refusing to query process: needle {needle!r} contains shell metacharacters",
                )
            # tasklist's /FI "IMAGENAME eq <name>" is EXACT-match (no
            # glob); try bare + ".exe" candidates, then fall back to
            # tasklist | findstr for case-insensitive substring match.
            for cand in candidates:
                cmd = f'tasklist /FI "IMAGENAME eq {cand}"'
                found, out_head = await _run_process_query(needle, cmd, registry, p["kind"])
                if found:
                    return PredicateResult(True, p["kind"], "process found", out_head)
            cmd = f'tasklist | findstr /I "{needle}"'
            found, out_head = await _run_process_query(needle, cmd, registry, p["kind"])
            if found:
                return PredicateResult(True, p["kind"],
                                       "process found (via findstr)", out_head)
        else:
            # POSIX (including WSL Linux side first).
            pgrep_cmd = f"pgrep -lf -- {shlex.quote(needle)}"
            found, out_head = await _run_process_query(needle, pgrep_cmd, registry, p["kind"])
            if found:
                return PredicateResult(True, p["kind"], "process found (Linux)", out_head)

            # WSL: a Windows GUI app like notepad.exe doesn't show up
            # in pgrep — it's a Windows process invisible to the Linux
            # kernel.  Probe tasklist.exe via interop with the SAME
            # exact-match candidate list used on Windows native.
            if is_wsl and not has_meta:
                for tasklist_bin in ("tasklist.exe", "/mnt/c/Windows/System32/tasklist.exe"):
                    for cand in candidates:
                        win_cmd = f'{tasklist_bin} /FI "IMAGENAME eq {cand}"'
                        found, tl_head = await _run_process_query(needle, win_cmd, registry, p["kind"])
                        if found:
                            return PredicateResult(
                                True, p["kind"],
                                "process found (Windows via interop)", tl_head,
                            )

    # Final fallback: cross-check the window list.  Many GUI apps the
    # model expects to "be running" register a window long before they
    # show up in tasklist (UWP apps run under ApplicationFrameHost,
    # browser tabs run under different process names, etc.).  If
    # ANY visible window has app or title containing the needle
    # (case-insensitive substring match), accept that as evidence the
    # process is up.  This makes the predicate work even when shell_run
    # is disabled or every shell-based probe missed.
    if "desktop_list_windows" in tools:
        try:
            wlist_raw = await registry.dispatch("desktop_list_windows", {})
            wlist = json.loads(wlist_raw)
        except (json.JSONDecodeError, Exception):
            wlist = {}
        # Strip the .exe suffix when matching window app/title so
        # "notepad.exe" the predicate matches the "Notepad" window
        # title and the "notepad.exe" app name from the windows list.
        bare = needle.lower().removesuffix(".exe")
        for w in wlist.get("windows") or []:
            if not isinstance(w, dict):
                continue
            app = str(w.get("app") or "").lower()
            title = str(w.get("title") or "").lower()
            if bare and (bare in app or bare in title):
                return PredicateResult(
                    True, p["kind"],
                    "process found (matched a visible window's app/title)",
                    f"window={w.get('title','')[:80]!r} app={w.get('app','')!r}",
                )

    if "shell_run" not in tools and "desktop_list_windows" not in tools:
        return PredicateResult(False, p["kind"], "no shell or window-list backend available")

    return PredicateResult(False, p["kind"],
                           f"no process matching {needle!r} found "
                           "(checked shell tasklist/pgrep + window list)",
                           out_head)


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
           "check_filesystem_predicate_sync", "PREDICATE_KINDS"]
