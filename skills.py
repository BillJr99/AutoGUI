"""
skills.py — Persistent skill (macro) library.

A "skill" is a named, ordered list of tool calls — typically captured from a
successful task — that can be replayed later either by the agent (when the
model decides the task matches) or by the standalone replay CLI (no LLM).

Storage is plain JSONL at ~/.autogui/skills.jsonl, one record per line:

    {"name": str,
     "keywords": [str, ...],
     "app": str,
     "steps": [{"tool": str, "args": dict}, ...],
     "created": float,        # unix timestamp
     "success_count": int}

The append-only format keeps the data trivially inspectable, mergeable, and
diffable without locking — good enough for a single-user agent.
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path

logger = logging.getLogger(__name__)


def _tokenize(text: str) -> list[str]:
    return [t for t in re.split(r"\W+", (text or "").lower()) if t and len(t) > 2]


# ---------------------------------------------------------------------------
# Step normalization
# ---------------------------------------------------------------------------

_ACTIVATE_TOOLS = frozenset({"desktop_launch", "desktop_activate_window"})
_TYPE_TOOLS = frozenset({"desktop_type", "desktop_hotkey"})


def normalize_skill_steps(steps: list[dict]) -> list[dict]:
    """Remove pixel-coordinate focus clicks sandwiched between a window-activation
    step and a type step.

    Saved skills sometimes include a desktop_click(x, y) immediately after
    desktop_activate_window or desktop_launch as a focus gesture.  On replay
    the window may open at a different screen position, so the hardcoded
    coordinates miss the window and steal focus before desktop_type fires.
    Dropping the click is safe because the preceding activate step already
    established focus.
    """
    drop: set[int] = set()
    for i, step in enumerate(steps):
        tool = step.get("tool", "")
        args = step.get("args") or {}
        if (
            tool == "desktop_click"
            and "x" in args and "y" in args
            and i > 0
            and i + 1 < len(steps)
        ):
            prev_tool = steps[i - 1].get("tool", "")
            next_tool = steps[i + 1].get("tool", "")
            if prev_tool in _ACTIVATE_TOOLS and next_tool in _TYPE_TOOLS:
                drop.add(i)
                logger.debug(
                    "[skills] normalize: dropping pixel focus-click at step %d (x=%s, y=%s)",
                    i, args.get("x"), args.get("y"),
                )
    return [s for i, s in enumerate(steps) if i not in drop]


class SkillStore:
    """JSONL-backed skill library with simple keyword retrieval."""

    def __init__(self, path: str = "~/.autogui/skills.jsonl"):
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.touch()

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def all(self) -> list[dict]:
        skills: list[dict] = []
        try:
            for line in self.path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    skills.append(json.loads(line))
                except json.JSONDecodeError:
                    logger.warning("[skills] Skipping malformed line in %s", self.path)
        except FileNotFoundError:
            return []
        return skills

    def get(self, name: str) -> dict | None:
        for s in self.all():
            if s.get("name") == name:
                return s
        return None

    def search(self, query: str, limit: int = 5) -> list[dict]:
        """Score by keyword overlap; ties broken by success_count then recency."""
        if not query:
            return self.all()[:limit]
        qtoks = set(_tokenize(query))
        if not qtoks:
            return []
        scored: list[tuple[int, int, float, dict]] = []
        for s in self.all():
            ktoks = set(_tokenize(" ".join(s.get("keywords", [])) + " " + s.get("name", "")))
            overlap = len(qtoks & ktoks)
            if overlap == 0 and query.lower() not in s.get("name", "").lower():
                continue
            scored.append((
                -overlap,
                -int(s.get("success_count", 0)),
                -float(s.get("created", 0.0)),
                s,
            ))
        scored.sort()
        return [s for *_, s in scored[:limit]]

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def save(
        self,
        *,
        name: str,
        keywords: list[str],
        app: str,
        steps: list[dict],
    ) -> dict:
        if not name:
            raise ValueError("Skill name is required")
        if not steps:
            raise ValueError("Cannot save a skill with no steps")
        # Replace any existing skill with the same name.
        existing = [s for s in self.all() if s.get("name") != name]
        skill = {
            "name": name,
            "keywords": list(keywords or []),
            "app": app or "",
            "steps": list(steps),
            "created": time.time(),
            "success_count": 0,
        }
        existing.append(skill)
        self._rewrite(existing)
        return skill

    def increment_success(self, name: str):
        skills = self.all()
        changed = False
        for s in skills:
            if s.get("name") == name:
                s["success_count"] = int(s.get("success_count", 0)) + 1
                changed = True
        if changed:
            self._rewrite(skills)

    def delete(self, name: str) -> bool:
        skills = self.all()
        kept = [s for s in skills if s.get("name") != name]
        if len(kept) == len(skills):
            return False
        self._rewrite(kept)
        return True

    def _rewrite(self, skills: list[dict]):
        # Atomic rewrite to avoid corrupting the file mid-write.
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            for s in skills:
                f.write(json.dumps(s, ensure_ascii=False) + "\n")
        tmp.replace(self.path)
