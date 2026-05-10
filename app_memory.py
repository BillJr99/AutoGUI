"""
app_memory.py — Per-app quirk / strategy memory.

Skills capture what *worked*; this captures what *didn't* and why.
After a step fails the controller records the failure class against
the active app/window's name; a follow-up planner pass reads the same
record and reorders click strategies accordingly (e.g. "Slack: prefer
desktop_click_text over desktop_click_element — a11y has a 4/5 failure
rate").

The store is intentionally small: per app, a rolling histogram of
failure classes per tool, plus the last N free-form notes the model
chose to attach via the ``memory_note`` tool.  No PII, no command
text, no file bodies — only structured metadata so the file is safe
to keep across machines if the user wants.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


_MAX_NOTES_PER_APP = 20


def _normalize_app(app: str) -> str:
    """
    Reduce 'C:\\Program Files\\Microsoft\\Excel.EXE' /
    '/Applications/Slack.app' / 'msedge' to a stable lowercase stem.
    """
    if not app:
        return ""
    base = app.strip().rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    base = re.sub(r"\.(exe|app|dmg)$", "", base, flags=re.IGNORECASE)
    return base.lower()


@dataclass
class AppRecord:
    app: str
    failure_counts: dict = field(default_factory=dict)
    success_counts: dict = field(default_factory=dict)
    last_failures: list = field(default_factory=list)   # rolling [{tool,class,reason,ts}]
    notes: list = field(default_factory=list)           # rolling [{text,ts,tag}]
    updated: float = field(default_factory=time.time)


class AppMemory:
    """
    JSON-per-app store.  ``<dir>/<app>.json`` contains one ``AppRecord``;
    ``<dir>/index.jsonl`` lists every app the user has interacted with.

    All writes are atomic (tmp + rename) so a crash mid-update doesn't
    leave a half-written file.
    """

    def __init__(self, directory: str = "memory"):
        self._dir = Path(directory).expanduser()
        self._dir.mkdir(parents=True, exist_ok=True)
        self._index_path = self._dir / "index.jsonl"

    # ------------------------------------------------------------------
    # I/O
    # ------------------------------------------------------------------

    def _path_for(self, app: str) -> Path:
        slug = re.sub(r"[^a-z0-9._-]+", "_", _normalize_app(app)) or "_unknown"
        return self._dir / f"{slug}.json"

    def _load(self, app: str) -> AppRecord:
        p = self._path_for(app)
        if not p.exists():
            return AppRecord(app=_normalize_app(app))
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            return AppRecord(**data)
        except (OSError, json.JSONDecodeError, TypeError) as e:
            logger.warning("[app_memory] load failed for %s: %s", p, e)
            return AppRecord(app=_normalize_app(app))

    def _save(self, rec: AppRecord) -> None:
        p = self._path_for(rec.app)
        tmp = p.with_suffix(p.suffix + ".tmp")
        try:
            tmp.write_text(json.dumps(asdict(rec), ensure_ascii=False, indent=2),
                           encoding="utf-8")
            os.replace(tmp, p)
        except OSError as e:
            logger.warning("[app_memory] save failed for %s: %s", p, e)
            return
        try:
            with self._index_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"app": rec.app, "updated": rec.updated}) + "\n")
        except OSError:
            pass

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def record_failure(self, *, app: str, tool: str, failure_class: str, reason: str = "") -> None:
        if not app:
            return
        rec = self._load(app)
        key = f"{tool}:{failure_class}"
        rec.failure_counts[key] = rec.failure_counts.get(key, 0) + 1
        rec.last_failures.append({
            "tool": tool, "class": failure_class,
            "reason": (reason or "")[:160], "ts": time.time(),
        })
        rec.last_failures = rec.last_failures[-_MAX_NOTES_PER_APP:]
        rec.updated = time.time()
        self._save(rec)

    def record_success(self, *, app: str, tool: str) -> None:
        if not app or not tool:
            return
        rec = self._load(app)
        rec.success_counts[tool] = rec.success_counts.get(tool, 0) + 1
        rec.updated = time.time()
        self._save(rec)

    def add_note(self, *, app: str, text: str, tag: str = "") -> None:
        if not app or not text:
            return
        rec = self._load(app)
        rec.notes.append({"text": text[:400], "tag": tag[:40], "ts": time.time()})
        rec.notes = rec.notes[-_MAX_NOTES_PER_APP:]
        rec.updated = time.time()
        self._save(rec)

    def get(self, app: str) -> dict:
        rec = self._load(app)
        return asdict(rec)

    def hint_for_planner(self, app: str) -> str:
        """
        Compact one-paragraph summary the planner can paste into the
        per-task prompt: which strategies have worked, which keep
        failing, plus any free-form notes.
        """
        rec = self._load(app)
        if (not rec.failure_counts and not rec.success_counts
                and not rec.notes):
            return ""
        lines = [f"App memory for {rec.app!r}:"]
        if rec.success_counts:
            wins = sorted(rec.success_counts.items(), key=lambda kv: -kv[1])[:5]
            lines.append("  reliable tools: " + ", ".join(
                f"{t}({c})" for t, c in wins
            ))
        if rec.failure_counts:
            losses = sorted(rec.failure_counts.items(), key=lambda kv: -kv[1])[:5]
            lines.append("  recent failure classes: " + ", ".join(
                f"{k}({c})" for k, c in losses
            ))
        for n in rec.notes[-3:]:
            text = n.get("text", "")
            if text:
                lines.append(f"  note: {text[:160]}")
        return "\n".join(lines)

    def list_apps(self) -> list[str]:
        return sorted(p.stem for p in self._dir.glob("*.json"))


__all__ = ["AppMemory", "AppRecord"]
