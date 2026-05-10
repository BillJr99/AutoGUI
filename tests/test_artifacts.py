"""Round-trip and lazy-create tests for the artifact store."""

from __future__ import annotations

import os

from artifacts import ArtifactStore


def test_round_trip_short_body(tmp_path):
    store = ArtifactStore(str(tmp_path))
    aid = store.put("hello world", kind="fs_read", source="/tmp/x")
    assert aid.startswith("artifact://")
    assert store.has(aid)
    assert store.get_body(aid) == "hello world"


def test_large_body_persists_to_disk(tmp_path):
    store = ArtifactStore(str(tmp_path))
    body = "x" * 8000
    aid = store.put(body, kind="shell_stdout", source="echo")
    art = store.get(aid)
    assert art is not None
    assert art.body_path  # should live on disk, not inline
    assert os.path.exists(art.body_path)
    assert store.get_body(aid) == body


def test_list_recent_filters_by_kind(tmp_path):
    store = ArtifactStore(str(tmp_path))
    store.put("a", kind="fs_read", source="a")
    store.put("b", kind="shell_stdout", source="b")
    store.put("c", kind="fs_read", source="c")
    fs = store.list_recent(kind="fs_read")
    assert {a.source for a in fs} == {"a", "c"}


def test_summary_includes_source_and_kind(tmp_path):
    store = ArtifactStore(str(tmp_path))
    aid = store.put("config = 1", kind="fs_read", source="conf.txt")
    summary = store.summarize(aid)
    assert "fs_read" in summary
    assert "conf.txt" in summary or "config" in summary


def test_unknown_id_returns_none(tmp_path):
    store = ArtifactStore(str(tmp_path))
    assert store.get_body("artifact://nope") is None


def test_load_skips_incompatible_index_records(tmp_path):
    """Forward-version / corrupted index lines must not crash startup —
    the loader should skip the bad line and load every recoverable one."""
    import json
    # Pre-populate index.jsonl with a mix of: valid, malformed JSON,
    # non-object JSON, and a forward-version record carrying an
    # unexpected field that breaks the dataclass.
    tmp_path.mkdir(parents=True, exist_ok=True)
    index = tmp_path / "index.jsonl"
    valid = {
        "id": "artifact://aaaaaaaa", "kind": "fs_read", "source": "ok.txt",
        "summary": "ok", "bytes_len": 2, "created": 1.0, "meta": {},
        "body_path": "", "body_inline": "ok",
    }
    forward = dict(valid)
    forward["id"] = "artifact://bbbbbbbb"
    forward["future_field"] = "extra"   # raises TypeError on Artifact(**)
    not_object = "[\"this is\", \"a list, not a record\"]"
    lines = [
        json.dumps(valid),
        "{not valid json",
        not_object,
        json.dumps(forward),
    ]
    index.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # Should not raise; should load the one valid record and skip the rest.
    store = ArtifactStore(str(tmp_path))
    assert store.has("artifact://aaaaaaaa")
    assert not store.has("artifact://bbbbbbbb")
