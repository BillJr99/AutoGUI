"""Preflight inference + check coverage (filesystem branch)."""

from __future__ import annotations

import asyncio

import pytest

from preflight import (
    PreflightCheck,
    infer_checks_from_plan,
    run_preflight,
)


def test_infer_from_predicate_paths():
    plan = {
        "steps": [
            {"id": "s1", "predicate": {"kind": "file_exists", "path": "/tmp/a"}},
            {"id": "s2", "predicate": {"kind": "file_contains",
                                        "path": "/tmp/b", "value": "x"}},
        ],
    }
    checks = infer_checks_from_plan(plan)
    targets = {(c.kind, c.target) for c in checks}
    assert ("file", "/tmp/a") in targets
    assert ("file", "/tmp/b") in targets


def test_infer_explicit_preflight_block():
    plan = {
        "preflight": [
            {"kind": "app", "target": "vim"},
            {"kind": "url", "target": "https://example.com"},
        ],
        "steps": [],
    }
    checks = infer_checks_from_plan(plan)
    targets = {(c.kind, c.target) for c in checks}
    assert ("app", "vim") in targets
    assert ("url", "https://example.com") in targets


def test_dedup_same_target():
    plan = {
        "preflight": [{"kind": "file", "target": "/tmp/x"}],
        "steps": [{"id": "s1",
                   "predicate": {"kind": "file_exists", "path": "/tmp/x"}}],
    }
    checks = infer_checks_from_plan(plan)
    assert len(checks) == 1


@pytest.mark.asyncio
async def test_run_file_check(tmp_path):
    f = tmp_path / "exists.txt"
    f.write_text("hi")
    report = await run_preflight([
        PreflightCheck("file", str(f)),
        PreflightCheck("file", str(tmp_path / "missing")),
    ])
    assert len(report.results) == 2
    assert report.results[0].ok is True
    assert report.results[1].ok is False
    assert report.all_passed is False


@pytest.mark.asyncio
async def test_run_app_check_present():
    # python is essentially guaranteed to resolve on PATH wherever pytest runs.
    report = await run_preflight([PreflightCheck("app", "python")])
    assert report.results[0].ok is True


@pytest.mark.asyncio
async def test_file_check_rejects_directory(tmp_path):
    """The ``file`` preflight kind should NOT pass for a directory."""
    d = tmp_path / "subdir"
    d.mkdir()
    report = await run_preflight([PreflightCheck("file", str(d))])
    assert report.results[0].ok is False
    assert "directory" in report.results[0].detail.lower()
