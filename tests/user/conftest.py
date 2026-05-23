"""
AutoGUI user-test fixtures.

These tests differ from tests/test_*.py (which run in-process against
the FastAPI TestClient and a StubClient) — they boot real subprocesses:
``python api.py``, ``python main.py``, and ``python OSScreenObserver/main.py``
for integration tests. The goal is to exercise CLI flags, REST/SSE
framing, MCP framing, the controller loop running end-to-end, the X11
backend on Xvfb, the Playwright browser tools, etc.

The existing tests/conftest.py StubClient + StubRegistry are re-exported
here so test files that drive the controller in-process can use them
without two parallel implementations.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
OSO_ROOT = ROOT / "OSScreenObserver"

# Make the existing unit-test fixtures importable:
#   StubClient, StubRegistry, make_assistant_text, make_tool_call.
sys.path.insert(0, str(ROOT / "tests"))
from conftest import (  # noqa: E402,F401
    StubClient,
    StubRegistry,
    make_assistant_text,
    make_tool_call,
)


# ---------------------------------------------------------------------------
# Process helpers
# ---------------------------------------------------------------------------

def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_http(url: str, timeout: float = 20.0, interval: float = 0.2) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1.0) as r:
                if r.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(interval)
    return False


def _kill_proc(p: subprocess.Popen) -> None:
    if p.poll() is not None:
        return
    try:
        p.send_signal(signal.SIGTERM)
        try:
            p.wait(timeout=5.0)
            return
        except subprocess.TimeoutExpired:
            pass
        p.kill()
        p.wait(timeout=2.0)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# AutoGUI REST API (api.py) subprocess fixture
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def autogui_api_factory(tmp_path_factory):
    """Factory that boots `python api.py` subprocesses on free ports.

    Defaults to AUTOGUI_DRY_RUN=true so the DryRunAgent answers; pass
    ``dry_run=False`` for tests that wire a real (stubbed) client via
    env variables (AUTOGUI_LLM_BASE_URL etc.).
    """
    spawned: list[subprocess.Popen] = []

    def _spawn(env_overrides: dict | None = None,
               dry_run: bool = True,
               cwd: Path | None = None) -> dict:
        port = _free_port()
        cwd = cwd or tmp_path_factory.mktemp("autogui_api")
        env = dict(os.environ)
        env["AUTOGUI_DRY_RUN"] = "true" if dry_run else "false"
        env["AUTOGUI_API_PORT"] = str(port)
        # Use a clearly-nonexistent config path so api.py picks env defaults.
        env.setdefault("AUTOGUI_CONFIG", "__no_config__.json")
        env["PYTHONUNBUFFERED"] = "1"
        if env_overrides:
            env.update(env_overrides)
        stderr_log = cwd / "api.stderr.log"
        proc = subprocess.Popen(
            [sys.executable, str(ROOT / "api.py")],
            cwd=str(cwd),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=stderr_log.open("wb"),
        )
        spawned.append(proc)
        base_url = f"http://127.0.0.1:{port}"
        if not _wait_for_http(f"{base_url}/api/healthz"):
            proc.terminate()
            raise RuntimeError(
                f"AutoGUI api.py did not become healthy. "
                f"stderr:\n{stderr_log.read_text(errors='replace')}"
            )
        return {"proc": proc, "base_url": base_url,
                "port": port, "stderr_log": stderr_log, "cwd": cwd}

    yield _spawn

    for p in spawned:
        _kill_proc(p)


@pytest.fixture
def autogui_api(autogui_api_factory):
    return autogui_api_factory()


# ---------------------------------------------------------------------------
# OSScreenObserver subprocess fixture (for Tier C)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def oso_server_factory(tmp_path_factory):
    """Spawns OSScreenObserver from the submodule on a free port."""
    spawned: list[subprocess.Popen] = []
    oso_main = OSO_ROOT / "main.py"
    if not oso_main.exists():
        pytest.skip("OSScreenObserver submodule not initialised (no OSScreenObserver/main.py)")

    def _spawn(mode: str = "inspect", mock: bool = True,
               extra_args: list[str] | None = None) -> dict:
        port = _free_port()
        cwd = tmp_path_factory.mktemp("oso_cwd")
        argv = [
            sys.executable, str(oso_main),
            "--mode", mode,
            "--port", str(port),
        ]
        if mock:
            argv.append("--mock")
        if extra_args:
            argv.extend(extra_args)
        env = dict(os.environ)
        env["PYTHONUNBUFFERED"] = "1"
        stderr_log = cwd / "oso.stderr.log"
        proc = subprocess.Popen(
            argv,
            cwd=str(cwd),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=stderr_log.open("wb"),
        )
        spawned.append(proc)
        base_url = f"http://127.0.0.1:{port}"
        if mode in ("inspect", "both"):
            if not _wait_for_http(f"{base_url}/api/healthz"):
                proc.terminate()
                raise RuntimeError(
                    f"OSScreenObserver did not become healthy. "
                    f"stderr:\n{stderr_log.read_text(errors='replace')}"
                )
        return {"proc": proc, "base_url": base_url, "port": port,
                "cwd": cwd, "stderr_log": stderr_log}

    yield _spawn

    for p in spawned:
        _kill_proc(p)


@pytest.fixture
def oso_server(oso_server_factory):
    return oso_server_factory()


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------

class HttpJson:
    def __init__(self, base_url: str, timeout: float = 10.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def get(self, path: str, params: dict | None = None) -> tuple[int, dict]:
        url = self.base_url + path
        if params:
            from urllib.parse import urlencode
            url += "?" + urlencode(params)
        return self._send(urllib.request.Request(url))

    def post(self, path: str, body: dict | None = None) -> tuple[int, dict]:
        data = json.dumps(body or {}).encode()
        req = urllib.request.Request(self.base_url + path, data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        return self._send(req)

    def _send(self, req) -> tuple[int, dict]:
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                raw = r.read()
                try:
                    return r.status, json.loads(raw or b"{}")
                except json.JSONDecodeError:
                    return r.status, {"_raw": raw.decode(errors="replace")}
        except urllib.error.HTTPError as e:
            try:
                payload = json.loads(e.read() or b"{}")
            except Exception:
                payload = {"_error": str(e)}
            return e.code, payload

    def sse(self, path: str, max_events: int = 50,
            timeout: float = 30.0):
        """Iterate parsed SSE events from `path` until the connection closes
        or `max_events` is reached. Yields dicts with 'event' and 'data' keys."""
        req = urllib.request.Request(self.base_url + path)
        req.add_header("Accept", "text/event-stream")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            buf: list[str] = []
            events = 0
            for raw in r:
                line = raw.decode("utf-8").rstrip("\n").rstrip("\r")
                if line == "":
                    if buf:
                        yield _parse_sse(buf)
                        buf = []
                        events += 1
                        if events >= max_events:
                            return
                else:
                    buf.append(line)


def _parse_sse(lines: list[str]) -> dict:
    ev = "message"
    data_lines: list[str] = []
    for line in lines:
        if line.startswith("event:"):
            ev = line.split(":", 1)[1].strip()
        elif line.startswith("data:"):
            data_lines.append(line.split(":", 1)[1].lstrip())
    body = "\n".join(data_lines)
    try:
        parsed = json.loads(body) if body else None
    except json.JSONDecodeError:
        parsed = body
    return {"event": ev, "data": parsed}


@pytest.fixture
def http(autogui_api):
    return HttpJson(autogui_api["base_url"])


# ---------------------------------------------------------------------------
# Static HTML fixture server for browser tests
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def html_fixture_server():
    """Serves tests/user/fixtures/html/ on a free port.

    Yields the base URL, e.g. http://127.0.0.1:NNNN/.
    """
    import http.server
    import threading

    fixtures_dir = Path(__file__).resolve().parent / "fixtures" / "html"
    fixtures_dir.mkdir(parents=True, exist_ok=True)
    port = _free_port()

    class _SilentHandler(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *args, **kwargs):
            pass

    handler = _SilentHandler
    # Pin the working directory by binding it to the handler.
    cwd = os.getcwd()
    os.chdir(fixtures_dir)
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()
        server.server_close()
        os.chdir(cwd)


# ---------------------------------------------------------------------------
# Display / Ollama probes
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def has_display():
    if not os.environ.get("DISPLAY"):
        return False
    if not shutil.which("xdpyinfo"):
        return False
    return subprocess.run(["xdpyinfo"], stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL).returncode == 0


@pytest.fixture
def require_display(has_display):
    if not has_display:
        pytest.skip("X11 display required (set DISPLAY to point at Xvfb / a real X server)")


@pytest.fixture(scope="session")
def ollama_base_url():
    candidates = [
        os.environ.get("AUTOGUI_LLM_BASE_URL"),
        os.environ.get("OLLAMA_BASE_URL"),
        "http://127.0.0.1:11434",
    ]
    for url in candidates:
        if not url:
            continue
        try:
            with urllib.request.urlopen(f"{url.rstrip('/')}/api/tags", timeout=1.5) as r:
                if r.status == 200:
                    return url.rstrip("/")
        except Exception:
            continue
    return None


@pytest.fixture(scope="session")
def chat_model():
    return os.environ.get("AUTOGUI_LLM_MODEL", "qwen2.5:0.5b")


@pytest.fixture(scope="session")
def vlm_model():
    return os.environ.get("AUTOGUI_VLM_MODEL", "qwen2.5vl:3b")


# ---------------------------------------------------------------------------
# Spawned X11 apps
# ---------------------------------------------------------------------------

@pytest.fixture
def xterm_window(require_display):
    """Spawn an xterm with a known title and yield its handle."""
    if not shutil.which("xterm"):
        pytest.skip("xterm not installed")
    title = f"autogui-user-{os.getpid()}-{int(time.time()*1000) % 100000}"
    proc = subprocess.Popen(
        ["xterm", "-T", title, "-geometry", "80x20", "-e",
         "bash", "-c", "echo USERTEST-OK-HELLO; sleep 60"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    # Wait for the window to appear via wmctrl.
    if shutil.which("wmctrl"):
        for _ in range(50):
            r = subprocess.run(["wmctrl", "-l"], capture_output=True, text=True)
            if title in (r.stdout or ""):
                break
            time.sleep(0.1)
    else:
        time.sleep(1.5)
    try:
        yield {"title": title, "proc": proc}
    finally:
        _kill_proc(proc)


# ---------------------------------------------------------------------------
# Stub-LLM agent harness (no subprocess; in-process StubClient)
# ---------------------------------------------------------------------------

@pytest.fixture
def scripted_agent(tmp_path):
    """Build a full Agent wired to a StubClient + ToolRegistry on Xvfb.

    Test files call ``scripted_agent.queue(...)`` to append scripted LLM
    responses, then ``await scripted_agent.run("task")`` to drive the
    real Agent loop (planner + controller + tool dispatcher), without
    any network call.
    """
    from agent import Agent
    from tools import ToolRegistry

    # Bare-bones config: no LLM-blocking features that need a real model.
    cfg = {
        "openwebui": {"base_url": "http://stub", "api_key": "", "model": "stub-model"},
        "prompts_dir": str(ROOT / "prompts"),
        "agent": {
            "max_iterations": 8,
            "confirm_destructive": False,
            "vision_screenshots": False,
            "record_trace": True,
            "trace_dir": str(tmp_path / "traces"),
            "skills_enabled": False,
            "suggest_skills": False,
            "skills_path": str(tmp_path / "skills.jsonl"),
            "drift_anchor": {"enabled": False, "capture_phash": False},
            "planner": {"enabled": False},
            "controller": {
                "enabled": False,
                "step_max_iterations": 4,
                "step_max_retries": 1,
                "auto_resume": False,
                "replan_on_block": False,
                "critique_enabled": False,
                "preflight_enabled": False,
                "predicate_check_enabled": False,
                "visual_diff_enabled": False,
                "watchdog_stall_threshold": 0,
                "recovery_probe_enabled": False,
                "recovery_probe_max_per_step": 0,
            },
            "artifacts": {"dir": str(tmp_path / "artifacts")},
            "progress": {"dir": str(tmp_path / "progress")},
            "memory": {"enabled": False, "dir": str(tmp_path / "memory")},
            "budget": {"max_tool_calls": 0, "max_chat_calls": 0,
                       "max_total_tokens": 0, "max_seconds": 0},
            "subagent": {"enabled": False, "max_tool_calls": 0},
            "bon": {"enabled": False, "n": 1, "temperature": 0.0,
                    "trigger_on_recent_failure": False,
                    "trigger_on_validator_disagreement": False},
            "screen_record": {"enabled": False, "fps": 1, "buffer_seconds": 1.0,
                              "max_width": 320,
                              "out_dir": str(tmp_path / "failures")},
        },
        "tools": {
            "shell_timeout_seconds": 5,
            "screenshot_dir": str(tmp_path / "screenshots"),
            "max_screenshot_width": 800,
            "perception_cache_ttl_seconds": 0.0,
            "allowed_shell": True,
            "allowed_filesystem": True,
            "allowed_desktop": True,
            "allowed_browser": False,
        },
        "browser": {"headless": True,
                    "screenshot_dir": str(tmp_path / "browser"),
                    "user_data_dir": "", "viewport": {"width": 640, "height": 480}},
        "logging": {"level": "WARNING", "file": str(tmp_path / "agent.log"),
                    "max_bytes": 1024, "backup_count": 1},
        "tui": {"theme": "dark", "show_tool_calls": False, "show_token_counts": False,
                "history_file": str(tmp_path / "history.jsonl")},
        "safety": {"command_confirm_delay_seconds": 0,
                   "dry_run": False, "allowed_apps": [], "blocked_window_titles": [],
                   "fs_write_snapshot_dir": ""},
        "screen_observer": {"enabled": False,
                             "base_url": "http://127.0.0.1:5901",
                             "timeout_seconds": 2.0,
                             "text_observation": {"enabled": False}},
    }
    client = StubClient()
    registry = ToolRegistry(cfg)
    agent = Agent(client, registry, cfg)
    return {"agent": agent, "client": client, "registry": registry, "cfg": cfg,
            "tmp_path": tmp_path}


# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------

@pytest.fixture
def text_image_bytes():
    from PIL import Image, ImageDraw, ImageFont

    def _render(text: str, size: tuple[int, int] = (480, 120)) -> bytes:
        img = Image.new("RGB", size, "white")
        draw = ImageDraw.Draw(img)
        try:
            font = ImageFont.truetype("DejaVuSans-Bold.ttf", 36)
        except OSError:
            font = ImageFont.load_default()
        draw.text((20, 30), text, fill="black", font=font)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()

    return _render


@contextlib.contextmanager
def kill_at_exit(*procs: subprocess.Popen):
    try:
        yield
    finally:
        for p in procs:
            _kill_proc(p)
