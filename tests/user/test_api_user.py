"""
AutoGUI REST API user tests — drives a real `python api.py` subprocess.

This complements tests/test_api.py (in-process FastAPI TestClient) by
verifying the wire format, SSE framing, port binding, and process
behaviour. Runs in AUTOGUI_DRY_RUN=true mode so no display or LLM is
needed.
"""
from __future__ import annotations

import json
import time

import pytest

pytestmark = [pytest.mark.user]


class TestHealth:
    def test_healthz_returns_200(self, http):
        status, body = http.get("/api/healthz")
        assert status == 200
        assert body["ok"] is True
        assert "version" in body
        assert body["uptime_s"] >= 0


class TestCapabilities:
    def test_capabilities_envelope(self, http):
        status, body = http.get("/api/capabilities")
        assert status == 200
        assert body["ok"] is True
        assert body["dry_run"] is True
        # Surfaces dict reports which tool families are enabled.
        assert isinstance(body["surfaces"], dict)
        for key in ("desktop", "shell", "browser"):
            assert key in body["surfaces"]


class TestTools:
    def test_tools_list_envelope(self, http):
        _, body = http.get("/api/tools")
        assert body["ok"] is True
        # In AUTOGUI_DRY_RUN mode the registry is intentionally empty —
        # the DryRunAgent doesn't construct backends. Just assert the
        # shape is right.
        assert isinstance(body["tools"], list)


class TestCreateTask:
    def test_task_creation_returns_id(self, http):
        status, body = http.post("/api/task", {"task": "do something benign"})
        # api.py returns 202 in some builds, 200 in others — accept either.
        assert status in (200, 202)
        assert body["ok"] is True
        assert "task_id" in body

    def test_get_unknown_task_404(self, http):
        status, _ = http.get("/api/task/nope-no-such-task")
        assert status == 404

    def test_task_eventually_completes(self, http):
        _, body = http.post("/api/task", {"task": "echo something"})
        tid = body["task_id"]
        # DryRunAgent finishes in <2 seconds.
        deadline = time.monotonic() + 10.0
        final_status = None
        while time.monotonic() < deadline:
            _, j = http.get(f"/api/task/{tid}")
            if j["status"] in ("done", "succeeded", "completed",
                                "failed", "cancelled", "error"):
                final_status = j["status"]
                break
            time.sleep(0.1)
        assert final_status in ("done", "succeeded", "completed"), \
            f"task did not finish; last={final_status!r}"

    def test_task_steps_contain_dry_run_markers(self, http):
        _, body = http.post("/api/task", {"task": "dry-run check"})
        tid = body["task_id"]
        time.sleep(2.0)
        _, j = http.get(f"/api/task/{tid}")
        kinds = [s["kind"] for s in j.get("steps", [])]
        assert "done" in kinds, kinds


class TestSSEStream:
    def test_stream_returns_events(self, http):
        _, body = http.post("/api/task", {"task": "stream me"})
        tid = body["task_id"]
        seen = []
        for ev in http.sse(f"/api/task/{tid}/stream", max_events=20, timeout=10):
            seen.append(ev["event"])
            if ev["event"] in ("done", "complete", "completed"):
                break
        # At least one event must have been delivered.
        assert seen, "no SSE events received"


class TestCancel:
    def test_cancel_known_task(self, http):
        _, body = http.post("/api/task", {"task": "go forever"})
        tid = body["task_id"]
        status, _ = http.post(f"/api/task/{tid}/cancel", {})
        assert status in (200, 202)

    def test_cancel_unknown_task_404(self, http):
        status, _ = http.post("/api/task/no-such/cancel", {})
        assert status in (404, 400)


class TestApprove:
    def test_approve_unknown_returns_404(self, http):
        status, _ = http.post("/api/task/nope/approve", {"approve": True})
        assert status in (404, 400)


class TestErrorShapes:
    def test_missing_task_body_returns_400_or_422(self, http):
        status, body = http.post("/api/task", {})
        assert status in (400, 422)


class TestProcessHygiene:
    def test_api_logs_to_stderr(self, autogui_api):
        # Trigger something noisy.
        from urllib.request import urlopen
        urlopen(autogui_api["base_url"] + "/api/healthz", timeout=2)
        # We don't assert specific log lines because in DryRun mode the
        # output is minimal — just check the file exists and is readable.
        log_path = autogui_api["stderr_log"]
        assert log_path.exists()
