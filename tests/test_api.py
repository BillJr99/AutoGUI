"""
test_api.py — pytest tests for the FastAPI REST API in api.py.

All tests run with AUTOGUI_DRY_RUN=true (set in pytest env or CI) so the
DryRunAgent is used instead of the real agent, meaning no display,
OpenWebUI instance, or real desktop is required.
"""

from __future__ import annotations

import asyncio
import os
import time

import pytest

# Force dry-run mode before api.py is imported (it reads env at module load).
os.environ.setdefault("AUTOGUI_DRY_RUN", "true")
os.environ.setdefault("AUTOGUI_CONFIG", "")  # no config file needed

from fastapi.testclient import TestClient  # noqa: E402 — must come after env setup
from api import app, TASKS  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fresh_client() -> TestClient:
    """Return a TestClient with a clean TASKS store."""
    TASKS.clear()
    return TestClient(app, raise_server_exceptions=True)


def _wait_for_status(client: TestClient, task_id: str, target_statuses: set[str], timeout: float = 5.0) -> dict:
    """
    Poll GET /api/task/{task_id} until status is one of *target_statuses*.
    Returns the final response JSON.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        r = client.get(f"/api/task/{task_id}")
        assert r.status_code == 200
        data = r.json()
        if data["status"] in target_statuses:
            return data
        time.sleep(0.05)
    pytest.fail(f"Task {task_id!r} did not reach {target_statuses} within {timeout}s; last status={data['status']}")


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

class TestHealthz:
    def test_returns_200(self):
        client = _fresh_client()
        r = client.get("/api/healthz")
        assert r.status_code == 200

    def test_ok_true(self):
        client = _fresh_client()
        data = client.get("/api/healthz").json()
        assert data["ok"] is True

    def test_has_version(self):
        client = _fresh_client()
        data = client.get("/api/healthz").json()
        assert "version" in data
        assert isinstance(data["version"], str)

    def test_has_uptime(self):
        client = _fresh_client()
        data = client.get("/api/healthz").json()
        assert "uptime_s" in data
        assert data["uptime_s"] >= 0


# ---------------------------------------------------------------------------
# Capabilities
# ---------------------------------------------------------------------------

class TestCapabilities:
    def test_returns_200(self):
        client = _fresh_client()
        r = client.get("/api/capabilities")
        assert r.status_code == 200

    def test_ok_true(self):
        client = _fresh_client()
        data = client.get("/api/capabilities").json()
        assert data["ok"] is True

    def test_has_dry_run_flag(self):
        client = _fresh_client()
        data = client.get("/api/capabilities").json()
        assert "dry_run" in data
        # We set AUTOGUI_DRY_RUN=true at module load
        assert data["dry_run"] is True

    def test_has_surfaces(self):
        client = _fresh_client()
        data = client.get("/api/capabilities").json()
        assert "surfaces" in data
        surfaces = data["surfaces"]
        assert isinstance(surfaces, dict)
        for key in ("desktop", "shell", "browser"):
            assert key in surfaces, f"missing surfaces.{key}"
            assert isinstance(surfaces[key], bool)


# ---------------------------------------------------------------------------
# Tools list
# ---------------------------------------------------------------------------

class TestTools:
    def test_returns_200(self):
        client = _fresh_client()
        r = client.get("/api/tools")
        assert r.status_code == 200

    def test_ok_true(self):
        client = _fresh_client()
        data = client.get("/api/tools").json()
        assert data["ok"] is True

    def test_has_tools_list(self):
        client = _fresh_client()
        data = client.get("/api/tools").json()
        assert "tools" in data
        assert isinstance(data["tools"], list)

    def test_dry_run_returns_empty_list(self):
        """In dry-run mode no real ToolRegistry is built — list must be empty."""
        client = _fresh_client()
        data = client.get("/api/tools").json()
        # api.py returns [] in DRY_RUN mode
        assert data["tools"] == []


# ---------------------------------------------------------------------------
# POST /api/task — task creation
# ---------------------------------------------------------------------------

class TestCreateTask:
    def test_returns_202(self):
        client = _fresh_client()
        r = client.post("/api/task", json={"task": "do something"})
        assert r.status_code == 202

    def test_ok_true(self):
        client = _fresh_client()
        data = client.post("/api/task", json={"task": "do something"}).json()
        assert data["ok"] is True

    def test_returns_task_id(self):
        client = _fresh_client()
        data = client.post("/api/task", json={"task": "do something"}).json()
        assert "task_id" in data
        assert isinstance(data["task_id"], str)
        assert len(data["task_id"]) > 0

    def test_each_task_gets_unique_id(self):
        client = _fresh_client()
        id1 = client.post("/api/task", json={"task": "task one"}).json()["task_id"]
        id2 = client.post("/api/task", json={"task": "task two"}).json()["task_id"]
        assert id1 != id2

    def test_empty_task_returns_error(self):
        client = _fresh_client()
        r = client.post("/api/task", json={"task": ""})
        data = r.json()
        assert data["ok"] is False
        assert "error" in data

    def test_whitespace_only_task_returns_error(self):
        client = _fresh_client()
        r = client.post("/api/task", json={"task": "   "})
        data = r.json()
        assert data["ok"] is False

    def test_dry_run_flag_in_request(self):
        """Explicit dry_run=true in the request body should also succeed."""
        client = _fresh_client()
        r = client.post("/api/task", json={"task": "do something", "dry_run": True})
        assert r.status_code == 202
        assert r.json()["ok"] is True

    def test_task_appears_in_store(self):
        """After creation, GET /api/task/{id} must return the task."""
        client = _fresh_client()
        task_id = client.post("/api/task", json={"task": "store test"}).json()["task_id"]
        r = client.get(f"/api/task/{task_id}")
        assert r.status_code == 200
        data = r.json()
        assert data["task_id"] == task_id
        assert data["task"] == "store test"


# ---------------------------------------------------------------------------
# GET /api/task/{task_id} — task status
# ---------------------------------------------------------------------------

class TestGetTask:
    def test_returns_200_for_existing_task(self):
        client = _fresh_client()
        task_id = client.post("/api/task", json={"task": "status test"}).json()["task_id"]
        r = client.get(f"/api/task/{task_id}")
        assert r.status_code == 200

    def test_returns_404_for_unknown_task(self):
        client = _fresh_client()
        r = client.get("/api/task/nonexistent-task-id")
        assert r.status_code == 404

    def test_404_detail_has_error_shape(self):
        client = _fresh_client()
        detail = client.get("/api/task/no-such-id").json()
        # FastAPI wraps HTTPException detail under "detail"
        assert "detail" in detail
        err = detail["detail"]
        assert err["ok"] is False
        assert "error" in err

    def test_task_has_expected_fields(self):
        client = _fresh_client()
        task_id = client.post("/api/task", json={"task": "field check"}).json()["task_id"]
        data = client.get(f"/api/task/{task_id}").json()
        for field in ("task_id", "task", "status", "steps", "started_at", "finished_at"):
            assert field in data, f"missing field: {field}"

    def test_task_status_is_valid(self):
        client = _fresh_client()
        task_id = client.post("/api/task", json={"task": "valid status"}).json()["task_id"]
        data = client.get(f"/api/task/{task_id}").json()
        valid_statuses = {"pending", "running", "done", "error", "cancelled"}
        assert data["status"] in valid_statuses

    def test_task_completes_in_dry_run(self):
        """DryRunAgent finishes quickly; wait for done/error status.

        Uses a context-manager TestClient so Starlette's anyio event loop
        stays alive for the duration of the test, preventing the background
        asyncio task from being spuriously cancelled when the client is GC'd.
        """
        TASKS.clear()
        with TestClient(app, raise_server_exceptions=True) as client:
            task_id = client.post("/api/task", json={"task": "complete me"}).json()["task_id"]
            data = _wait_for_status(client, task_id, {"done", "error"}, timeout=10.0)
            assert data["status"] == "done"

    def test_completed_task_has_steps(self):
        """After completion, steps list should be non-empty."""
        client = _fresh_client()
        task_id = client.post("/api/task", json={"task": "steps check"}).json()["task_id"]
        data = _wait_for_status(client, task_id, {"done", "error", "cancelled"})
        assert isinstance(data["steps"], list)
        assert len(data["steps"]) > 0

    def test_completed_task_has_timestamps(self):
        """started_at and finished_at should be set after completion."""
        client = _fresh_client()
        task_id = client.post("/api/task", json={"task": "timestamp check"}).json()["task_id"]
        data = _wait_for_status(client, task_id, {"done", "error", "cancelled"})
        assert data["started_at"] is not None
        assert data["finished_at"] is not None

    def test_steps_have_expected_fields(self):
        """Each step should have seq, kind, content, data.

        Uses a context-manager TestClient so the event loop stays alive and
        the background task is not spuriously cancelled before steps are written.
        """
        TASKS.clear()
        with TestClient(app, raise_server_exceptions=True) as client:
            task_id = client.post("/api/task", json={"task": "step fields"}).json()["task_id"]
            data = _wait_for_status(client, task_id, {"done", "error", "cancelled"}, timeout=10.0)
            for step in data["steps"]:
                for field in ("seq", "kind", "content", "data"):
                    assert field in step, f"step missing field: {field}"

    def test_step_kinds_from_dry_run(self):
        """DryRunAgent should produce plan, text, tool_call, tool_result, done events.

        Uses a context-manager TestClient so the event loop stays alive and
        the background task is not spuriously cancelled before all events are
        emitted.
        """
        TASKS.clear()
        with TestClient(app, raise_server_exceptions=True) as client:
            task_id = client.post("/api/task", json={"task": "event kinds"}).json()["task_id"]
            data = _wait_for_status(client, task_id, {"done"}, timeout=10.0)
            kinds = {step["kind"] for step in data["steps"]}
            # DryRunAgent always yields at least these
            assert "plan" in kinds
            assert "done" in kinds


# ---------------------------------------------------------------------------
# POST /api/task/{task_id}/cancel
# ---------------------------------------------------------------------------

class TestCancelTask:
    def test_cancel_returns_200(self):
        client = _fresh_client()
        task_id = client.post("/api/task", json={"task": "cancel me"}).json()["task_id"]
        r = client.post(f"/api/task/{task_id}/cancel")
        assert r.status_code == 200

    def test_cancel_ok_true(self):
        client = _fresh_client()
        task_id = client.post("/api/task", json={"task": "cancel me"}).json()["task_id"]
        data = client.post(f"/api/task/{task_id}/cancel").json()
        assert data["ok"] is True

    def test_cancel_finished_task_returns_ok(self):
        """Cancelling an already-finished task should still return ok."""
        client = _fresh_client()
        task_id = client.post("/api/task", json={"task": "finish first"}).json()["task_id"]
        _wait_for_status(client, task_id, {"done", "error", "cancelled"})
        data = client.post(f"/api/task/{task_id}/cancel").json()
        assert data["ok"] is True

    def test_cancel_nonexistent_task_returns_404(self):
        client = _fresh_client()
        r = client.post("/api/task/no-such-id/cancel")
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# POST /api/task/{task_id}/approve
# ---------------------------------------------------------------------------

class TestApproveTask:
    def test_approve_existing_task_returns_200(self):
        client = _fresh_client()
        task_id = client.post("/api/task", json={"task": "approve me"}).json()["task_id"]
        r = client.post(f"/api/task/{task_id}/approve")
        assert r.status_code == 200

    def test_approve_ok_true(self):
        client = _fresh_client()
        task_id = client.post("/api/task", json={"task": "approve me"}).json()["task_id"]
        data = client.post(f"/api/task/{task_id}/approve").json()
        assert data["ok"] is True

    def test_approve_nonexistent_task_returns_404(self):
        client = _fresh_client()
        r = client.post("/api/task/no-such-id/approve")
        assert r.status_code == 404

    def test_approve_includes_stub_note(self):
        """The stub implementation should include a 'note' field."""
        client = _fresh_client()
        task_id = client.post("/api/task", json={"task": "approve stub"}).json()["task_id"]
        data = client.post(f"/api/task/{task_id}/approve").json()
        assert "note" in data


# ---------------------------------------------------------------------------
# SSE stream — basic smoke test (not a full event-stream parse)
# ---------------------------------------------------------------------------

class TestStreamTask:
    def test_stream_already_done_task_returns_200(self):
        """For a completed task, the stream endpoint should respond with 200."""
        client = _fresh_client()
        task_id = client.post("/api/task", json={"task": "stream me"}).json()["task_id"]
        _wait_for_status(client, task_id, {"done", "error", "cancelled"})
        r = client.get(f"/api/task/{task_id}/stream")
        assert r.status_code == 200

    def test_stream_content_type_is_event_stream(self):
        client = _fresh_client()
        task_id = client.post("/api/task", json={"task": "stream ct"}).json()["task_id"]
        _wait_for_status(client, task_id, {"done", "error", "cancelled"})
        r = client.get(f"/api/task/{task_id}/stream")
        assert "text/event-stream" in r.headers.get("content-type", "")

    def test_stream_nonexistent_task_returns_404(self):
        client = _fresh_client()
        r = client.get("/api/task/no-such-id/stream")
        assert r.status_code == 404

    def test_stream_body_contains_data_lines(self):
        """SSE format: each event should be a 'data: ...' line."""
        client = _fresh_client()
        task_id = client.post("/api/task", json={"task": "stream body"}).json()["task_id"]
        _wait_for_status(client, task_id, {"done", "error", "cancelled"})
        r = client.get(f"/api/task/{task_id}/stream")
        body = r.text
        data_lines = [line for line in body.splitlines() if line.startswith("data:")]
        assert len(data_lines) > 0

    def test_stream_contains_done_event(self):
        """The stream must include a terminal 'done' sentinel."""
        import json as _json
        client = _fresh_client()
        task_id = client.post("/api/task", json={"task": "stream done"}).json()["task_id"]
        _wait_for_status(client, task_id, {"done", "error", "cancelled"})
        r = client.get(f"/api/task/{task_id}/stream")
        body = r.text
        found_done = False
        for line in body.splitlines():
            if line.startswith("data:"):
                try:
                    payload = _json.loads(line[len("data:"):].strip())
                    if payload.get("kind") == "done":
                        found_done = True
                        break
                except _json.JSONDecodeError:
                    pass
        assert found_done, "stream did not include a 'done' event"


# ---------------------------------------------------------------------------
# Error shape consistency
# ---------------------------------------------------------------------------

class TestErrorShapes:
    """Verify that all 404 responses follow the {ok, error: {code, message}} shape."""

    def test_get_unknown_task_error_shape(self):
        client = _fresh_client()
        detail = client.get("/api/task/bogus").json()["detail"]
        assert detail["ok"] is False
        assert "error" in detail
        assert "code" in detail["error"]
        assert "message" in detail["error"]

    def test_cancel_unknown_task_error_shape(self):
        client = _fresh_client()
        detail = client.post("/api/task/bogus/cancel").json()["detail"]
        assert detail["ok"] is False
        assert "error" in detail

    def test_approve_unknown_task_error_shape(self):
        client = _fresh_client()
        detail = client.post("/api/task/bogus/approve").json()["detail"]
        assert detail["ok"] is False
        assert "error" in detail
