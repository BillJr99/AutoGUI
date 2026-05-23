"""
Verify the OSScreenObserver MCP protocol works end-to-end for a Python
client (the AutoGUI side currently uses REST, but the MCP path must
remain working so the integration story stays open).
"""
from __future__ import annotations

import json
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = [pytest.mark.user, pytest.mark.integration]

ROOT = Path(__file__).resolve().parents[3]
OSO_MAIN = ROOT / "OSScreenObserver" / "main.py"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _NDJSONMCPClient:
    def __init__(self, proc):
        self.proc = proc
        self._id = 0

    def _send(self, msg):
        self.proc.stdin.write((json.dumps(msg) + "\n").encode())
        self.proc.stdin.flush()

    def _read(self, timeout=5.0):
        deadline = time.monotonic() + timeout
        buf = b""
        while True:
            if time.monotonic() > deadline:
                raise TimeoutError("MCP read timeout")
            chunk = self.proc.stdout.read(1)
            if not chunk:
                raise RuntimeError("MCP stdout closed")
            if chunk == b"\n":
                if buf:
                    return json.loads(buf.decode())
                continue
            buf += chunk

    def request(self, method, params=None):
        self._id += 1
        msg = {"jsonrpc": "2.0", "id": self._id, "method": method}
        if params is not None:
            msg["params"] = params
        self._send(msg)
        while True:
            r = self._read()
            if r.get("id") == self._id:
                return r


@pytest.fixture
def mcp_oso():
    if not OSO_MAIN.exists():
        pytest.skip("OSScreenObserver submodule not present")
    proc = subprocess.Popen(
        [sys.executable, str(OSO_MAIN), "--mode", "mcp", "--mock"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        yield _NDJSONMCPClient(proc)
    finally:
        try:
            proc.send_signal(signal.SIGTERM)
            proc.wait(timeout=5)
        except Exception:
            proc.kill()


def test_mcp_initialize_and_list_tools(mcp_oso):
    r = mcp_oso.request("initialize",
                         {"protocolVersion": "2024-11-05",
                          "capabilities": {},
                          "clientInfo": {"name": "autogui-int", "version": "0"}})
    assert r["result"]["serverInfo"]["name"] == "os-screen-observer"

    r = mcp_oso.request("tools/list", {})
    names = [t["name"] for t in r["result"]["tools"]]
    for required in ("list_windows", "find_element", "click_element",
                      "observe_window", "get_screen_description"):
        assert required in names


def test_mcp_path_can_drive_a_full_scenario(mcp_oso):
    mcp_oso.request("initialize",
                     {"protocolVersion": "2024-11-05",
                      "capabilities": {},
                      "clientInfo": {"name": "x", "version": "0"}})
    # 1. List windows.
    r = mcp_oso.request("tools/call",
                         {"name": "list_windows", "arguments": {}})
    payload = json.loads(r["result"]["content"][0]["text"])
    assert payload["ok"] is True
    # 2. Find an element.
    r = mcp_oso.request("tools/call",
                         {"name": "find_element",
                          "arguments": {"window_index": 0,
                                        "selector": 'Window/MenuBar/MenuItem[name="Edit"]'}})
    payload = json.loads(r["result"]["content"][0]["text"])
    assert payload["ok"] is True
    eid = payload["element_id"]
    # 3. Click it in dry-run.
    r = mcp_oso.request("tools/call",
                         {"name": "click_element",
                          "arguments": {"window_index": 0,
                                        "element_id": eid,
                                        "dry_run": True}})
    payload = json.loads(r["result"]["content"][0]["text"])
    assert payload["ok"] is True
