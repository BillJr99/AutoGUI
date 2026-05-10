"""
artifacts.py — Stable-id artifact store (NOT content-addressed).

Large observations (file bodies, OCR snippets, command stdout, accessibility
trees) bloat the conversation history if pasted in full on every turn.  The
artifact store gives each capture a short opaque id so the executor can
reference an observation by id and only fetch the body when it actually
needs to look at it.

ID format: ``artifact://<8-hex>``.  IDs are deliberately *not* a pure
content hash: each capture mixes in a high-resolution timestamp, so reading
the same file twice produces two distinct ids and two distinct records.
That suits an append-only per-task observation log — the model can
reason about "what did I see at step 3 vs step 7" without earlier
records being silently overwritten by later identical reads.  Use
``ArtifactStore.list_recent`` if you want to find prior captures of the
same source.

Bodies are stored on disk under a configurable directory; metadata lives
in an in-memory map plus a JSONL sidecar for crash recovery.

Typical use
-----------

    store = ArtifactStore("logs/artifacts")
    aid = store.put("<long file body>",
                    kind="fs_read", source="config.txt", meta={"size": 2048})
    # Later, agent decides it actually needs the content:
    body = store.get_body(aid)
    summary = store.summarize(aid)  # one-line description for the LLM

This module is deliberately dependency-free (stdlib only) so it can be
imported by the agent loop without changing requirements.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger(__name__)


# Pasted bodies above this size are written to disk as separate files; smaller
# bodies are kept inline in the metadata file.  Either way, the body is NOT
# put back into the conversation history unless explicitly fetched.
_INLINE_BODY_LIMIT = 4096


@dataclass
class Artifact:
    """A single stored observation."""

    id: str                                  # "artifact://<8-hex>"
    kind: str                                # e.g. "fs_read", "shell_stdout"
    source: str = ""                         # path, URL, command, etc.
    summary: str = ""                        # one-line human description
    bytes_len: int = 0
    created: float = field(default_factory=time.time)
    meta: dict[str, Any] = field(default_factory=dict)
    body_path: str = ""                      # set when body lives on disk
    body_inline: str = ""                    # set when body is small enough


class ArtifactStore:
    """
    Append-only, JSONL-backed artifact store with timestamp-salted ids.

    Bodies above ``_INLINE_BODY_LIMIT`` characters are written to a sibling
    file (``<id_prefix>.txt``) so the metadata sidecar stays cheap to load.
    Smaller bodies are kept inline.  ``get_body`` is the single read path —
    it transparently reads from disk when needed.

    IDs are NOT content hashes (see module docstring): every ``put`` call
    returns a fresh id even when the body is byte-identical to a prior
    capture.  This lets the model reason about WHEN something was
    observed, at the cost of giving up dedup.
    """

    def __init__(self, directory: str = "logs/artifacts"):
        self._dir = Path(directory).expanduser()
        self._dir.mkdir(parents=True, exist_ok=True)
        self._index_path = self._dir / "index.jsonl"
        self._artifacts: dict[str, Artifact] = {}
        self._load()

    # ------------------------------------------------------------------
    # I/O
    # ------------------------------------------------------------------

    def _load(self) -> None:
        if not self._index_path.exists():
            return
        try:
            for line in self._index_path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    logger.warning("[artifacts] skipping malformed index line")
                    continue
                aid = record.get("id")
                if not aid:
                    continue
                self._artifacts[aid] = Artifact(**record)
        except OSError as e:
            logger.warning("[artifacts] could not load index: %s", e)

    def _append_index(self, art: Artifact) -> None:
        try:
            with self._index_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(asdict(art), ensure_ascii=False) + "\n")
        except OSError as e:
            logger.warning("[artifacts] could not write index: %s", e)

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def put(
        self,
        body: str,
        *,
        kind: str,
        source: str = "",
        summary: str = "",
        meta: dict[str, Any] | None = None,
    ) -> str:
        """Store ``body`` and return its artifact id.

        The id is a short opaque sha1 prefix derived from
        kind|source|timestamp|body-prefix.  Identical bodies stored
        moments apart receive *different* ids — the store is a log of
        captures, not a content-addressed cache.
        """
        body = body if isinstance(body, str) else str(body)
        h = hashlib.sha1(
            f"{kind}|{source}|{time.time_ns()}|{body[:200]}".encode("utf-8")
        ).hexdigest()[:8]
        aid = f"artifact://{h}"

        art = Artifact(
            id=aid,
            kind=kind,
            source=source,
            summary=summary or self._auto_summary(kind, source, body),
            bytes_len=len(body.encode("utf-8")),
            meta=dict(meta or {}),
        )
        if len(body) <= _INLINE_BODY_LIMIT:
            art.body_inline = body
        else:
            body_file = self._dir / f"{h}.txt"
            try:
                body_file.write_text(body, encoding="utf-8")
                art.body_path = str(body_file)
            except OSError as e:
                logger.warning("[artifacts] body write failed; storing inline truncated: %s", e)
                art.body_inline = body[:_INLINE_BODY_LIMIT]

        self._artifacts[aid] = art
        self._append_index(art)
        return aid

    def has(self, aid: str) -> bool:
        return aid in self._artifacts

    def get(self, aid: str) -> Artifact | None:
        return self._artifacts.get(aid)

    def get_body(self, aid: str) -> str | None:
        """Return the body or ``None`` when the artifact is unknown."""
        art = self._artifacts.get(aid)
        if art is None:
            return None
        if art.body_inline:
            return art.body_inline
        if art.body_path:
            try:
                return Path(art.body_path).read_text(encoding="utf-8")
            except OSError as e:
                logger.warning("[artifacts] body read failed for %s: %s", aid, e)
                return None
        return ""

    def summarize(self, aid: str) -> str:
        art = self._artifacts.get(aid)
        if art is None:
            return f"{aid} (unknown)"
        return f"{aid} [{art.kind}] {art.summary} ({art.bytes_len}B)"

    def list_recent(self, *, kind: str | None = None, limit: int = 20) -> list[Artifact]:
        items: Iterable[Artifact] = self._artifacts.values()
        if kind:
            items = (a for a in items if a.kind == kind)
        return sorted(items, key=lambda a: a.created, reverse=True)[:limit]

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _auto_summary(kind: str, source: str, body: str) -> str:
        """One-line description for an artifact when caller didn't supply one."""
        first = (body or "").splitlines()[:1]
        preview = first[0].strip() if first else ""
        if len(preview) > 120:
            preview = preview[:117] + "..."
        if source:
            return f"{kind} of {source}: {preview}"
        return f"{kind}: {preview}" if preview else kind
