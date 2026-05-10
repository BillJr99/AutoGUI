"""
progress.py — Persistent task progress markers.

Long, multi-step tasks should be resumable: a context reset, manual abort,
or process crash should not throw away the steps that already succeeded.
The progress store persists ``{task_id, plan, completed_step_ids,
checkpoint_data}`` to disk on every plan-step status change so the
controller can pick up exactly where it left off.

Format: one JSON file per task under ``logs/progress/<task_id>.json``.
The file is atomically replaced on each write to avoid half-written
state.  An ``index.jsonl`` lists every task ever seen so the user (or
the controller) can list resumable tasks.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class TaskProgress:
    """In-memory mirror of a persisted task progress record."""

    task_id: str
    user_input: str
    created: float = field(default_factory=time.time)
    updated: float = field(default_factory=time.time)
    completed_step_ids: list[str] = field(default_factory=list)
    failed_step_ids: list[str] = field(default_factory=list)
    plan_snapshot: dict[str, Any] = field(default_factory=dict)
    checkpoint_data: dict[str, Any] = field(default_factory=dict)
    status: str = "running"   # running | done | failed | abandoned


class ProgressStore:
    """
    Disk-backed task progress.

    Methods are intentionally narrow: the controller calls
    ``open_task`` once per user request (it returns an existing record
    when the task_id matches an unfinished task), then ``mark_done`` /
    ``mark_failed`` as steps complete, and ``finalize`` when the whole
    task ends.
    """

    def __init__(self, directory: str = "logs/progress"):
        self._dir = Path(directory).expanduser()
        self._dir.mkdir(parents=True, exist_ok=True)
        self._index_path = self._dir / "index.jsonl"

    # ------------------------------------------------------------------
    # ID helper — derive a stable id from the task text so re-running the
    # same task picks up the previous record.
    # ------------------------------------------------------------------

    @staticmethod
    def derive_task_id(user_input: str) -> str:
        normalised = " ".join((user_input or "").split())[:512]
        return hashlib.sha1(normalised.encode("utf-8")).hexdigest()[:12]

    # ------------------------------------------------------------------
    # I/O
    # ------------------------------------------------------------------

    def _path_for(self, task_id: str) -> Path:
        return self._dir / f"{task_id}.json"

    def _atomic_write(self, path: Path, data: dict[str, Any]) -> None:
        tmp = path.with_suffix(path.suffix + ".tmp")
        try:
            tmp.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(tmp, path)
        except OSError as e:
            logger.warning("[progress] write failed for %s: %s", path, e)

    def _append_index(self, record: TaskProgress) -> None:
        line = {
            "task_id": record.task_id,
            "user_input": record.user_input[:160],
            "updated": record.updated,
            "status": record.status,
        }
        try:
            with self._index_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(line, ensure_ascii=False) + "\n")
        except OSError as e:
            logger.warning("[progress] index append failed: %s", e)

    def load(self, task_id: str) -> TaskProgress | None:
        p = self._path_for(task_id)
        if not p.exists():
            return None
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("[progress] could not parse %s: %s", p, e)
            return None
        try:
            return TaskProgress(**data)
        except TypeError as e:
            logger.warning("[progress] schema mismatch in %s: %s", p, e)
            return None

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def open_task(self, user_input: str) -> TaskProgress:
        """
        Return an existing in-progress record for the same task text, or
        start a new one.  Finalised records (``done`` / ``failed`` /
        ``abandoned``) never resume — the user explicitly re-runs them.
        """
        task_id = self.derive_task_id(user_input)
        existing = self.load(task_id)
        if existing and existing.status == "running":
            existing.updated = time.time()
            return existing

        record = TaskProgress(task_id=task_id, user_input=user_input)
        self._atomic_write(self._path_for(task_id), asdict(record))
        self._append_index(record)
        return record

    def update_plan_snapshot(self, record: TaskProgress, snapshot: dict[str, Any]) -> None:
        record.plan_snapshot = dict(snapshot or {})
        record.updated = time.time()
        self._atomic_write(self._path_for(record.task_id), asdict(record))

    def mark_done(self, record: TaskProgress, step_id: str) -> None:
        if step_id and step_id not in record.completed_step_ids:
            record.completed_step_ids.append(step_id)
        record.updated = time.time()
        self._atomic_write(self._path_for(record.task_id), asdict(record))

    def mark_failed(self, record: TaskProgress, step_id: str) -> None:
        if step_id and step_id not in record.failed_step_ids:
            record.failed_step_ids.append(step_id)
        record.updated = time.time()
        self._atomic_write(self._path_for(record.task_id), asdict(record))

    def update_checkpoint(self, record: TaskProgress, data: dict[str, Any]) -> None:
        """Free-form checkpoint — used for "tab N of M done" style markers."""
        record.checkpoint_data.update(data or {})
        record.updated = time.time()
        self._atomic_write(self._path_for(record.task_id), asdict(record))

    def finalize(self, record: TaskProgress, *, status: str) -> None:
        record.status = status
        record.updated = time.time()
        self._atomic_write(self._path_for(record.task_id), asdict(record))
        self._append_index(record)

    def list_resumable(self) -> list[TaskProgress]:
        out: list[TaskProgress] = []
        for path in sorted(self._dir.glob("*.json")):
            if path.name == "index.jsonl":
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                rec = TaskProgress(**data)
            except Exception:  # pragma: no cover — best-effort listing
                continue
            if rec.status == "running":
                out.append(rec)
        out.sort(key=lambda r: r.updated, reverse=True)
        return out
