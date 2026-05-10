"""Per-app memory store coverage."""

from __future__ import annotations

from app_memory import AppMemory, _normalize_app


def test_read_only_store_creates_no_directory(tmp_path):
    """Default AppMemory(allow_writes=False) must not touch the disk."""
    target = tmp_path / "memory"
    mem = AppMemory(str(target))
    # All read paths return empty without raising or creating anything.
    assert mem.list_apps() == []
    rec = mem.get("anything")
    assert rec["app"] == "anything"
    assert rec["failure_counts"] == {}
    assert mem.hint_for_planner("anything") == ""
    assert not target.exists(), "directory must not be created on read-only access"


def test_read_only_store_silently_no_ops_writes(tmp_path):
    target = tmp_path / "memory"
    mem = AppMemory(str(target))
    mem.record_failure(app="vim", tool="t", failure_class="x")
    mem.record_success(app="vim", tool="t")
    mem.add_note(app="vim", text="hi")
    # No file written; the next read still returns empty.
    assert mem.get("vim")["failure_counts"] == {}
    assert not target.exists()


def test_writes_enabled_persists(tmp_path):
    target = tmp_path / "memory"
    mem = AppMemory(str(target), allow_writes=True)
    mem.record_failure(app="vim", tool="t", failure_class="x")
    assert target.exists()
    rec = mem.get("vim")
    assert rec["failure_counts"]["t:x"] == 1


def test_normalize_app_strips_path_and_extension():
    assert _normalize_app("C:\\Program Files\\Microsoft\\msedge.EXE") == "msedge"
    assert _normalize_app("/Applications/Slack.app") == "slack"
    assert _normalize_app("vim") == "vim"
    assert _normalize_app("") == ""


def test_record_failure_increments_counts(tmp_path):
    mem = AppMemory(str(tmp_path), allow_writes=True)
    mem.record_failure(app="slack", tool="desktop_click_element",
                       failure_class="missing_element")
    mem.record_failure(app="slack", tool="desktop_click_element",
                       failure_class="missing_element")
    rec = mem.get("slack")
    assert rec["failure_counts"]["desktop_click_element:missing_element"] == 2
    assert len(rec["last_failures"]) == 2


def test_record_success_increments_counts(tmp_path):
    mem = AppMemory(str(tmp_path), allow_writes=True)
    mem.record_success(app="vim", tool="desktop_type")
    rec = mem.get("vim")
    assert rec["success_counts"]["desktop_type"] == 1


def test_hint_for_planner_summarises(tmp_path):
    mem = AppMemory(str(tmp_path), allow_writes=True)
    mem.record_failure(app="slack", tool="desktop_click_element",
                       failure_class="missing_element")
    mem.record_success(app="slack", tool="desktop_click_text")
    mem.add_note(app="slack", text="ctrl+a does not select all in input box")
    hint = mem.hint_for_planner("slack")
    assert "slack" in hint
    assert "desktop_click_text" in hint
    assert "missing_element" in hint
    assert "ctrl+a" in hint


def test_hint_for_unknown_app_is_empty(tmp_path):
    mem = AppMemory(str(tmp_path), allow_writes=True)
    assert mem.hint_for_planner("unseen") == ""


def test_list_apps_after_writes(tmp_path):
    mem = AppMemory(str(tmp_path), allow_writes=True)
    mem.record_success(app="vim", tool="desktop_type")
    mem.record_success(app="msedge.exe", tool="browser_navigate")
    apps = set(mem.list_apps())
    assert {"vim", "msedge"} <= apps
