"""
trace.py — Per-session JSONL trajectory log.

Every AgentEvent the agent yields is also written to a session trace file so
the run can be:

  - inspected after the fact (what did the model decide and why?),
  - replayed via replay.py (re-run the tool calls deterministically),
  - mined for new skills.

One JSON object per line.  Records include a millisecond timestamp,
the event kind, the human-readable content, and the structured data
payload (which includes tool_name + args for tool_call events).
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)


class TraceWriter:
    """Append-only JSONL writer for AgentEvent records."""

    def __init__(self, dir_path: str = "logs/traces", session_id: str | None = None):
        self.session_id = session_id or (
            time.strftime("%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:8]
        )
        d = Path(dir_path).expanduser()
        d.mkdir(parents=True, exist_ok=True)
        self.path = d / f"{self.session_id}.jsonl"
        try:
            self._fh = self.path.open("a", encoding="utf-8")
        except OSError as e:
            logger.warning("[trace] Could not open %s: %s", self.path, e)
            self._fh = None

    def write_event(self, event) -> None:
        """Persist one AgentEvent.  Best-effort; errors are swallowed."""
        if self._fh is None:
            return
        record = {
            "t": time.time(),
            "session_id": self.session_id,
            "kind": getattr(event, "kind", "?"),
            "content": getattr(event, "content", ""),
            "data": getattr(event, "data", {}) or {},
        }
        try:
            self._fh.write(json.dumps(record, default=str, ensure_ascii=False) + "\n")
            self._fh.flush()
        except Exception as e:
            logger.debug("[trace] write failed: %s", e)

    def write_meta(self, **fields) -> None:
        """Write a 'meta' record (e.g. session start/end markers, config snapshot)."""
        if self._fh is None:
            return
        record = {"t": time.time(), "session_id": self.session_id, "kind": "meta", **fields}
        try:
            self._fh.write(json.dumps(record, default=str, ensure_ascii=False) + "\n")
            self._fh.flush()
        except Exception:
            pass

    def close(self):
        if self._fh is not None:
            try:
                self._fh.close()
            finally:
                self._fh = None

    def __del__(self):
        self.close()
